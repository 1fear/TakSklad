import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.attributes import flag_modified

from .event_leases import claim_event_leases, event_leases_enabled, finalize_event_leases
from .models import (
    AuditLog,
    ImportJob,
    Incident,
    Order,
    OrderItem,
    PendingEvent,
    SmartupFulfillment,
    SmartupFulfillmentOrder,
)
from .observability_context import bind_pending_event
from .outbox_service import (
    OutboxIdentityConflict,
    outbox_event_identity_conflict_reason,
    queue_outbox_event,
)
from .representative_contacts import build_representative_comment, find_representative_contact
from .skladbot_client import (
    SkladBotApiError,
    SkladBotClient,
    SkladBotErrorKind,
    env_int,
    notify_skladbot_progress,
    sanitize_skladbot_error,
)
from .skladbot_contracts import (
    TAKSKLAD_MARKER_RE,
    build_taksklad_marker,
    canonical_remote_request_id,
    canonical_skladbot_request_link,
    canonical_skladbot_request_number,
    format_internal_smartup_ids,
    is_stock_shortage_text,
    normalize_request_payload,
    normalize_text,
    parse_int,
    product_sku_key,
    request_list_value,
    request_has_exact_taksklad_marker,
    taksklad_marker_from_comment,
)


SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE = "skladbot_request_dry_run"
SKLADBOT_REQUEST_CREATE_EVENT_TYPE = "skladbot_request_create"
SKLADBOT_CREATE_REQUESTS_MODE_ENV = "SKLADBOT_CREATE_REQUESTS_MODE"
SKLADBOT_CREATE_REQUESTS_DEFAULT_MODE = "dry_run"
SKLADBOT_CUSTOMER_ID = 6211
SKLADBOT_REQUEST_TYPE_ID = 3389
SKLADBOT_REQUEST_CREATE_LIMIT_ENV = "SKLADBOT_REQUEST_CREATE_LIMIT"
SKLADBOT_SKU_MAPPING_JSON_ENV = "SKLADBOT_SKU_MAPPING_JSON"
STALE_SKLADBOT_CREATE_TIMEOUT = timedelta(minutes=10)
TELEGRAM_NOTIFICATION_EVENT_TYPE = "telegram_notification"
POST_OUTCOME_AMBIGUOUS_STATES = {"started", "ambiguous", "response_received"}

DEFAULT_SKU_MAPPING = {
    "red:op": {
        "product_data_id": 2189390,
        "barcode": "4006396053947",
        "is_main_barcode": False,
    },
    "brown:op": {
        "product_data_id": 2189391,
        "barcode": "4006396053978",
        "is_main_barcode": False,
    },
    "brown:ssl": {
        "product_data_id": 2189392,
        "barcode": "4006396054067",
        "is_main_barcode": False,
    },
    "gold:ssl": {
        "product_data_id": 2189394,
        "barcode": "4006396054005",
        "is_main_barcode": False,
    },
    "green:op": {
        "product_data_id": 2430805,
        "barcode": "4006396104441",
        "is_main_barcode": False,
    },
    "red:ssl": {
        "product_data_id": 2189393,
        "barcode": "4006396054036",
        "is_main_barcode": False,
    },
}
SKU_MAPPING = DEFAULT_SKU_MAPPING


def skladbot_create_requests_mode(environ: dict[str, str] | None = None) -> str:
    environ = environ or os.environ
    mode = normalize_text(environ.get(SKLADBOT_CREATE_REQUESTS_MODE_ENV)).lower()
    if mode in {"dry_run", "enabled", "disabled"}:
        return mode
    return SKLADBOT_CREATE_REQUESTS_DEFAULT_MODE


def load_sku_mapping(environ: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    environ = environ or os.environ
    raw_mapping = normalize_text(environ.get(SKLADBOT_SKU_MAPPING_JSON_ENV))
    mapping = {key: dict(value) for key, value in DEFAULT_SKU_MAPPING.items()}
    if not raw_mapping:
        return mapping

    try:
        overrides = json.loads(raw_mapping)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{SKLADBOT_SKU_MAPPING_JSON_ENV} содержит невалидный JSON") from exc
    if not isinstance(overrides, dict):
        raise ValueError(f"{SKLADBOT_SKU_MAPPING_JSON_ENV} должен быть JSON object")

    for raw_key, raw_value in overrides.items():
        sku_key = normalize_text(raw_key).lower()
        if not sku_key:
            raise ValueError(f"{SKLADBOT_SKU_MAPPING_JSON_ENV} содержит пустой SKU key")
        mapping[sku_key] = validate_sku_mapping_entry(sku_key, raw_value)
    return mapping


def validate_sku_mapping_entry(sku_key: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{SKLADBOT_SKU_MAPPING_JSON_ENV}.{sku_key} должен быть object")
    product_data_id = parse_int(value.get("product_data_id"))
    barcode = normalize_text(value.get("barcode"))
    is_main_barcode = value.get("is_main_barcode")
    if product_data_id <= 0:
        raise ValueError(f"{SKLADBOT_SKU_MAPPING_JSON_ENV}.{sku_key}.product_data_id должен быть positive integer")
    if not barcode:
        raise ValueError(f"{SKLADBOT_SKU_MAPPING_JSON_ENV}.{sku_key}.barcode обязателен")
    if not isinstance(is_main_barcode, bool):
        raise ValueError(f"{SKLADBOT_SKU_MAPPING_JSON_ENV}.{sku_key}.is_main_barcode должен быть boolean")
    return {
        "product_data_id": product_data_id,
        "barcode": barcode,
        "is_main_barcode": is_main_barcode,
    }


def create_skladbot_dry_run_for_import(
    db: Session,
    import_id: str,
    rebuild: bool = False,
    *,
    force_mode: str | None = None,
) -> dict[str, Any]:
    configured_mode = normalize_text(force_mode).lower() if force_mode is not None else skladbot_create_requests_mode()
    if configured_mode not in {"dry_run", "enabled", "disabled"}:
        configured_mode = skladbot_create_requests_mode()
    if configured_mode == "disabled":
        return {
            "status": "disabled",
            "mode": configured_mode,
            "orders": 0,
            "ready": 0,
            "blocked": 0,
            "already_linked": 0,
            "queued": 0,
            "created": 0,
            "recovered": 0,
            "create_failed": 0,
            "event_id": "",
        }
    mode = configured_mode if configured_mode == "enabled" and not rebuild else "dry_run"

    existing_event = find_skladbot_dry_run_event(db, import_id)
    if existing_event is not None and not rebuild:
        summary = (existing_event.payload or {}).get("summary") or {}
        if configured_mode == "enabled":
            dry_runs = (existing_event.payload or {}).get("dry_runs") or []
            queued = queue_skladbot_create_events(db, import_id, dry_runs)
            if queued:
                summary = {
                    **default_summary(mode="enabled"),
                    **summary,
                    "mode": "enabled",
                    "queued": int(summary.get("queued") or 0) + queued,
                    "ready": max(0, int(summary.get("ready") or 0) - queued),
                    "events_queued": int(summary.get("events_queued") or 0) + queued,
                }
                existing_event.payload = {
                    **(existing_event.payload or {}),
                    "mode": "enabled",
                    "configured_mode": configured_mode,
                    "would_post": True,
                    "summary": summary,
                    "dry_runs": dry_runs,
                }
                db.add(existing_event)
        return {
            **default_summary(mode=summary.get("mode") or mode),
            **summary,
            "status": "deduplicated",
            "event_id": str(existing_event.id),
        }

    orders = list_orders_for_import(db, import_id)
    dry_runs = [
        build_order_dry_run(
            order,
            items,
            import_id,
            index,
            representative_contact=find_representative_contact(db, order.representative),
        )
        for index, (order, items) in enumerate(orders, start=1)
    ]
    queued = 0
    if configured_mode == "enabled" and not rebuild:
        queued = queue_skladbot_create_events(db, import_id, dry_runs)
    summary = summarize_dry_runs(dry_runs, mode=mode)
    summary["events_queued"] = queued
    generated_at = datetime.now(timezone.utc).isoformat()
    event_payload = {
        "version": 1,
        "mode": mode,
        "configured_mode": configured_mode,
        "dry_run": mode != "enabled",
        "would_post": mode == "enabled",
        "import_id": import_id,
        "generated_at": generated_at,
        "summary": summary,
        "dry_runs": dry_runs,
    }

    if existing_event is None:
        event = PendingEvent(
            event_type=SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE,
            action=SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE,
            aggregate_type="import",
            aggregate_id=import_id,
            status="completed",
            attempts=0,
            payload=event_payload,
            last_error=None,
        )
        db.add(event)
        db.flush()
    else:
        event = existing_event
        event.action = event.action or SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE
        event.aggregate_type = event.aggregate_type or "import"
        event.aggregate_id = event.aggregate_id or import_id
        event.status = "completed"
        event.attempts = 0
        event.payload = event_payload
        event.last_error = None
        db.add(event)
        db.flush()

    summary = {**summary, "event_id": str(event.id)}
    event.payload = {**event.payload, "summary": summary}
    add_skladbot_dry_run_audit(db, import_id, str(event.id), summary, dry_runs)
    return summary


def create_skladbot_dry_run_for_orders(
    db: Session,
    order_ids: list[str],
    *,
    import_id: str = "",
    force_mode: str | None = None,
) -> dict[str, Any]:
    """Build or queue SkladBot creates for explicit canonical orders.

    This path is intentionally independent from ``backend_import_id`` so a
    duplicate-only retry can resume fulfillment for the original Order rows.
    """
    configured_mode = normalize_text(force_mode).lower() if force_mode is not None else skladbot_create_requests_mode()
    if configured_mode not in {"dry_run", "enabled", "disabled"}:
        configured_mode = skladbot_create_requests_mode()
    if configured_mode == "disabled":
        return {**default_summary(mode="disabled"), "status": "disabled", "event_id": ""}

    import_id = normalize_text(import_id)
    normalized_order_ids = normalize_explicit_order_ids(order_ids)
    if not normalized_order_ids:
        raise ValueError("At least one canonical order id is required")
    orders = list_orders_for_ids(db, normalized_order_ids)
    found_order_ids = {str(order.id) for order, _items in orders}
    missing_order_ids = [order_id for order_id in normalized_order_ids if order_id not in found_order_ids]
    if missing_order_ids:
        raise ValueError(f"Canonical orders not found: {', '.join(missing_order_ids)}")

    batch_key = explicit_order_batch_key(import_id, normalized_order_ids)
    existing_event = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE)
        .where(PendingEvent.aggregate_type == "order_batch")
        .where(PendingEvent.aggregate_id == batch_key)
        .order_by(PendingEvent.created_at.desc(), PendingEvent.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing_event is not None:
        dry_runs = list((existing_event.payload or {}).get("dry_runs") or [])
        if configured_mode == "enabled":
            queue_skladbot_create_events(db, import_id, dry_runs)
        summary = summarize_dry_runs(dry_runs, mode=configured_mode)
        summary["event_id"] = str(existing_event.id)
        existing_event.payload = {
            **(existing_event.payload or {}),
            "configured_mode": configured_mode,
            "would_post": configured_mode == "enabled",
            "summary": summary,
            "dry_runs": dry_runs,
        }
        return {**summary, "status": "deduplicated"}

    dry_runs = [
        build_order_dry_run(
            order,
            items,
            import_id,
            index,
            representative_contact=find_representative_contact(db, order.representative),
        )
        for index, (order, items) in enumerate(orders, start=1)
    ]
    queued = 0
    if configured_mode == "enabled":
        queued = queue_skladbot_create_events(db, import_id, dry_runs)
    summary = summarize_dry_runs(dry_runs, mode=configured_mode)
    summary["events_queued"] = queued
    generated_at = datetime.now(timezone.utc).isoformat()
    event = PendingEvent(
        event_type=SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE,
        action=SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE,
        aggregate_type="order_batch",
        aggregate_id=batch_key,
        status="completed",
        attempts=0,
        payload={
            "version": 2,
            "mode": configured_mode,
            "configured_mode": configured_mode,
            "dry_run": configured_mode != "enabled",
            "would_post": configured_mode == "enabled",
            "import_id": import_id,
            "explicit_order_ids": normalized_order_ids,
            "generated_at": generated_at,
            "summary": summary,
            "dry_runs": dry_runs,
        },
    )
    db.add(event)
    db.flush()
    summary = {**summary, "event_id": str(event.id)}
    event.payload = {**event.payload, "summary": summary}
    add_skladbot_dry_run_audit(db, import_id, str(event.id), summary, dry_runs)
    return summary


def normalize_explicit_order_ids(order_ids: list[str]) -> list[str]:
    normalized = []
    for raw_order_id in order_ids or []:
        order_id = normalize_text(raw_order_id)
        try:
            canonical = str(uuid.UUID(order_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"Invalid canonical order id: {order_id or '<empty>'}") from exc
        if canonical not in normalized:
            normalized.append(canonical)
    return normalized


def explicit_order_batch_key(import_id: str, order_ids: list[str]) -> str:
    return f"skladbot:orders:{stable_payload_hash({'import_id': import_id, 'order_ids': sorted(order_ids)})[:32]}"


def list_skladbot_dry_runs(
    db: Session,
    import_id: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    row_limit = max(1, min(int(limit or 200), 200))
    row_offset = max(0, int(offset or 0))
    events = list_skladbot_dry_run_events(db, import_id, limit=200)
    result = []
    seen = 0
    for event in events:
        payload = event.payload or {}
        generated_at = payload.get("generated_at") or None
        for row in payload.get("dry_runs") or []:
            if seen < row_offset:
                seen += 1
                continue
            event_id = str(event.id)
            order_id = str(row.get("order_id") or "")
            result.append({
                "id": f"{event_id}:{order_id}" if order_id else event_id,
                "event_id": event_id,
                "import_id": str(row.get("import_id") or payload.get("import_id") or ""),
                "order_id": order_id,
                "client": str(row.get("client") or ""),
                "order_date": row.get("order_date") or None,
                "payment_type": str(row.get("payment_type") or ""),
                "address": str(row.get("address") or ""),
                "blocks": int(row.get("blocks") or 0),
                "status": str(row.get("status") or ""),
                "error": str(row.get("error") or ""),
                "linked_skladbot_blocks": int(row.get("linked_skladbot_blocks") or 0),
                "linked_skladbot_source": str(row.get("linked_skladbot_source") or ""),
                "products": row.get("products") or [],
                "payload": row.get("payload") or {},
                "generated_at": generated_at,
            })
            seen += 1
            if len(result) >= row_limit:
                return attach_skladbot_dry_run_correlations(db, result)
    return attach_skladbot_dry_run_correlations(db, result)


def attach_skladbot_dry_run_correlations(
    db: Session,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    order_ids = {order_id for row in rows if (order_id := parse_uuid(row.get("order_id")))}
    orders = db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id.in_(order_ids))
    ).scalars().unique().all() if order_ids else []
    correlations = {}
    for order in orders:
        raw_payload = order.raw_payload or {}
        correlations[str(order.id)] = {
            "smartup_id": format_internal_smartup_ids([
                raw_payload.get("source_order_id"),
                *((item.raw_payload or {}).get("source_order_id") for item in order.items),
            ]),
            "skladbot_request_id": canonical_remote_request_id(
                raw_payload.get("skladbot_request_id")
            ),
            "skladbot_request_number": canonical_skladbot_request_number(
                raw_payload.get("skladbot_request_number")
            ),
            "skladbot_return_request_id": canonical_remote_request_id(
                raw_payload.get("skladbot_return_request_id")
            ),
            "skladbot_return_request_number": canonical_skladbot_request_number(
                raw_payload.get("skladbot_return_request_number")
            ),
        }
    empty = {
        "smartup_id": "",
        "skladbot_request_id": "",
        "skladbot_request_number": "",
        "skladbot_return_request_id": "",
        "skladbot_return_request_number": "",
    }
    for row in rows:
        row.update(correlations.get(normalize_text(row.get("order_id")), empty))
    return rows


def rebuild_skladbot_dry_run(db: Session, dry_run_id: str) -> list[dict[str, Any]]:
    event_id = normalize_text(dry_run_id).split(":", 1)[0]
    try:
        event_uuid = uuid.UUID(event_id)
    except ValueError as exc:
        raise ValueError("SkladBot dry-run не найден") from exc
    event = db.get(PendingEvent, event_uuid)
    if event is None or event.event_type != SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE:
        raise ValueError("SkladBot dry-run не найден")
    import_id = str((event.payload or {}).get("import_id") or "")
    if not import_id:
        raise ValueError("У dry-run нет import_id")
    create_skladbot_dry_run_for_import(db, import_id, rebuild=True)
    db.commit()
    return list_skladbot_dry_runs(db, import_id)


def list_orders_for_import(db: Session, import_id: str) -> list[tuple[Order, list[Any]]]:
    import_id = normalize_text(import_id)
    stmt = (
        select(Order)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .options(selectinload(Order.items))
        .where(OrderItem.raw_payload["backend_import_id"].as_string() == import_id)
        .order_by(Order.created_at.asc(), Order.client.asc(), Order.id.asc())
    )
    orders = db.execute(stmt).scalars().unique().all()
    matched_orders = []
    for order in orders:
        matched_items = [
            item
            for item in order.items
            if str((item.raw_payload or {}).get("backend_import_id") or "") == import_id
        ]
        if not matched_items:
            continue
        matched_orders.append((order, list(order.items)))
    return matched_orders


def list_orders_for_ids(db: Session, order_ids: list[str]) -> list[tuple[Order, list[Any]]]:
    if not order_ids:
        return []
    order_uuid_by_id = {order_id: uuid.UUID(order_id) for order_id in order_ids}
    orders = db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id.in_(tuple(order_uuid_by_id.values())))
    ).scalars().unique().all()
    by_id = {str(order.id): order for order in orders}
    return [
        (by_id[order_id], list(by_id[order_id].items))
        for order_id in order_ids
        if order_id in by_id
    ]


def find_skladbot_dry_run_event(db: Session, import_id: str) -> PendingEvent | None:
    events = list_skladbot_dry_run_events(db, import_id)
    return events[0] if events else None


def list_skladbot_dry_run_events(
    db: Session,
    import_id: str | None = None,
    *,
    limit: int = 50,
) -> list[PendingEvent]:
    stmt = (
        select(PendingEvent)
        .where(PendingEvent.event_type == SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE)
        .order_by(PendingEvent.created_at.desc(), PendingEvent.id.desc())
    )
    if import_id:
        import_id = normalize_text(import_id)
        stmt = stmt.where(or_(
            PendingEvent.aggregate_id == import_id,
            PendingEvent.payload["import_id"].as_string() == import_id,
        ))
    return db.execute(stmt.limit(max(1, min(int(limit or 50), 200)))).scalars().all()


def queue_skladbot_create_events(db: Session, import_id: str, dry_runs: list[dict[str, Any]]) -> int:
    queued = 0
    for row in dry_runs:
        if str(row.get("status") or "") != "ready":
            continue
        order_id = normalize_text(row.get("order_id"))
        payload = row.get("payload") or {}
        if not order_id or not payload:
            continue
        order = db.get(Order, uuid.UUID(order_id))
        payload_marker = request_payload_taksklad_marker(payload)
        payload = markerless_skladbot_request_payload(payload)
        row["payload"] = payload
        taksklad_marker = payload_marker or (order_taksklad_marker(order) if order is not None else "")
        idempotency_key = skladbot_create_idempotency_key(order_id, marker=taksklad_marker)
        try:
            existing = find_skladbot_create_event_by_key(db, idempotency_key, order_id=order_id)
        except OutboxIdentityConflict as exc:
            mark_skladbot_create_queue_identity_conflict(
                db,
                row,
                import_id=import_id,
                order_id=order_id,
                idempotency_key=idempotency_key,
                existing_event_id=exc.existing_event_id,
                detail=exc.reason,
            )
            continue
        if existing is not None:
            apply_existing_create_event_to_dry_run(row, existing)
            continue

        now = datetime.now(timezone.utc).isoformat()
        try:
            event = queue_outbox_event(
                db,
                event_type=SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
                action=SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
                aggregate_type="order",
                aggregate_id=order_id,
                idempotency_key=idempotency_key,
                payload={
                    "version": 1,
                    "import_id": import_id,
                    "order_id": order_id,
                    "idempotency_key": idempotency_key,
                    "request_payload": payload,
                    "request_payload_hash": stable_payload_hash(payload),
                    "taksklad_marker": taksklad_marker,
                    "queued_at": now,
                    "create_status": "queued",
                },
                strict_identity=True,
            )
        except OutboxIdentityConflict as exc:
            mark_skladbot_create_queue_identity_conflict(
                db,
                row,
                import_id=import_id,
                order_id=order_id,
                idempotency_key=idempotency_key,
                existing_event_id=exc.existing_event_id,
                detail=exc.reason,
            )
            continue
        identity_error = skladbot_create_queue_identity_conflict_reason(
            event,
            idempotency_key=idempotency_key,
            order_id=order_id,
        )
        if identity_error:
            mark_skladbot_create_queue_identity_conflict(
                db,
                row,
                import_id=import_id,
                order_id=order_id,
                idempotency_key=idempotency_key,
                existing_event_id=str(event.id),
                detail=identity_error,
            )
            continue
        if order is not None:
            order_payload = dict(order.raw_payload or {})
            order_payload["skladbot_status"] = "create_queued"
            order_payload["skladbot_create_event_id"] = str(event.id)
            order_payload["skladbot_create_idempotency_key"] = idempotency_key
            order.raw_payload = order_payload
            flag_modified(order, "raw_payload")
        row["status"] = "queued"
        row["create_event_id"] = str(event.id)
        row["error"] = ""
        queued += 1
        db.add(AuditLog(
            action="skladbot_request_create_queued",
            entity_type="order",
            entity_id=order_id,
            payload={
                "import_id": import_id,
                "event_id": str(event.id),
                "idempotency_key": idempotency_key,
                "payload_hash": event.payload["request_payload_hash"],
            },
        ))
    return queued


def find_skladbot_create_event_by_key(
    db: Session,
    idempotency_key: str,
    *,
    order_id: str = "",
) -> PendingEvent | None:
    if not idempotency_key:
        return None
    canonical_order_id = normalize_text(order_id)
    legacy_v1_key = skladbot_create_idempotency_key(canonical_order_id)
    candidate_keys = [idempotency_key]
    if legacy_v1_key and legacy_v1_key not in candidate_keys:
        candidate_keys.append(legacy_v1_key)
    events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.idempotency_key.in_(candidate_keys))
        .order_by(PendingEvent.created_at, PendingEvent.id)
    ).scalars().all()
    events_by_key = {normalize_text(event.idempotency_key): event for event in events}
    for candidate_key in candidate_keys:
        event = events_by_key.get(candidate_key)
        if event is None:
            continue
        reason = skladbot_create_queue_identity_conflict_reason(
            event,
            idempotency_key=candidate_key,
            order_id=order_id,
        )
        if not reason and candidate_key == legacy_v1_key:
            reason = legacy_v1_create_event_marker_conflict_reason(event)
        if reason:
            raise OutboxIdentityConflict(event.id, reason)
        return event
    return None


def legacy_v1_create_event_marker_conflict_reason(event: PendingEvent) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    for field in ("taksklad_marker", "post_request_marker"):
        value = normalize_text(payload.get(field))
        if value and not taksklad_marker_from_comment(value):
            return f"legacy_{field}_invalid"
    return ""


def skladbot_create_queue_identity_conflict_reason(
    event: PendingEvent,
    *,
    idempotency_key: str,
    order_id: str,
) -> str:
    ownership_reason = skladbot_create_event_ownership_conflict_reason(
        event,
        expected_order_id=order_id,
    )
    if ownership_reason:
        return ownership_reason
    reason = outbox_event_identity_conflict_reason(
        event,
        event_type=SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
        action=SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
        aggregate_type="order",
        aggregate_id=normalize_text(order_id),
        idempotency_key=idempotency_key,
    )
    if reason:
        return reason
    payload = event.payload if isinstance(event.payload, dict) else {}
    if normalize_text(payload.get("idempotency_key")) != normalize_text(idempotency_key):
        return "payload_idempotency_key_mismatch"
    return ""


def skladbot_create_event_ownership_conflict_reason(
    event: PendingEvent,
    *,
    expected_order_id: Any = "",
) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    aggregate_order_id = parse_uuid(event.aggregate_id)
    payload_order_id = parse_uuid(payload.get("order_id"))
    expected_uuid = parse_uuid(expected_order_id) if normalize_text(expected_order_id) else payload_order_id
    if normalize_text(event.event_type) != SKLADBOT_REQUEST_CREATE_EVENT_TYPE:
        return "event_type_mismatch"
    if normalize_text(event.action) != SKLADBOT_REQUEST_CREATE_EVENT_TYPE:
        return "action_mismatch"
    if normalize_text(event.aggregate_type) != "order":
        return "aggregate_type_mismatch"
    if (
        aggregate_order_id is None
        or payload_order_id is None
        or expected_uuid is None
        or aggregate_order_id != payload_order_id
        or aggregate_order_id != expected_uuid
    ):
        return "order_id_mismatch"

    markers = set()
    for field in ("taksklad_marker", "post_request_marker"):
        raw_marker = normalize_text(payload.get(field))
        if not raw_marker:
            continue
        marker = taksklad_marker_from_comment(raw_marker)
        if not marker:
            return f"{field}_invalid"
        markers.add(marker)
    request_marker = request_payload_taksklad_marker(payload.get("request_payload"))
    if request_marker:
        markers.add(request_marker)
    if len(markers) > 1:
        return "marker_mismatch"

    order_id = str(expected_uuid)
    actual_key = normalize_text(event.idempotency_key)
    legacy_v1_key = skladbot_create_idempotency_key(order_id)
    allowed_keys = {legacy_v1_key}
    if markers:
        allowed_keys.add(skladbot_create_idempotency_key(order_id, marker=next(iter(markers))))
    if actual_key not in allowed_keys:
        return "idempotency_key_mismatch"
    return ""


def mark_skladbot_create_queue_identity_conflict(
    db: Session,
    row: dict[str, Any],
    *,
    import_id: str,
    order_id: str,
    idempotency_key: str,
    existing_event_id: str,
    detail: str,
) -> None:
    error = "SkladBot create queue identity conflict; manual review required"
    row["status"] = "blocked"
    row["error"] = error
    row.pop("create_event_id", None)
    order_uuid = parse_uuid(order_id)
    order = db.get(Order, order_uuid) if order_uuid is not None else None
    if order is not None:
        order_payload = dict(order.raw_payload or {})
        order_payload["skladbot_status"] = "manual_review"
        order_payload["skladbot_error"] = error
        order.raw_payload = order_payload
        flag_modified(order, "raw_payload")
    incident = db.execute(
        select(Incident)
        .where(Incident.source == "skladbot_create")
        .where(Incident.order_id == order_uuid)
        .where(Incident.title == "SkladBot create queue identity conflict")
    ).scalar_one_or_none()
    if incident is None:
        db.add(Incident(
            source="skladbot_create",
            severity="critical",
            status="manual_review",
            title="SkladBot create queue identity conflict",
            message=error,
            entity_type="order",
            entity_id=order_id,
            order_id=order_uuid,
            raw_payload={"reason": "event_identity_conflict"},
        ))
        db.add(AuditLog(
            action="skladbot_request_create_identity_conflict",
            entity_type="order",
            entity_id=order_id,
            payload={"reason": "event_identity_conflict"},
        ))
    else:
        incident.status = "manual_review"
        incident.severity = "critical"
        incident.message = error


def apply_existing_create_event_to_dry_run(row: dict[str, Any], event: PendingEvent) -> None:
    payload = event.payload or {}
    create_status = normalize_text(payload.get("create_status"))
    row["create_event_id"] = str(event.id)
    if event.status == "completed" and create_status in {"created", "created_recovered", "already_linked"}:
        if create_status == "created_recovered":
            row["status"] = "recovered"
        elif create_status == "already_linked":
            row["status"] = "already_linked"
        else:
            row["status"] = "created"
        row["error"] = ""
        return
    if event.status == "blocked":
        row["status"] = create_status if create_status in {"blocked_stock", "ambiguous"} else "blocked"
        row["error"] = normalize_text(event.last_error)
        return
    if event.status == "failed":
        row["status"] = "create_failed"
        row["error"] = normalize_text(event.last_error)
        return
    row["status"] = "queued"
    row["error"] = ""


def skladbot_create_idempotency_key(order_id: str, *, marker: str = "") -> str:
    normalized_marker = taksklad_marker_from_comment(marker)
    if normalized_marker:
        marker_id = normalized_marker.rsplit("-", 1)[-1]
        return f"skladbot:create:v2:marker:{marker_id}:order:{normalize_text(order_id)}"
    return f"skladbot:create:v1:order:{normalize_text(order_id)}"


def stable_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def request_payload_taksklad_marker(payload: dict[str, Any] | None) -> str:
    payload = payload if isinstance(payload, dict) else {}
    marker = taksklad_marker_from_comment(payload.get("comment"))
    if marker:
        return marker
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
    comment_field = fields.get("comment") if isinstance(fields.get("comment"), dict) else {}
    return taksklad_marker_from_comment(comment_field.get("value"))


def markerless_skladbot_comment(comment: Any) -> str:
    return "\n".join(
        line
        for line in normalize_text(comment).splitlines()
        if TAKSKLAD_MARKER_RE.fullmatch(line) is None
        and not is_legacy_generated_smartup_comment_line(line)
    ).strip()


def is_legacy_generated_smartup_comment_line(line: Any) -> bool:
    prefix = "Smartup ID: smartup:"
    normalized = str(line or "")
    if not normalized.startswith(prefix):
        return False
    smartup_id = normalized[len(prefix):]
    return bool(
        smartup_id
        and len(smartup_id) <= 40
        and smartup_id.isascii()
        and smartup_id.isdecimal()
        and smartup_id[0] != "0"
    )


def markerless_skladbot_request_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Remove legacy transport markers while keeping the public comments equal."""
    payload = dict(payload) if isinstance(payload, dict) else {}
    fields = dict(payload.get("fields")) if isinstance(payload.get("fields"), dict) else {}
    comment_field = dict(fields.get("comment")) if isinstance(fields.get("comment"), dict) else {}
    source_comment = payload.get("comment")
    if source_comment is None:
        source_comment = comment_field.get("value")
    comment = markerless_skladbot_comment(source_comment)
    payload["comment"] = comment
    comment_field["value"] = comment
    fields["comment"] = comment_field
    payload["fields"] = fields
    return payload


def remote_taksklad_marker_from_event_payload(payload: dict[str, Any] | None) -> str:
    """Return only a marker that may already have reached SkladBot.

    New events keep an internal deterministic marker for local idempotency, but
    markerless request payloads must never use it for remote reconciliation.
    Legacy events did not record ``post_request_marker``; for an already-started
    POST their stored request payload is the durable evidence that the marker
    was actually part of the request.
    """
    payload = payload if isinstance(payload, dict) else {}
    post_state = normalize_text(payload.get("post_state"))
    if post_state not in POST_OUTCOME_AMBIGUOUS_STATES:
        return ""
    explicit = taksklad_marker_from_comment(payload.get("post_request_marker"))
    if explicit:
        return explicit
    return request_payload_taksklad_marker(payload.get("request_payload"))


def legacy_attempt_taksklad_marker_from_event_payload(payload: dict[str, Any] | None) -> str:
    """Return marker evidence stored by an attempted pre-markerless event."""
    payload = payload if isinstance(payload, dict) else {}
    explicit = taksklad_marker_from_comment(payload.get("post_request_marker"))
    if explicit:
        return explicit
    return request_payload_taksklad_marker(payload.get("request_payload"))


def build_order_dry_run(
    order: Order,
    items: list[Any],
    import_id: str,
    index: int,
    *,
    representative_contact: Any | None = None,
) -> dict[str, Any]:
    raw_payload = order.raw_payload or {}
    linked_number = normalize_text(raw_payload.get("skladbot_request_number"))
    linked_id = normalize_text(raw_payload.get("skladbot_request_id"))
    try:
        sku_mapping = load_sku_mapping()
        sku_mapping_error = ""
    except ValueError as exc:
        sku_mapping = {}
        sku_mapping_error = str(exc)
    products = aggregate_skladbot_products([
        build_product_dry_run(item.product, item.quantity_blocks, sku_mapping=sku_mapping, sku_mapping_error=sku_mapping_error)
        for item in items
    ])
    blocks = sum(int(product.get("quantity_blocks") or 0) for product in products)
    linked_snapshot = linked_skladbot_amount_snapshot(raw_payload)
    status = "ready"
    error = ""

    if linked_number or linked_id:
        status = "already_linked"
        error = "У заказа уже есть номер или ID SkladBot"
        linked_blocks = int(linked_snapshot.get("blocks") or 0)
        if linked_snapshot and linked_blocks != blocks:
            status = "linked_mismatch"
            source = normalize_text(linked_snapshot.get("source")) or "linked SkladBot payload"
            error = (
                f"Расхождение с уже созданной SkladBot-заявкой: "
                f"в БД {blocks} блок., в SkladBot {linked_blocks} блок. ({source})"
            )
    else:
        blocked_errors = [product["error"] for product in products if product.get("status") == "blocked"]
        if blocked_errors:
            status = "blocked"
            error = "; ".join(blocked_errors)

    payload = {}
    if status == "ready":
        payload = build_skladbot_payload(order, products, representative_contact=representative_contact)

    return {
        "id": f"{import_id}:{index}",
        "import_id": import_id,
        "order_id": str(order.id),
        "client": order.client,
        "order_date": order.order_date.isoformat() if order.order_date else None,
        "payment_type": order.payment_type,
        "address": order.address,
        "blocks": blocks,
        "status": status,
        "error": error,
        "linked_skladbot_blocks": int(linked_snapshot.get("blocks") or 0),
        "linked_skladbot_source": normalize_text(linked_snapshot.get("source")),
        "products": products,
        "payload": payload,
    }


def linked_skladbot_amount_snapshot(raw_payload: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        ("skladbot_raw.detail.products", nested_value(raw_payload, "skladbot_raw", "detail", "products")),
        ("skladbot_raw.products", nested_value(raw_payload, "skladbot_raw", "products")),
        ("skladbot_create_request_payload.products", nested_value(raw_payload, "skladbot_create_request_payload", "products")),
    ]
    for source, products in candidates:
        amounts = product_amounts(products)
        if amounts:
            return {
                "source": source,
                "blocks": sum(amounts),
                "products": len(amounts),
                "amounts": amounts,
            }
    return {}


def nested_value(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def product_amounts(products: Any) -> list[int]:
    if not isinstance(products, list):
        return []
    amounts = []
    for product in products:
        if not isinstance(product, dict):
            continue
        for key in ("amount", "request_amount", "delivery_amount"):
            if key in product:
                amounts.append(parse_int(product.get(key)))
                break
    return amounts


def aggregate_skladbot_products(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aggregated: list[dict[str, Any]] = []
    index_by_key: dict[tuple[Any, str, bool], int] = {}
    for product in products:
        if product.get("status") != "ready":
            aggregated.append(product)
            continue

        key = (
            product.get("product_data_id"),
            normalize_text(product.get("barcode")),
            bool(product.get("is_main_barcode")),
        )
        source_product = {
            "product": product.get("product"),
            "quantity_blocks": int(product.get("quantity_blocks") or 0),
        }
        existing_index = index_by_key.get(key)
        if existing_index is None:
            row = dict(product)
            row["source_products"] = [source_product]
            index_by_key[key] = len(aggregated)
            aggregated.append(row)
            continue

        existing = aggregated[existing_index]
        existing["quantity_blocks"] = int(existing.get("quantity_blocks") or 0) + int(product.get("quantity_blocks") or 0)
        existing.setdefault("source_products", []).append(source_product)
    return aggregated


def build_product_dry_run(
    product: str,
    quantity_blocks: int,
    *,
    sku_mapping: dict[str, dict[str, Any]] | None = None,
    sku_mapping_error: str = "",
) -> dict[str, Any]:
    sku_key = product_sku_key(product)
    effective_mapping = sku_mapping
    effective_mapping_error = sku_mapping_error
    if effective_mapping is None:
        try:
            effective_mapping = load_sku_mapping()
        except ValueError as exc:
            effective_mapping = {}
            effective_mapping_error = str(exc)
    mapping = effective_mapping.get(sku_key)
    blocks = int(quantity_blocks or 0)
    if blocks <= 0:
        return {
            "product": product,
            "quantity_blocks": blocks,
            "product_data_id": None,
            "barcode": "",
            "is_main_barcode": False,
            "status": "blocked",
            "error": f"Некорректное количество блоков для {product}: {blocks}",
        }
    if effective_mapping_error:
        return {
            "product": product,
            "quantity_blocks": blocks,
            "product_data_id": None,
            "barcode": "",
            "is_main_barcode": False,
            "status": "blocked",
            "error": f"Ошибка настройки SKU mapping: {effective_mapping_error}",
        }
    if not mapping:
        return {
            "product": product,
            "quantity_blocks": blocks,
            "product_data_id": None,
            "barcode": "",
            "is_main_barcode": False,
            "status": "blocked",
            "error": f"SKU не найден в mapping: {product}",
        }
    return {
        "product": product,
        "quantity_blocks": blocks,
        "product_data_id": mapping["product_data_id"],
        "barcode": mapping["barcode"],
        "is_main_barcode": mapping["is_main_barcode"],
        "status": "ready",
        "error": "",
    }


def build_skladbot_payload(
    order: Order,
    products: list[dict[str, Any]],
    *,
    representative_contact: Any | None = None,
) -> dict[str, Any]:
    comment = build_representative_comment(order.payment_type, order.representative, representative_contact)
    return {
        "customer_id": SKLADBOT_CUSTOMER_ID,
        "request_type_id": SKLADBOT_REQUEST_TYPE_ID,
        "notify": True,
        "comment": comment,
        "fields": {
            "address": {"value": order.address},
            "comment": {"value": comment},
            "company_name": {"value": order.client},
            "unloading_date": {"value": order.order_date.isoformat() if order.order_date else ""},
        },
        "products": [
            {
                "product_data_id": product["product_data_id"],
                "barcode": product["barcode"],
                "is_main_barcode": product["is_main_barcode"],
                "amount": product["quantity_blocks"],
                "services": [],
                "packages": [],
                "comment": "",
            }
            for product in products
        ],
    }


def order_taksklad_marker(order: Order) -> str:
    raw_payload = order.raw_payload or {}
    workflow_reference = next(
        (
            normalize_text(raw_payload.get(key))
            for key in (
                "smartup_fulfillment_key",
                "fulfillment_key",
                "workflow_key",
                "source_identity_key",
            )
            if normalize_text(raw_payload.get(key))
        ),
        "",
    )
    reference = (
        f"{workflow_reference}:order:{order.id}"
        if workflow_reference
        else f"order:{order.id}"
    )
    return build_taksklad_marker(reference)


def summarize_dry_runs(dry_runs: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    summary = default_summary(mode=mode)
    summary["orders"] = len(dry_runs)
    for item in dry_runs:
        status = str(item.get("status") or "")
        if status in summary:
            summary[status] += 1
    if summary["linked_mismatch"]:
        summary["status"] = "mismatch"
    return summary


def default_summary(mode: str) -> dict[str, Any]:
    return {
        "status": "ok",
        "mode": mode,
        "orders": 0,
        "ready": 0,
        "blocked": 0,
        "already_linked": 0,
        "queued": 0,
        "created": 0,
        "recovered": 0,
        "create_failed": 0,
        "linked_mismatch": 0,
        "events_queued": 0,
        "event_id": "",
    }


def add_skladbot_dry_run_audit(
    db: Session,
    import_id: str,
    event_id: str,
    summary: dict[str, Any],
    dry_runs: list[dict[str, Any]],
) -> None:
    db.add(AuditLog(
        action="skladbot_request_dry_run_built",
        entity_type="import",
        entity_id=import_id,
        payload={
            "import_id": import_id,
            "event_id": event_id,
            "summary": summary,
            "orders": [
                {
                    "order_id": item.get("order_id"),
                    "status": item.get("status"),
                    "error": item.get("error"),
                    "payload_preview": item.get("payload") or {},
                }
                for item in dry_runs
            ],
        },
    ))


def process_pending_skladbot_request_creates(
    db: Session,
    client: Any | None = None,
    limit: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if skladbot_create_requests_mode() != "enabled":
        return default_create_processing_result(status="disabled")

    client = client or SkladBotClient(progress_callback=progress_callback)
    if not getattr(client, "configured", False):
        return default_create_processing_result(status="not_configured")

    limit = max(1, min(int(limit or env_int(SKLADBOT_REQUEST_CREATE_LIMIT_ENV, 20)), 100))
    leases_enabled = event_leases_enabled()
    reset_stale_skladbot_create_events(db)
    if leases_enabled:
        events = claim_event_leases(
            db,
            event_types=(SKLADBOT_REQUEST_CREATE_EVENT_TYPE,),
            owner=f"skladbot-create:{uuid.uuid4()}",
            limit=limit,
            now=datetime.now(timezone.utc),
        )
    else:
        events = None
    result = default_create_processing_result(status="completed")
    if leases_enabled and not events:
        result["remaining"] = count_pending_skladbot_create_events(db)
        return result

    claimed_event_ids: set[uuid.UUID] = set()
    for index in range(1, limit + 1):
        if leases_enabled:
            if index > len(events):
                break
            event = events[index - 1]
        else:
            event = claim_next_pending_event_without_lease(
                db,
                event_type=SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
                excluded_event_ids=claimed_event_ids,
            )
            if event is None:
                break
            claimed_event_ids.add(event.id)
        result["checked"] += 1

        with bind_pending_event(event):
            try:
                event_result = process_skladbot_create_event(db, event, client)
            except Exception as exc:
                event_result = {"status": "create_failed", "error": sanitize_skladbot_error(exc)}

            finish_skladbot_create_event(db, event, event_result, result)
        if progress_callback is not None:
            notify_skladbot_progress(progress_callback, f"create_events_processed:{index}")

    if result["failed"]:
        result["status"] = "completed_with_errors"
    result["remaining"] = count_pending_skladbot_create_events(db)
    return result


def claim_next_pending_event_without_lease(
    db: Session,
    *,
    event_type: str,
    excluded_event_ids: set[uuid.UUID] | None = None,
) -> PendingEvent | None:
    """Atomically claim one non-lease event before any external side effect."""
    now = datetime.now(timezone.utc)
    eligible = (
        select(PendingEvent)
        .where(PendingEvent.event_type == event_type)
        .where(PendingEvent.status.in_(("pending", "failed")))
        .where(PendingEvent.available_at <= now)
        .order_by(PendingEvent.available_at, PendingEvent.created_at, PendingEvent.id)
        .limit(1)
    )
    excluded_event_ids = set(excluded_event_ids or ())
    if excluded_event_ids:
        eligible = eligible.where(PendingEvent.id.not_in(excluded_event_ids))
    if db.bind.dialect.name == "postgresql":
        event = db.execute(
            eligible
            .with_for_update(skip_locked=True)
            .execution_options(
                populate_existing=True,
                taksklad_nonlease_claim=True,
            )
        ).scalars().one_or_none()
        if event is not None:
            event.status = "processing"
            event.attempts = int(event.attempts or 0) + 1
            event.completed_at = None
            event.lease_owner = None
            event.lease_expires_at = None
        db.commit()
        return event

    candidate_id = eligible.with_only_columns(PendingEvent.id).scalar_subquery()
    claim = (
        update(PendingEvent)
        .where(PendingEvent.id == candidate_id)
        .where(PendingEvent.event_type == event_type)
        .where(PendingEvent.status.in_(("pending", "failed")))
        .where(PendingEvent.available_at <= now)
        .values(
            status="processing",
            attempts=PendingEvent.attempts + 1,
            completed_at=None,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=now,
        )
        .returning(PendingEvent.id)
        .execution_options(
            synchronize_session=False,
            taksklad_nonlease_claim=True,
        )
    )
    event_id = db.execute(claim).scalar_one_or_none()
    if event_id is None:
        db.commit()
        return None
    db.commit()
    return db.get(PendingEvent, event_id, populate_existing=True)


def default_create_processing_result(status: str = "completed") -> dict[str, Any]:
    return {
        "status": status,
        "checked": 0,
        "created": 0,
        "recovered": 0,
        "already_linked": 0,
        "blocked": 0,
        "ambiguous": 0,
        "stock_shortage_blocked": 0,
        "stock_shortage_cancelled": 0,
        "failed": 0,
        "remaining": 0,
        "errors": [],
    }


def select_pending_skladbot_create_events(db: Session, limit: int) -> list[PendingEvent]:
    stmt = (
        select(PendingEvent)
        .where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
        .where(PendingEvent.status.in_(("pending", "failed")))
        .where(PendingEvent.available_at <= datetime.now(timezone.utc))
        .order_by(PendingEvent.created_at, PendingEvent.id)
        .limit(limit)
    )
    if db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    return db.execute(stmt).scalars().all()


def reset_stale_skladbot_create_events(db: Session) -> int:
    now = datetime.now(timezone.utc)
    cutoff = now - STALE_SKLADBOT_CREATE_TIMEOUT
    stmt = (
        select(PendingEvent)
        .where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
        .where(PendingEvent.status == "processing")
        .where(PendingEvent.updated_at < cutoff)
        .where((PendingEvent.lease_owner.is_(None)) | (PendingEvent.lease_expires_at <= now))
        .order_by(PendingEvent.updated_at, PendingEvent.id)
        .limit(200)
        .execution_options(taksklad_stale_reset_candidate=True)
    )
    if db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    events = db.execute(stmt).scalars().all()
    if not events:
        return 0
    reset_events = []
    for event in events:
        reset_id = db.execute(
            update(PendingEvent)
            .where(PendingEvent.id == event.id)
            .where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
            .where(PendingEvent.status == "processing")
            .where(PendingEvent.updated_at < cutoff)
            .where((PendingEvent.lease_owner.is_(None)) | (PendingEvent.lease_expires_at <= now))
            .values(
                status="pending",
                available_at=now,
                lease_owner=None,
                lease_expires_at=None,
                completed_at=None,
                last_error="stale SkladBot create event reset",
                updated_at=now,
            )
            .returning(PendingEvent.id)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()
        if reset_id is None:
            continue
        event = db.get(PendingEvent, reset_id, populate_existing=True)
        event.payload = {**(event.payload or {}), "reset_at": now.isoformat()}
        reset_events.append(event)
        db.add(AuditLog(
            action="skladbot_request_create_stale_reset",
            entity_type="pending_event",
            entity_id=str(event.id),
            payload={
                "order_id": (event.payload or {}).get("order_id") or "",
                "idempotency_key": event.idempotency_key or "",
            },
        ))
    db.commit()
    return len(reset_events)


def count_pending_skladbot_create_events(db: Session) -> int:
    return int(db.execute(
        select(func.count(PendingEvent.id))
        .where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
        .where(PendingEvent.status.in_(("pending", "failed")))
    ).scalar_one() or 0)


def classify_skladbot_create_exception(error: Exception) -> str:
    if isinstance(error, SkladBotApiError):
        if error.kind == SkladBotErrorKind.STOCK_SHORTAGE and not error.ambiguous:
            return "stock_shortage"
        if error.kind == SkladBotErrorKind.RATE_LIMIT and not error.ambiguous:
            return "rate_limited"
        return "ambiguous" if error.ambiguous else "failed"
    if isinstance(error, (TimeoutError, ConnectionError)):
        return "ambiguous"
    text = normalize_text(error)
    lowered = text.casefold()
    if "timeout" in lowered or "timed out" in lowered or "connection reset" in lowered:
        return "ambiguous"
    if any(f"http {status}" in lowered for status in range(500, 600)):
        return "ambiguous"
    if is_stock_shortage_text(text) and any(f"http {status}" in lowered for status in range(400, 500)):
        return "stock_shortage"
    return "failed"


def order_import_job(db: Session, order: Order, event: PendingEvent) -> ImportJob | None:
    import_uuid = parse_uuid((event.payload or {}).get("import_id"))
    if import_uuid is None:
        for item in order.items or []:
            import_uuid = parse_uuid((item.raw_payload or {}).get("backend_import_id"))
            if import_uuid is not None:
                break
    if import_uuid is None:
        return None
    return db.get(ImportJob, import_uuid)


def format_order_date_for_message(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%d.%m.%Y")
    return normalize_text(value)


def first_order_source_file(order: Order) -> str:
    for item in order.items or []:
        source_file = normalize_text((item.raw_payload or {}).get("source_file"))
        if source_file:
            return source_file
    return ""


def build_stock_shortage_notification_text(order: Order, error: str) -> str:
    lines = [
        "Заказ заблокирован из-за недостатка товара",
        "",
        f"Клиент: {order.client}",
        f"Дата отгрузки: {format_order_date_for_message(order.order_date) or 'не указана'}",
        f"Тип оплаты: {order.payment_type}",
        f"Адрес: {order.address}",
    ]
    source_file = first_order_source_file(order)
    if source_file:
        lines.append(f"Файл: {source_file}")
    lines.extend([
        "",
        "Позиции:",
        *[
            f"- {item.product}: {int(item.quantity_blocks or 0)} блок."
            for item in order.items or []
        ],
        "",
        f"Причина SkladBot: {normalize_text(error)}",
        "",
        "SkladBot заявку не создал. Заказ не удалён; нужна ручная проверка.",
    ])
    return "\n".join(lines)


def queue_stock_shortage_notification(
    db: Session,
    order: Order,
    event: PendingEvent,
    import_job: ImportJob | None,
    error: str,
) -> PendingEvent:
    idempotency_key = f"telegram:notification:v1:skladbot_stock_shortage:{event.id}"
    existing = db.execute(
        select(PendingEvent).where(PendingEvent.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    notification = PendingEvent(
        event_type=TELEGRAM_NOTIFICATION_EVENT_TYPE,
        status="pending",
        idempotency_key=idempotency_key,
        payload={
            "kind": "skladbot_stock_shortage_blocked_order",
            "order_id": str(order.id),
            "import_id": str(import_job.id) if import_job else normalize_text((event.payload or {}).get("import_id")),
            "text": build_stock_shortage_notification_text(order, error),
            "error": normalize_text(error),
        },
    )
    db.add(notification)
    db.flush()
    return notification


def ensure_skladbot_create_incident(
    db: Session,
    order: Order,
    event: PendingEvent,
    error: str,
    *,
    status: str = "open",
) -> Incident:
    existing = db.execute(
        select(Incident).where(Incident.pending_event_id == event.id).where(Incident.source == "skladbot_create")
    ).scalar_one_or_none()
    import_job = order_import_job(db, order, event)
    products = [
        {
            "item_id": str(item.id),
            "product": item.product,
            "sku": product_sku_key(item.product),
            "quantity_blocks": int(item.quantity_blocks or 0),
            "source_file": normalize_text((item.raw_payload or {}).get("source_file")),
        }
        for item in order.items or []
    ]
    raw_payload = {
        "order_id": str(order.id),
        "import_id": str(import_job.id) if import_job else normalize_text((event.payload or {}).get("import_id")),
        "source_file": first_order_source_file(order),
        "client": order.client,
        "order_date": order.order_date.isoformat() if order.order_date else "",
        "payment_type": order.payment_type,
        "skladbot_event_id": str(event.id),
        "skladbot_create_status": status,
        "error": normalize_text(error),
        "products": products,
    }
    if existing is not None:
        existing.status = status
        existing.severity = "critical"
        existing.message = normalize_text(error)
        existing.raw_payload = {**(existing.raw_payload or {}), **raw_payload}
        return existing

    incident = Incident(
        source="skladbot_create",
        severity="critical",
        status=status,
        title="SkladBot request create failed",
        message=normalize_text(error),
        entity_type="order",
        entity_id=str(order.id),
        pending_event_id=event.id,
        order_id=order.id,
        import_id=import_job.id if import_job else parse_uuid((event.payload or {}).get("import_id")),
        external_ref=first_order_source_file(order),
        raw_payload=raw_payload,
    )
    db.add(incident)
    db.add(AuditLog(
        action="skladbot_create_incident_created",
        entity_type="order",
        entity_id=str(order.id),
        payload={
            "incident_source": incident.source,
            "status": status,
            "event_id": str(event.id),
            "import_id": raw_payload["import_id"],
            "source_file": raw_payload["source_file"],
            "error": normalize_text(error),
        },
    ))
    db.flush()
    return incident


def block_order_after_skladbot_stock_shortage(
    db: Session,
    order: Order,
    event: PendingEvent,
    error: str,
) -> dict[str, Any]:
    import_job = order_import_job(db, order, event)
    mark_order_skladbot_create_blocked(order, event, error, status="blocked_stock")
    incident = ensure_skladbot_create_incident(db, order, event, error, status="manual_review")
    notification_event = queue_stock_shortage_notification(db, order, event, import_job, error)
    update_event_payload(event, {
        "create_status": "blocked_stock",
        "error": normalize_text(error),
        "stock_shortage_blocked_at": datetime.now(timezone.utc).isoformat(),
        "telegram_notification_event_id": str(notification_event.id) if notification_event else "",
    })
    transition_linked_fulfillment(db, event, "blocked_stock", error=error)
    order_id = str(order.id)
    db.add(AuditLog(
        action="skladbot_stock_shortage_order_blocked",
        entity_type="order",
        entity_id=order_id,
        payload={
            "order_id": order_id,
            "import_id": str(import_job.id) if import_job else normalize_text((event.payload or {}).get("import_id")),
            "error": normalize_text(error),
            "telegram_notification_event_id": str(notification_event.id) if notification_event else "",
            "incident_id": str(incident.id) if incident.id else "",
        },
    ))
    return {
        "status": "blocked_stock",
        "order_id": order_id,
        "error": normalize_text(error),
        "telegram_notification_event_id": str(notification_event.id) if notification_event else "",
        "incident_id": str(incident.id) if incident.id else "",
    }


def cancel_unscanned_order_after_skladbot_stock_shortage(
    db: Session,
    order: Order,
    event: PendingEvent,
    error: str,
) -> dict[str, Any]:
    """Legacy name retained; stock shortage is now non-destructive."""
    return block_order_after_skladbot_stock_shortage(db, order, event, error)


def skladbot_create_event_ownership_is_valid(event: PendingEvent, payload: dict[str, Any]) -> bool:
    return not skladbot_create_event_ownership_conflict_reason(event)


def mark_skladbot_create_event_ownership_invalid(
    db: Session,
    event: PendingEvent,
    payload: dict[str, Any],
) -> dict[str, Any]:
    error = "SkladBot create event ownership mismatch; manual review required"
    order_uuid = parse_uuid(payload.get("order_id"))
    update_event_payload(event, {
        "create_status": "ambiguous",
        "post_state": "ambiguous",
        "error": error,
        "ownership_validation": "failed",
    })
    if not event.lease_owner:
        event.status = "blocked"
        event.last_error = error
    existing = db.execute(
        select(Incident)
        .where(Incident.pending_event_id == event.id)
        .where(Incident.source == "skladbot_create")
        .where(Incident.title == "Invalid SkladBot create event ownership")
    ).scalar_one_or_none()
    if existing is None:
        db.add(Incident(
            source="skladbot_create",
            severity="critical",
            status="manual_review",
            title="Invalid SkladBot create event ownership",
            message=error,
            entity_type="pending_event",
            entity_id=str(event.id),
            pending_event_id=event.id,
            order_id=order_uuid,
            raw_payload={
                "event_id": str(event.id),
                "event_type": normalize_text(event.event_type),
                "aggregate_type": normalize_text(event.aggregate_type),
                "aggregate_id": normalize_text(event.aggregate_id),
                "payload_order_id": normalize_text(payload.get("order_id")),
                "reason": "ownership_mismatch",
            },
        ))
        db.add(AuditLog(
            action="skladbot_create_event_ownership_invalid",
            entity_type="pending_event",
            entity_id=str(event.id),
            payload={
                "aggregate_id": normalize_text(event.aggregate_id),
                "payload_order_id": normalize_text(payload.get("order_id")),
                "reason": "ownership_mismatch",
            },
        ))
    else:
        existing.status = "manual_review"
        existing.severity = "critical"
        existing.message = error
    return {
        "status": "ambiguous",
        "error": error,
        "order_id": str(order_uuid) if order_uuid else "",
    }


def process_skladbot_create_event(db: Session, event: PendingEvent, client: Any) -> dict[str, Any]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    if not skladbot_create_event_ownership_is_valid(event, payload):
        return mark_skladbot_create_event_ownership_invalid(db, event, payload)
    post_state = normalize_text(payload.get("post_state"))
    create_status = normalize_text(payload.get("create_status"))
    remote_marker = remote_taksklad_marker_from_event_payload(payload)
    attempted_without_explicit_retry = int(event.attempts or 0) > 1 and post_state != "retry_scheduled"
    if attempted_without_explicit_retry and not remote_marker:
        remote_marker = legacy_attempt_taksklad_marker_from_event_payload(payload)
    order_uuid = parse_uuid(payload.get("order_id"))
    if order_uuid is None:
        return {"status": "create_failed", "error": "invalid order id"}

    order = db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(Order.id == order_uuid)
    ).scalars().unique().one_or_none()
    if order is None:
        return {"status": "create_failed", "error": "order not found"}

    raw_payload = dict(order.raw_payload or {})
    existing_id_raw = normalize_text(raw_payload.get("skladbot_request_id"))
    existing_number_raw = normalize_text(raw_payload.get("skladbot_request_number"))
    existing_id = canonical_remote_request_id(existing_id_raw)
    existing_number = canonical_skladbot_request_number(existing_number_raw)
    durable_response_id_raw = normalize_text(payload.get("post_response_request_id"))
    durable_response_id = canonical_remote_request_id(durable_response_id_raw)
    if existing_id and existing_number:
        if durable_response_id_raw and durable_response_id != existing_id:
            return mark_skladbot_create_ambiguous(
                db,
                order,
                event,
                "Existing SkladBot link conflicts with durable response request id",
            )
        update_event_payload(event, {
            "create_status": "already_linked",
            "created_request_id": existing_id,
            "created_request_number": existing_number,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        transition_linked_fulfillment(
            db,
            event,
            "skladbot_created",
            remote_request_id=existing_id or existing_number,
        )
        return {
            "status": "already_linked",
            "request_id": existing_id,
            "request_number": existing_number,
            "order_id": str(order.id),
        }
    if existing_id_raw or existing_number_raw:
        if (
            existing_id
            and not existing_number_raw
            and durable_response_id == existing_id
        ):
            existing_request = reconcile_ambiguous_skladbot_request(order, event, client, "")
            if existing_request:
                request_payload = (
                    payload.get("request_payload")
                    if isinstance(payload.get("request_payload"), dict)
                    else {}
                )
                return save_skladbot_create_result(
                    db,
                    order,
                    event,
                    request_payload,
                    existing_request,
                    status="created_recovered",
                )
        return mark_skladbot_create_ambiguous(
            db,
            order,
            event,
            "Existing SkladBot link is incomplete or conflicts with durable evidence",
        )

    dry_run = build_order_dry_run(
        order,
        list(order.items),
        str(payload.get("import_id") or ""),
        1,
        representative_contact=find_representative_contact(db, order.representative),
    )
    if dry_run.get("status") != "ready":
        error = normalize_text(dry_run.get("error")) or "SkladBot payload is blocked"
        mark_order_skladbot_create_failed(db, order, event, error)
        return {"status": "blocked", "error": error, "order_id": str(order.id)}

    request_payload = dry_run.get("payload") or {}
    request_payload_hash = stable_payload_hash(request_payload)
    event_marker = normalize_text(payload.get("taksklad_marker"))
    taksklad_marker = event_marker or order_taksklad_marker(order)
    update_event_payload(event, {
        "request_payload": request_payload,
        "request_payload_hash": request_payload_hash,
        "taksklad_marker": taksklad_marker,
    })

    if (
        post_state in POST_OUTCOME_AMBIGUOUS_STATES
        or create_status == "ambiguous"
        or attempted_without_explicit_retry
    ):
        existing_request = reconcile_ambiguous_skladbot_request(order, event, client, remote_marker)
        if existing_request:
            return save_skladbot_create_result(
                db,
                order,
                event,
                request_payload,
                existing_request,
                status="created_recovered",
            )
        error = normalize_text(payload.get("error")) or (
            "SkladBot POST result is ambiguous; exact remote request was not confirmed"
        )
        return mark_skladbot_create_ambiguous(db, order, event, error)

    request_marker = request_payload_taksklad_marker(request_payload)
    update_event_payload(event, {
        "post_state": "started",
        "post_started_at": datetime.now(timezone.utc).isoformat(),
        "post_request_marker": request_marker,
    })
    transition_linked_fulfillment(db, event, "skladbot_post_started")
    db.commit()

    try:
        response = client.create_request(request_payload)
    except Exception as exc:
        classification = classify_skladbot_create_exception(exc)
        error = sanitize_skladbot_error(exc)
        if classification == "stock_shortage":
            update_event_payload(event, {"post_state": "rejected"})
            return block_order_after_skladbot_stock_shortage(db, order, event, error)
        if classification == "ambiguous":
            update_event_payload(event, {"post_state": "ambiguous"})
            existing_request = reconcile_ambiguous_skladbot_request(order, event, client, request_marker)
            if existing_request:
                return save_skladbot_create_result(
                    db,
                    order,
                    event,
                    request_payload,
                    existing_request,
                    status="created_recovered",
                )
            return mark_skladbot_create_ambiguous(db, order, event, error)
        if classification == "rate_limited":
            retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            event.available_at = retry_at
            raw_payload = dict(order.raw_payload or {})
            raw_payload["skladbot_status"] = "create_queued"
            raw_payload["skladbot_error"] = error
            order.raw_payload = raw_payload
            flag_modified(order, "raw_payload")
            update_event_payload(event, {
                "post_state": "retry_scheduled",
                "create_status": "queued",
                "retry_at": retry_at.isoformat(),
            })
            transition_linked_fulfillment(db, event, "skladbot_create_queued", error=error)
            return {"status": "retry_scheduled", "error": error, "order_id": str(order.id)}
        update_event_payload(event, {"post_state": "failed_confirmed"})
        mark_order_skladbot_create_failed(db, order, event, error)
        ensure_skladbot_create_incident(db, order, event, error, status="open")
        return {"status": "blocked", "error": error, "order_id": str(order.id)}

    response_request = normalize_created_request_response(response)
    request_id_text = canonical_remote_request_id(response_request.get("id"))
    if not request_id_text:
        error = "SkladBot create response did not include request id"
        update_event_payload(event, {"post_state": "ambiguous"})
        return mark_skladbot_create_ambiguous(db, order, event, error)
    request_id = int(request_id_text)

    update_event_payload(event, {
        "post_state": "response_received",
        "post_response_request_id": request_id,
    })

    try:
        detail = client.get_request_detail(request_id)
    except Exception as exc:
        error = f"SkladBot created request {request_id}, but canonical detail failed: {sanitize_skladbot_error(exc)}"
        update_event_payload(event, {"post_state": "ambiguous"})
        return mark_skladbot_create_ambiguous(db, order, event, error)

    request = normalize_request_payload({"id": request_id}, detail)
    canonical_request_id, request_number = canonical_skladbot_request_link(
        detail.get("id") if isinstance(detail, dict) else "",
        request.get("number"),
    )
    if canonical_request_id != request_id_text:
        error = (
            f"SkladBot canonical detail ID mismatch: expected {request_id}, got {canonical_request_id or '<empty>'}"
        )
        update_event_payload(event, {"post_state": "ambiguous"})
        return mark_skladbot_create_ambiguous(db, order, event, error)
    if not request_number:
        error = f"SkladBot created request {request_id}, but canonical WH-R/WR is empty"
        update_event_payload(event, {"post_state": "ambiguous"})
        return mark_skladbot_create_ambiguous(db, order, event, error)

    return save_skladbot_create_result(
        db,
        order,
        event,
        request_payload,
        request,
        status="created",
        response=response,
    )


def normalize_created_request_response(response: Any) -> dict[str, Any]:
    data = response.get("data") if isinstance(response, dict) else {}
    if not isinstance(data, dict):
        data = response if isinstance(response, dict) else {}
    return {
        "id": canonical_remote_request_id(data.get("id")),
        "number": canonical_skladbot_request_number(data.get("delivery_number") or data.get("number")),
        "created_at": normalize_text(data.get("created_at") or data.get("createdAt")),
        "raw": {"response": data},
    }


def reconcile_ambiguous_skladbot_request(
    order: Order,
    event: PendingEvent,
    client: Any,
    marker: str,
) -> dict[str, Any] | None:
    request_id_text = canonical_remote_request_id((event.payload or {}).get("post_response_request_id"))
    if request_id_text:
        request_id = int(request_id_text)
        try:
            detail = client.get_request_detail(request_id)
        except Exception:
            detail = None
        if detail:
            request = normalize_request_payload({"id": request_id}, detail)
            canonical_request_id, request_number = canonical_skladbot_request_link(
                detail.get("id") if isinstance(detail, dict) else "",
                request.get("number"),
            )
            if (
                canonical_request_id == request_id_text
                and request_number
            ):
                return request
        return None
    exact_marker = taksklad_marker_from_comment(marker)
    if not exact_marker:
        return None
    return find_existing_skladbot_request_for_order(order, client, marker=exact_marker)


def find_existing_skladbot_request_for_order(
    order: Order,
    client: Any,
    *,
    marker: str = "",
) -> dict[str, Any] | None:
    exact_marker = taksklad_marker_from_comment(marker)
    if not exact_marker:
        return None
    try:
        list_items = client.list_requests()
    except Exception:
        return None
    for item in list_items:
        request_id_text = canonical_remote_request_id(request_list_value(item, "id"))
        if not request_id_text:
            continue
        request_id = int(request_id_text)
        try:
            detail = client.get_request_detail(request_id)
        except Exception:
            continue
        detail_request_id = canonical_remote_request_id(detail.get("id")) if isinstance(detail, dict) else ""
        if detail_request_id != request_id_text:
            continue
        request = normalize_request_payload(item, detail)
        canonical_request_id, request_number = canonical_skladbot_request_link(
            detail_request_id,
            request.get("number"),
        )
        if (
            canonical_request_id == request_id_text
            and request_number
            and request_has_exact_taksklad_marker(request, exact_marker)
        ):
            return request
    return None


def save_skladbot_create_result(
    db: Session,
    order: Order,
    event: PendingEvent,
    request_payload: dict[str, Any],
    request: dict[str, Any],
    status: str,
    response: Any | None = None,
) -> dict[str, Any]:
    checked_at = datetime.now(timezone.utc).isoformat()
    request_id, request_number = canonical_skladbot_request_link(
        request.get("id"),
        request.get("number"),
    )
    if not request_id or not request_number:
        error = "Exact SkladBot match is incomplete: canonical request id and WH-R are required"
        return mark_skladbot_create_ambiguous(db, order, event, error)
    raw_payload = dict(order.raw_payload or {})
    raw_payload.update({
        "skladbot_request_id": request_id,
        "skladbot_request_number": request_number,
        "skladbot_status": status,
        "skladbot_checked_at": checked_at,
        "skladbot_created_at": checked_at,
        "skladbot_created_by_taksklad": True,
        "skladbot_create_idempotency_key": event.idempotency_key or "",
        "skladbot_create_payload_hash": stable_payload_hash(request_payload),
        "skladbot_create_event_id": str(event.id),
        "skladbot_create_request_payload": request_payload,
        "skladbot_create_response": safe_skladbot_response_summary(response),
        "skladbot_raw": request.get("raw") or {},
    })
    raw_payload.pop("skladbot_error", None)
    order.raw_payload = raw_payload
    flag_modified(order, "raw_payload")
    db.add(order)
    update_event_payload(event, {
        "create_status": status,
        "post_state": "completed",
        "created_request_id": request_id,
        "created_request_number": request_number,
        "completed_at": checked_at,
        "response_summary": safe_skladbot_response_summary(response),
    })
    transition_linked_fulfillment(
        db,
        event,
        "skladbot_created",
        remote_request_id=request_id,
    )
    for incident in db.execute(
        select(Incident)
        .where(Incident.pending_event_id == event.id)
        .where(Incident.source == "skladbot_create")
    ).scalars().all():
        incident.status = "resolved"
        incident.resolved_at = datetime.now(timezone.utc)
    return {
        "status": status,
        "request_id": request_id,
        "request_number": request_number,
        "order_id": str(order.id),
    }


def mark_order_skladbot_create_failed(db: Session, order: Order, event: PendingEvent, error: str) -> None:
    raw_payload = dict(order.raw_payload or {})
    raw_payload["skladbot_status"] = "create_failed"
    raw_payload["skladbot_checked_at"] = datetime.now(timezone.utc).isoformat()
    raw_payload["skladbot_error"] = normalize_text(error)
    raw_payload["skladbot_create_event_id"] = str(event.id)
    raw_payload["skladbot_create_idempotency_key"] = event.idempotency_key or ""
    order.raw_payload = raw_payload
    flag_modified(order, "raw_payload")
    update_event_payload(event, {
        "create_status": "create_failed",
        "error": normalize_text(error),
    })
    transition_linked_fulfillment(db, event, "manual_review", error=error)


def mark_order_skladbot_create_blocked(
    order: Order,
    event: PendingEvent,
    error: str,
    *,
    status: str,
) -> None:
    raw_payload = dict(order.raw_payload or {})
    raw_payload["skladbot_status"] = status
    raw_payload["skladbot_checked_at"] = datetime.now(timezone.utc).isoformat()
    raw_payload["skladbot_error"] = normalize_text(error)
    raw_payload["skladbot_create_event_id"] = str(event.id)
    raw_payload["skladbot_create_idempotency_key"] = event.idempotency_key or ""
    order.raw_payload = raw_payload
    flag_modified(order, "raw_payload")
    update_event_payload(event, {
        "create_status": status,
        "error": normalize_text(error),
    })


def transition_linked_fulfillment(
    db: Session,
    event: PendingEvent,
    target_state: str,
    *,
    error: str = "",
    remote_request_id: str = "",
) -> None:
    link = db.execute(
        select(SmartupFulfillmentOrder).where(SmartupFulfillmentOrder.skladbot_event_id == event.id)
    ).scalar_one_or_none()
    if link is None:
        return
    fulfillment = db.get(SmartupFulfillment, link.fulfillment_id)
    if fulfillment is None:
        return
    from .smartup_saga import link_fulfillment_order_event, transition_fulfillment_order

    state_map = {
        "skladbot_create_queued": "create_queued",
        "skladbot_post_started": "post_started",
        "skladbot_created": "created",
        "skladbot_ambiguous": "ambiguous",
        "blocked_stock": "blocked_stock",
        "manual_review": "manual_review",
    }
    link_fulfillment_order_event(
        db,
        fulfillment,
        link.order_id,
        create_event=event,
        remote_request_id=remote_request_id or None,
    )
    transition_fulfillment_order(
        db,
        link,
        state_map[target_state],
        error=error,
        remote_request_id=remote_request_id,
    )


def mark_skladbot_create_ambiguous(
    db: Session,
    order: Order,
    event: PendingEvent,
    error: str,
) -> dict[str, Any]:
    mark_order_skladbot_create_blocked(order, event, error, status="ambiguous")
    update_event_payload(event, {
        "post_state": "ambiguous",
        "ambiguous_at": datetime.now(timezone.utc).isoformat(),
    })
    transition_linked_fulfillment(db, event, "skladbot_ambiguous", error=error)
    ensure_skladbot_create_incident(db, order, event, error, status="manual_review")
    return {"status": "ambiguous", "error": normalize_text(error), "order_id": str(order.id)}


def finish_skladbot_create_event(
    db: Session,
    event: PendingEvent,
    event_result: dict[str, Any],
    result: dict[str, Any],
) -> None:
    status = normalize_text(event_result.get("status"))
    event_payload = {**(event.payload or {}), "last_result": event_result}
    final_status = "failed"
    final_error = ""
    if status in {"created", "created_recovered", "already_linked"}:
        final_status = "completed"
        if status == "created":
            result["created"] += 1
        elif status == "created_recovered":
            result["recovered"] += 1
        else:
            result["already_linked"] += 1
    elif status in {"blocked", "blocked_stock", "ambiguous"}:
        final_status = "blocked"
        final_error = normalize_text(event_result.get("error"))
        result["blocked"] += 1
        if status == "blocked_stock":
            result["stock_shortage_blocked"] += 1
        elif status == "ambiguous":
            result["ambiguous"] += 1
    elif status == "retry_scheduled":
        final_status = "pending"
        final_error = normalize_text(event_result.get("error"))
    elif status == "cancelled_stock_shortage":
        final_status = "completed"
        result["stock_shortage_cancelled"] += 1
    else:
        final_error = normalize_text(event_result.get("error")) or "SkladBot request create failed"
        result["failed"] += 1
        result["errors"].append({
            "event_id": str(event.id),
            "order_id": (event.payload or {}).get("order_id") or "",
            "error": final_error,
        })
    db.add(AuditLog(
        action="skladbot_request_create_processed",
        entity_type="pending_event",
        entity_id=str(event.id),
        payload={
            "order_id": (event.payload or {}).get("order_id") or "",
            "status": status,
            "request_id": event_result.get("request_id") or "",
            "request_number": event_result.get("request_number") or "",
            "error": normalize_text(event_result.get("error")),
        },
    ))
    if event.lease_owner:
        finalize_event_leases(
            db,
            event_ids=(event.id,),
            owner=event.lease_owner,
            status=final_status,
            last_error=final_error,
            payload=event_payload,
            available_at=event.available_at if status == "retry_scheduled" else None,
        )
    else:
        event.payload = event_payload
        event.status = final_status
        event.last_error = final_error
        event.completed_at = datetime.now(timezone.utc) if final_status in {"completed", "blocked"} else None
        db.commit()


def update_event_payload(event: PendingEvent, values: dict[str, Any]) -> None:
    event.payload = {**(event.payload or {}), **values}


def safe_skladbot_response_summary(response: Any | None) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    data = response.get("data") if isinstance(response.get("data"), dict) else response
    if not isinstance(data, dict):
        return {}
    return {
        "id": data.get("id"),
        "delivery_number": data.get("delivery_number") or data.get("number"),
        "created_at": data.get("created_at") or data.get("createdAt"),
        "customer_id": data.get("customer_id"),
        "request_type_id": data.get("request_type_id"),
    }


def parse_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
