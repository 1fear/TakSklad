import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .google_sheets_exporter import make_sheet_record
from .google_sheets_pending import queue_google_sheets_export
from .models import AuditLog, ImportJob, Order, OrderItem, PendingEvent
from .skladbot_worker import (
    SkladBotClient,
    env_int,
    normalize_request_payload,
    normalize_text,
    parse_int,
    product_sku_key,
    request_list_value,
    request_matches_order,
    sanitize_skladbot_error,
)


SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE = "skladbot_request_dry_run"
SKLADBOT_REQUEST_CREATE_EVENT_TYPE = "skladbot_request_create"
SKLADBOT_CREATE_REQUESTS_MODE_ENV = "SKLADBOT_CREATE_REQUESTS_MODE"
SKLADBOT_CREATE_REQUESTS_DEFAULT_MODE = "dry_run"
SKLADBOT_CUSTOMER_ID = 6211
SKLADBOT_REQUEST_TYPE_ID = 3389
SKLADBOT_REQUEST_CREATE_LIMIT_ENV = "SKLADBOT_REQUEST_CREATE_LIMIT"
STALE_SKLADBOT_CREATE_TIMEOUT = timedelta(minutes=10)
GOOGLE_DELETE_IMPORT_RECORDS_ACTION = "google_sheets_delete_import_records_export"
TELEGRAM_NOTIFICATION_EVENT_TYPE = "telegram_notification"

SKU_MAPPING = {
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
    "gold:ssl": {
        "product_data_id": 2189394,
        "barcode": "4006396054005",
        "is_main_barcode": False,
    },
}


def skladbot_create_requests_mode(environ: dict[str, str] | None = None) -> str:
    environ = environ or os.environ
    mode = normalize_text(environ.get(SKLADBOT_CREATE_REQUESTS_MODE_ENV)).lower()
    if mode in {"dry_run", "enabled", "disabled"}:
        return mode
    return SKLADBOT_CREATE_REQUESTS_DEFAULT_MODE


def create_skladbot_dry_run_for_import(db: Session, import_id: str, rebuild: bool = False) -> dict[str, Any]:
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
                existing_event.payload = {**(existing_event.payload or {}), "mode": "enabled", "would_post": True, "summary": summary, "dry_runs": dry_runs}
                db.add(existing_event)
        return {
            **default_summary(mode=summary.get("mode") or mode),
            **summary,
            "status": "deduplicated",
            "event_id": str(existing_event.id),
        }

    orders = list_orders_for_import(db, import_id)
    dry_runs = [
        build_order_dry_run(order, items, import_id, index)
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
            status="completed",
            attempts=0,
            payload=event_payload,
            last_error=None,
        )
        db.add(event)
        db.flush()
    else:
        event = existing_event
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


def list_skladbot_dry_runs(db: Session, import_id: str | None = None) -> list[dict[str, Any]]:
    events = list_skladbot_dry_run_events(db, import_id)
    result = []
    for event in events:
        payload = event.payload or {}
        generated_at = payload.get("generated_at") or None
        for row in payload.get("dry_runs") or []:
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
                "products": row.get("products") or [],
                "payload": row.get("payload") or {},
                "generated_at": generated_at,
            })
    return result


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
    stmt = select(Order).options(selectinload(Order.items)).order_by(Order.created_at.asc(), Order.client.asc())
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


def find_skladbot_dry_run_event(db: Session, import_id: str) -> PendingEvent | None:
    events = list_skladbot_dry_run_events(db, import_id)
    return events[0] if events else None


def list_skladbot_dry_run_events(db: Session, import_id: str | None = None) -> list[PendingEvent]:
    stmt = (
        select(PendingEvent)
        .where(PendingEvent.event_type == SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE)
        .order_by(PendingEvent.created_at.desc(), PendingEvent.id.desc())
    )
    events = db.execute(stmt).scalars().all()
    if not import_id:
        return events
    return [
        event
        for event in events
        if str((event.payload or {}).get("import_id") or "") == import_id
    ]


def queue_skladbot_create_events(db: Session, import_id: str, dry_runs: list[dict[str, Any]]) -> int:
    queued = 0
    for row in dry_runs:
        if str(row.get("status") or "") != "ready":
            continue
        order_id = normalize_text(row.get("order_id"))
        payload = row.get("payload") or {}
        if not order_id or not payload:
            continue
        idempotency_key = skladbot_create_idempotency_key(order_id)
        existing = find_skladbot_create_event_by_key(db, idempotency_key)
        if existing is not None:
            apply_existing_create_event_to_dry_run(row, existing)
            continue

        now = datetime.now(timezone.utc).isoformat()
        event = PendingEvent(
            event_type=SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
            idempotency_key=idempotency_key,
            status="pending",
            attempts=0,
            payload={
                "version": 1,
                "import_id": import_id,
                "order_id": order_id,
                "idempotency_key": idempotency_key,
                "request_payload": payload,
                "request_payload_hash": stable_payload_hash(payload),
                "queued_at": now,
                "create_status": "queued",
            },
            last_error=None,
        )
        db.add(event)
        db.flush()
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


def find_skladbot_create_event_by_key(db: Session, idempotency_key: str) -> PendingEvent | None:
    if not idempotency_key:
        return None
    return db.execute(
        select(PendingEvent).where(PendingEvent.idempotency_key == idempotency_key)
    ).scalar_one_or_none()


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
        row["status"] = "blocked"
        row["error"] = normalize_text(event.last_error)
        return
    if event.status == "failed":
        row["status"] = "create_failed"
        row["error"] = normalize_text(event.last_error)
        return
    row["status"] = "queued"
    row["error"] = ""


def skladbot_create_idempotency_key(order_id: str) -> str:
    return f"skladbot:create:v1:order:{normalize_text(order_id)}"


def stable_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_order_dry_run(order: Order, items: list[Any], import_id: str, index: int) -> dict[str, Any]:
    raw_payload = order.raw_payload or {}
    linked_number = normalize_text(raw_payload.get("skladbot_request_number"))
    linked_id = normalize_text(raw_payload.get("skladbot_request_id"))
    products = [build_product_dry_run(item.product, item.quantity_blocks) for item in items]
    blocks = sum(int(product.get("quantity_blocks") or 0) for product in products)
    status = "ready"
    error = ""

    if linked_number or linked_id:
        status = "already_linked"
        error = "У заказа уже есть номер или ID SkladBot"
    else:
        blocked_errors = [product["error"] for product in products if product.get("status") == "blocked"]
        if blocked_errors:
            status = "blocked"
            error = "; ".join(blocked_errors)

    payload = {}
    if status == "ready":
        payload = build_skladbot_payload(order, products)

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
        "products": products,
        "payload": payload,
    }


def build_product_dry_run(product: str, quantity_blocks: int) -> dict[str, Any]:
    sku_key = product_sku_key(product)
    mapping = SKU_MAPPING.get(sku_key)
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


def build_skladbot_payload(order: Order, products: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "customer_id": SKLADBOT_CUSTOMER_ID,
        "request_type_id": SKLADBOT_REQUEST_TYPE_ID,
        "notify": True,
        "comment": order.payment_type,
        "fields": {
            "address": {"value": order.address},
            "comment": {"value": order.payment_type},
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


def summarize_dry_runs(dry_runs: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    summary = default_summary(mode=mode)
    summary["orders"] = len(dry_runs)
    for item in dry_runs:
        status = str(item.get("status") or "")
        if status in summary:
            summary[status] += 1
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
) -> dict[str, Any]:
    if skladbot_create_requests_mode() != "enabled":
        return default_create_processing_result(status="disabled")

    client = client or SkladBotClient()
    if not getattr(client, "configured", False):
        return default_create_processing_result(status="not_configured")

    reset_stale_skladbot_create_events(db)
    limit = max(1, min(int(limit or env_int(SKLADBOT_REQUEST_CREATE_LIMIT_ENV, 20)), 100))
    events = select_pending_skladbot_create_events(db, limit)
    result = default_create_processing_result(status="completed")
    result["checked"] = len(events)
    if not events:
        return result

    for event in events:
        event.status = "processing"
        event.attempts = int(event.attempts or 0) + 1
        db.commit()

        try:
            event_result = process_skladbot_create_event(db, event, client)
        except Exception as exc:
            event_result = {"status": "create_failed", "error": sanitize_skladbot_error(exc)}

        finish_skladbot_create_event(db, event, event_result, result)

    if result["failed"]:
        result["status"] = "completed_with_errors"
    result["remaining"] = count_pending_skladbot_create_events(db)
    return result


def default_create_processing_result(status: str = "completed") -> dict[str, Any]:
    return {
        "status": status,
        "checked": 0,
        "created": 0,
        "recovered": 0,
        "already_linked": 0,
        "blocked": 0,
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
        .order_by(PendingEvent.created_at, PendingEvent.id)
        .limit(limit)
    )
    if db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    return db.execute(stmt).scalars().all()


def reset_stale_skladbot_create_events(db: Session) -> int:
    cutoff = datetime.now(timezone.utc) - STALE_SKLADBOT_CREATE_TIMEOUT
    events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
        .where(PendingEvent.status == "processing")
        .where(PendingEvent.updated_at < cutoff)
    ).scalars().all()
    if not events:
        return 0
    for event in events:
        event.status = "pending"
        event.last_error = "stale SkladBot create event reset"
        event.payload = {
            **(event.payload or {}),
            "create_status": "queued",
            "reset_at": datetime.now(timezone.utc).isoformat(),
        }
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
    return len(events)


def count_pending_skladbot_create_events(db: Session) -> int:
    return len(db.execute(
        select(PendingEvent.id)
        .where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
        .where(PendingEvent.status.in_(("pending", "failed")))
    ).scalars().all())


def is_skladbot_stock_shortage_error(error: str) -> bool:
    text = normalize_text(error).casefold().replace("ё", "е")
    if not text:
        return False
    direct_phrases = (
        "не хватает",
        "не хватило",
        "недостаточно",
        "insufficient stock",
        "not enough stock",
        "not enough quantity",
        "not enough products",
    )
    if any(phrase in text for phrase in direct_phrases):
        return True
    if "недостат" in text and any(word in text for word in ("товар", "остат", "склад", "количеств")):
        return True
    if "остат" in text and "меньш" in text and any(word in text for word in ("товар", "количеств", "заявк")):
        return True
    return False


def order_has_scans(order: Order) -> bool:
    for item in order.items or []:
        if int(item.scanned_blocks or 0) > 0:
            return True
        if getattr(item, "scan_codes", None):
            return True
    return False


def build_order_google_delete_records(order: Order) -> list[dict[str, Any]]:
    records = []
    order_raw = dict(order.raw_payload or {})
    for item in order.items or []:
        item_raw = dict(item.raw_payload or {})
        item_key = (
            normalize_text(item_raw.get("item_key"))
            or normalize_text(item_raw.get("source_import_id"))
            or str(item.id)
        )
        source_file = normalize_text(item_raw.get("source_file"))
        row = {
            "order_date": order.order_date,
            "payment_type": order.payment_type,
            "client": order.client,
            "address": order.address,
            "representative": order.representative,
            "product": item.product,
            "quantity_pieces": item.quantity_pieces,
            "quantity_blocks": item.quantity_blocks,
            "status": item.status,
            "source_order_id": normalize_text(item_raw.get("source_order_id")),
            "source_import_id": normalize_text(item_raw.get("source_import_id")),
            "source_file": source_file,
            "source_row": item_raw.get("source_row"),
            "skladbot_request_number": normalize_text(order_raw.get("skladbot_request_number")),
            "skladbot_request_id": normalize_text(order_raw.get("skladbot_request_id")),
        }
        records.append(make_sheet_record(row, item_key=item_key, filename=source_file))
    return records


def record_matches_google_targets(record: dict[str, Any], import_ids: set[str], order_ids: set[str]) -> bool:
    import_id = normalize_text(record.get("ID импорта"))
    order_id = normalize_text(record.get("ID заказа"))
    return bool((import_id and import_id in import_ids) or (order_id and order_id in order_ids))


def remove_records_from_pending_google_import_exports(db: Session, records: list[dict[str, Any]]) -> dict[str, int]:
    import_ids = {normalize_text(record.get("ID импорта")) for record in records if normalize_text(record.get("ID импорта"))}
    order_ids = {normalize_text(record.get("ID заказа")) for record in records if normalize_text(record.get("ID заказа"))}
    if not import_ids and not order_ids:
        return {"removed": 0, "completed_events": 0}

    removed = 0
    completed_events = 0
    events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == "google_sheets_export")
        .where(PendingEvent.status.in_(("pending", "failed")))
    ).scalars().all()
    for pending_event in events:
        payload = dict(pending_event.payload or {})
        if payload.get("action") != "google_sheets_import_export":
            continue
        original_records = list(payload.get("records") or [])
        if not original_records:
            continue
        kept_records = [
            record
            for record in original_records
            if not record_matches_google_targets(record, import_ids, order_ids)
        ]
        if len(kept_records) == len(original_records):
            continue
        removed += len(original_records) - len(kept_records)
        payload["records"] = kept_records
        payload["stock_shortage_removed_records"] = removed
        pending_event.payload = payload
        if not kept_records:
            pending_event.status = "completed"
            pending_event.last_error = ""
            completed_events += 1
        db.add(pending_event)
    return {"removed": removed, "completed_events": completed_events}


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
        "Заказ отменён из-за недостатка товара",
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
        "SkladBot заявку не создал. Заказ удалён из активной БД и поставлено удаление из Google Sheets.",
    ])
    return "\n".join(lines)


def queue_stock_shortage_notification(
    db: Session,
    order: Order,
    event: PendingEvent,
    import_job: ImportJob | None,
    error: str,
) -> PendingEvent:
    chat_id = normalize_text((import_job.raw_payload or {}).get("telegram_chat_id")) if import_job else ""
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
            "kind": "skladbot_stock_shortage_cancelled_order",
            "chat_id": chat_id,
            "order_id": str(order.id),
            "import_id": str(import_job.id) if import_job else normalize_text((event.payload or {}).get("import_id")),
            "text": build_stock_shortage_notification_text(order, error),
            "error": normalize_text(error),
        },
    )
    db.add(notification)
    db.flush()
    return notification


def cancel_unscanned_order_after_skladbot_stock_shortage(
    db: Session,
    order: Order,
    event: PendingEvent,
    error: str,
) -> dict[str, Any]:
    if order_has_scans(order):
        guarded_error = f"{normalize_text(error)}; автоотмена пропущена, потому что у заказа уже есть сканы"
        mark_order_skladbot_create_failed(order, event, guarded_error)
        return {"status": "create_failed", "error": guarded_error, "order_id": str(order.id)}

    import_job = order_import_job(db, order, event)
    records = build_order_google_delete_records(order)
    pending_google_cleanup = remove_records_from_pending_google_import_exports(db, records)
    google_event = queue_google_sheets_export(
        db,
        GOOGLE_DELETE_IMPORT_RECORDS_ACTION,
        "order",
        str(order.id),
        result={"status": "queued", "updated": 0, "error": ""},
        payload={
            "records": records,
            "reason": "skladbot_stock_shortage",
        },
    )
    notification_event = queue_stock_shortage_notification(db, order, event, import_job, error)
    update_event_payload(event, {
        "create_status": "cancelled_stock_shortage",
        "error": normalize_text(error),
        "stock_shortage_cancelled_at": datetime.now(timezone.utc).isoformat(),
        "google_delete_event_id": str(google_event.id) if google_event else "",
        "telegram_notification_event_id": str(notification_event.id) if notification_event else "",
        "pending_google_cleanup": pending_google_cleanup,
    })
    order_id = str(order.id)
    db.add(AuditLog(
        action="skladbot_stock_shortage_order_cancelled",
        entity_type="order",
        entity_id=order_id,
        payload={
            "order_id": order_id,
            "import_id": str(import_job.id) if import_job else normalize_text((event.payload or {}).get("import_id")),
            "error": normalize_text(error),
            "google_records": len(records),
            "google_delete_event_id": str(google_event.id) if google_event else "",
            "telegram_notification_event_id": str(notification_event.id) if notification_event else "",
        },
    ))
    db.delete(order)
    return {
        "status": "cancelled_stock_shortage",
        "order_id": order_id,
        "error": normalize_text(error),
        "google_delete_event_id": str(google_event.id) if google_event else "",
        "telegram_notification_event_id": str(notification_event.id) if notification_event else "",
        "records": len(records),
    }


def process_skladbot_create_event(db: Session, event: PendingEvent, client: Any) -> dict[str, Any]:
    payload = event.payload or {}
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
    existing_number = normalize_text(raw_payload.get("skladbot_request_number"))
    existing_id = normalize_text(raw_payload.get("skladbot_request_id"))
    if existing_number or existing_id:
        update_event_payload(event, {
            "create_status": "already_linked",
            "created_request_id": existing_id,
            "created_request_number": existing_number,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        return {
            "status": "already_linked",
            "request_id": existing_id,
            "request_number": existing_number,
            "order_id": str(order.id),
        }

    dry_run = build_order_dry_run(order, list(order.items), str(payload.get("import_id") or ""), 1)
    if dry_run.get("status") != "ready":
        error = normalize_text(dry_run.get("error")) or "SkladBot payload is blocked"
        mark_order_skladbot_create_failed(order, event, error)
        return {"status": "blocked", "error": error, "order_id": str(order.id)}

    request_payload = dry_run.get("payload") or {}
    request_payload_hash = stable_payload_hash(request_payload)
    update_event_payload(event, {
        "request_payload": request_payload,
        "request_payload_hash": request_payload_hash,
    })

    if int(event.attempts or 0) > 1:
        existing_request = find_existing_skladbot_request_for_order(order, client)
        if existing_request:
            return save_skladbot_create_result(
                db,
                order,
                event,
                request_payload,
                existing_request,
                status="created_recovered",
            )

    try:
        response = client.create_request(request_payload)
    except Exception as exc:
        existing_request = find_existing_skladbot_request_for_order(order, client)
        if existing_request:
            return save_skladbot_create_result(
                db,
                order,
                event,
                request_payload,
                existing_request,
                status="created_recovered",
            )
        error = sanitize_skladbot_error(exc)
        if is_skladbot_stock_shortage_error(error):
            return cancel_unscanned_order_after_skladbot_stock_shortage(db, order, event, error)
        mark_order_skladbot_create_failed(order, event, error)
        return {"status": "create_failed", "error": error, "order_id": str(order.id)}

    response_request = normalize_created_request_response(response)
    request_id = parse_int(response_request.get("id"))
    if request_id <= 0:
        existing_request = find_existing_skladbot_request_for_order(order, client)
        if existing_request:
            return save_skladbot_create_result(
                db,
                order,
                event,
                request_payload,
                existing_request,
                status="created_recovered",
            )
        error = "SkladBot create response did not include request id"
        mark_order_skladbot_create_failed(order, event, error)
        return {"status": "create_failed", "error": error, "order_id": str(order.id)}

    try:
        detail = client.get_request_detail(request_id)
    except Exception as exc:
        existing_request = find_existing_skladbot_request_for_order(order, client)
        if existing_request:
            return save_skladbot_create_result(
                db,
                order,
                event,
                request_payload,
                existing_request,
                status="created_recovered",
            )
        error = f"SkladBot created request {request_id}, but canonical detail failed: {sanitize_skladbot_error(exc)}"
        mark_order_skladbot_create_failed(order, event, error)
        return {"status": "create_failed", "error": error, "order_id": str(order.id)}

    request = normalize_request_payload({"id": request_id}, detail)
    request_number = normalize_text(request.get("number"))
    if not request_number:
        existing_request = find_existing_skladbot_request_for_order(order, client)
        if existing_request and normalize_text(existing_request.get("number")):
            return save_skladbot_create_result(
                db,
                order,
                event,
                request_payload,
                existing_request,
                status="created_recovered",
            )
        error = f"SkladBot created request {request_id}, but canonical WH-R is empty"
        mark_order_skladbot_create_failed(order, event, error)
        return {"status": "create_failed", "error": error, "order_id": str(order.id)}

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
        "id": parse_int(data.get("id")),
        "number": normalize_text(data.get("delivery_number") or data.get("number")),
        "created_at": normalize_text(data.get("created_at") or data.get("createdAt")),
        "raw": {"response": data},
    }


def find_existing_skladbot_request_for_order(order: Order, client: Any) -> dict[str, Any] | None:
    try:
        list_items = client.list_requests()
    except Exception:
        return None
    detail_limit = max(1, min(env_int("SKLADBOT_CREATE_RECONCILE_DETAIL_LIMIT", 30), 100))
    checked = 0
    for item in list_items:
        request_id = parse_int(request_list_value(item, "id"))
        if request_id <= 0:
            continue
        try:
            detail = client.get_request_detail(request_id)
        except Exception:
            continue
        checked += 1
        request = normalize_request_payload(item, detail)
        if request_matches_order(order, request):
            return request
        if checked >= detail_limit:
            break
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
    request_id = normalize_text(request.get("id"))
    request_number = normalize_text(request.get("number"))
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
    queue_google_sheets_export(
        db,
        "google_sheets_skladbot_export",
        "skladbot",
        str(order.id),
        result={"status": "queued", "updated": 0, "error": ""},
        payload={
            "order_ids": [str(order.id)],
            "include_inactive": True,
            "include_archive": True,
        },
    )
    update_event_payload(event, {
        "create_status": status,
        "created_request_id": request_id,
        "created_request_number": request_number,
        "completed_at": checked_at,
        "response_summary": safe_skladbot_response_summary(response),
    })
    return {
        "status": status,
        "request_id": request_id,
        "request_number": request_number,
        "order_id": str(order.id),
    }


def mark_order_skladbot_create_failed(order: Order, event: PendingEvent, error: str) -> None:
    raw_payload = dict(order.raw_payload or {})
    raw_payload["skladbot_status"] = "create_failed"
    raw_payload["skladbot_checked_at"] = datetime.now(timezone.utc).isoformat()
    raw_payload["skladbot_error"] = normalize_text(error)
    raw_payload["skladbot_create_event_id"] = str(event.id)
    raw_payload["skladbot_create_idempotency_key"] = event.idempotency_key or ""
    order.raw_payload = raw_payload
    update_event_payload(event, {
        "create_status": "create_failed",
        "error": normalize_text(error),
    })


def finish_skladbot_create_event(
    db: Session,
    event: PendingEvent,
    event_result: dict[str, Any],
    result: dict[str, Any],
) -> None:
    status = normalize_text(event_result.get("status"))
    event.payload = {**(event.payload or {}), "last_result": event_result}
    if status in {"created", "created_recovered", "already_linked"}:
        event.status = "completed"
        event.last_error = ""
        if status == "created":
            result["created"] += 1
        elif status == "created_recovered":
            result["recovered"] += 1
        else:
            result["already_linked"] += 1
    elif status == "blocked":
        event.status = "blocked"
        event.last_error = normalize_text(event_result.get("error"))
        result["blocked"] += 1
    elif status == "cancelled_stock_shortage":
        event.status = "completed"
        event.last_error = ""
        result["stock_shortage_cancelled"] += 1
    else:
        event.status = "failed"
        event.last_error = normalize_text(event_result.get("error")) or "SkladBot request create failed"
        result["failed"] += 1
        result["errors"].append({
            "event_id": str(event.id),
            "order_id": (event.payload or {}).get("order_id") or "",
            "error": event.last_error,
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
