from datetime import datetime, timedelta, timezone
from typing import Any, Callable
import logging
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from .event_leases import claim_event_leases, event_leases_enabled, finalize_event_leases
from .models import AuditLog, Incident, Order, PendingEvent
from .observability_context import bind_pending_event, log_trace
from .outbox_service import (
    OutboxIdentityConflict,
    outbox_event_identity_conflict_reason,
    queue_outbox_event,
)
from .representative_contacts import build_representative_comment, find_representative_contact
from .skladbot_request_dry_run import (
    SKLADBOT_CUSTOMER_ID,
    build_product_dry_run,
    claim_next_pending_event_without_lease,
    classify_skladbot_create_exception,
    markerless_skladbot_request_payload,
    normalize_created_request_response,
    safe_skladbot_response_summary,
    stable_payload_hash,
    update_event_payload,
)
from .skladbot_client import (
    SkladBotApiError,
    SkladBotClient,
    SkladBotErrorKind,
    env_int,
    notify_skladbot_progress,
    sanitize_skladbot_error,
)
from .skladbot_contracts import (
    canonical_remote_request_id,
    canonical_skladbot_request_number,
    normalize_request_payload,
    normalize_text,
    parse_int,
    request_list_value,
    request_matches_order,
)


SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE = "skladbot_return_request_create"
SKLADBOT_RETURN_REQUEST_TYPE_ID = 3403
SKLADBOT_RETURN_REQUEST_CREATE_LIMIT_ENV = "SKLADBOT_RETURN_REQUEST_CREATE_LIMIT"
STALE_SKLADBOT_RETURN_CREATE_TIMEOUT = timedelta(minutes=10)
SKLADBOT_RETURN_STALE_RESET_LIMIT_ENV = "SKLADBOT_RETURN_STALE_RESET_LIMIT"
SKLADBOT_RETURN_DETAIL_RETRY_DELAY = timedelta(minutes=5)
logger = logging.getLogger(__name__)


def skladbot_return_create_idempotency_key(order_id: str) -> str:
    return f"skladbot:return_create:v1:order:{normalize_text(order_id)}"


def canonical_skladbot_return_request_id(value: Any) -> int:
    normalized = canonical_remote_request_id(value)
    return int(normalized) if normalized else 0


def canonical_skladbot_return_request_number(value: Any) -> str:
    return canonical_skladbot_request_number(value)


def queue_skladbot_return_request_create(
    db: Session,
    order: Order,
    confirmed_items: list[dict[str, Any]],
) -> PendingEvent | None:
    raw_payload = dict(order.raw_payload or {})
    existing_id = canonical_skladbot_return_request_id(raw_payload.get("skladbot_return_request_id"))
    existing_number = canonical_skladbot_return_request_number(raw_payload.get("skladbot_return_request_number"))
    if existing_id > 0 and existing_number:
        return None

    order_id = str(order.id)
    idempotency_key = skladbot_return_create_idempotency_key(order_id)
    now = datetime.now(timezone.utc).isoformat()
    try:
        event = queue_outbox_event(
            db,
            event_type=SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE,
            action=SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE,
            aggregate_type="order",
            aggregate_id=order_id,
            idempotency_key=idempotency_key,
            payload={
                "version": 1,
                "order_id": order_id,
                "idempotency_key": idempotency_key,
                "confirmed_items": confirmed_items,
                "queued_at": now,
                "create_status": "queued",
            },
            strict_identity=True,
        )
    except OutboxIdentityConflict as exc:
        mark_skladbot_return_queue_identity_conflict(
            db,
            order,
            idempotency_key=idempotency_key,
            existing_event_id=exc.existing_event_id,
            detail=exc.reason,
        )
        return None
    identity_error = skladbot_return_queue_identity_conflict_reason(
        event,
        idempotency_key=idempotency_key,
        order_id=order_id,
    )
    if identity_error:
        mark_skladbot_return_queue_identity_conflict(
            db,
            order,
            idempotency_key=idempotency_key,
            existing_event_id=str(event.id),
            detail=identity_error,
        )
        return None
    raw_payload.update({
        "skladbot_return_request_status": "queued",
        "skladbot_return_create_event_id": str(event.id),
        "skladbot_return_create_idempotency_key": idempotency_key,
    })
    order.raw_payload = raw_payload
    db.add(AuditLog(
        action="skladbot_return_request_create_queued",
        entity_type="order",
        entity_id=order_id,
        payload={
            "event_id": str(event.id),
            "idempotency_key": idempotency_key,
            "confirmed_items": confirmed_items,
        },
    ))
    return event


def skladbot_return_queue_identity_conflict_reason(
    event: PendingEvent,
    *,
    idempotency_key: str,
    order_id: str,
) -> str:
    canonical_order_id = parse_uuid(order_id)
    reason = outbox_event_identity_conflict_reason(
        event,
        event_type=SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE,
        action=SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE,
        aggregate_type="order",
        aggregate_id=normalize_text(order_id),
        idempotency_key=idempotency_key,
    )
    if reason:
        return reason
    payload = event.payload if isinstance(event.payload, dict) else {}
    payload_order_id = parse_uuid(payload.get("order_id"))
    if canonical_order_id is None or payload_order_id != canonical_order_id:
        return "payload_order_id_mismatch"
    if normalize_text(payload.get("idempotency_key")) != normalize_text(idempotency_key):
        return "payload_idempotency_key_mismatch"
    return ""


def mark_skladbot_return_queue_identity_conflict(
    db: Session,
    order: Order,
    *,
    idempotency_key: str,
    existing_event_id: str,
    detail: str,
) -> None:
    error = "SkladBot return queue identity conflict; manual review required"
    raw_payload = dict(order.raw_payload or {})
    raw_payload["skladbot_return_request_status"] = "manual_review"
    raw_payload["skladbot_return_error"] = error
    raw_payload.pop("skladbot_return_create_event_id", None)
    raw_payload.pop("skladbot_return_create_idempotency_key", None)
    order.raw_payload = raw_payload
    incident = db.execute(
        select(Incident)
        .where(Incident.source == "skladbot_return_create")
        .where(Incident.order_id == order.id)
        .where(Incident.title == "SkladBot return queue identity conflict")
    ).scalar_one_or_none()
    if incident is None:
        db.add(Incident(
            source="skladbot_return_create",
            severity="critical",
            status="manual_review",
            title="SkladBot return queue identity conflict",
            message=error,
            entity_type="order",
            entity_id=str(order.id),
            order_id=order.id,
            raw_payload={"reason": "event_identity_conflict"},
        ))
        db.add(AuditLog(
            action="skladbot_return_request_create_identity_conflict",
            entity_type="order",
            entity_id=str(order.id),
            payload={"reason": "event_identity_conflict"},
        ))
    else:
        incident.status = "manual_review"
        incident.severity = "critical"
        incident.message = error


def build_skladbot_return_payload(
    order: Order,
    confirmed_items: list[dict[str, Any]],
    *,
    representative_contact: Any | None = None,
) -> tuple[dict[str, Any], list[str]]:
    products = [
        build_product_dry_run(item.get("product") or item.get("sku") or "", parse_int(item.get("quantity_blocks")))
        for item in confirmed_items
    ]
    blocked_errors = [product["error"] for product in products if product.get("status") == "blocked"]
    if blocked_errors:
        return {}, blocked_errors

    comment = markerless_skladbot_return_comment(
        build_representative_comment(order.payment_type, order.representative, representative_contact)
    )
    payload = {
        "customer_id": SKLADBOT_CUSTOMER_ID,
        "request_type_id": SKLADBOT_RETURN_REQUEST_TYPE_ID,
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
    return payload, []


def markerless_skladbot_return_comment(comment: Any) -> str:
    return markerless_skladbot_request_payload({"comment": comment})["comment"]


def process_pending_skladbot_return_request_creates(
    db: Session,
    client: Any | None = None,
    limit: int | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    client = client or SkladBotClient(progress_callback=progress_callback)
    if not getattr(client, "configured", False):
        return default_return_create_processing_result(status="not_configured")

    limit = max(1, min(int(limit or env_int(SKLADBOT_RETURN_REQUEST_CREATE_LIMIT_ENV, 20)), 100))
    leases_enabled = event_leases_enabled()
    reset_stale_skladbot_return_create_events(db)
    if leases_enabled:
        events = claim_event_leases(
            db,
            event_types=(SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE,),
            owner=f"skladbot-return:{uuid.uuid4()}",
            limit=limit,
            now=datetime.now(timezone.utc),
        )
    else:
        events = None
    result = default_return_create_processing_result(status="completed")
    if leases_enabled and not events:
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
                event_type=SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE,
                excluded_event_ids=claimed_event_ids,
            )
            if event is None:
                break
            claimed_event_ids.add(event.id)
        result["checked"] += 1
        with bind_pending_event(event):
            log_trace(logger, "event_processing_started", event_type="skladbot_return_request_create")
            try:
                event_result = process_skladbot_return_create_event(db, event, client)
            except Exception as exc:
                event_result = {"status": "create_failed", "error": sanitize_skladbot_error(exc)}
            finish_skladbot_return_create_event(db, event, event_result, result)
            log_trace(
                logger,
                "event_processing_finished",
                event_type="skladbot_return_request_create",
                result=event_result.get("status") or "failed",
            )
        if progress_callback is not None:
            notify_skladbot_progress(progress_callback, f"return_events_processed:{index}")

    if result["failed"]:
        result["status"] = "completed_with_errors"
    result["remaining"] = count_pending_skladbot_return_create_events(db)
    return result


def default_return_create_processing_result(status: str = "completed") -> dict[str, Any]:
    return {
        "status": status,
        "checked": 0,
        "created": 0,
        "recovered": 0,
        "already_linked": 0,
        "blocked": 0,
        "ambiguous": 0,
        "failed": 0,
        "remaining": 0,
        "errors": [],
    }


def select_pending_skladbot_return_create_events(db: Session, limit: int) -> list[PendingEvent]:
    stmt = (
        select(PendingEvent)
        .where(PendingEvent.event_type == SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE)
        .where(PendingEvent.status.in_(("pending", "failed")))
        .where(PendingEvent.available_at <= datetime.now(timezone.utc))
        .order_by(PendingEvent.created_at, PendingEvent.id)
        .limit(limit)
    )
    if db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    return db.execute(stmt).scalars().all()


def reset_stale_skladbot_return_create_events(db: Session) -> int:
    now = datetime.now(timezone.utc)
    cutoff = now - STALE_SKLADBOT_RETURN_CREATE_TIMEOUT
    limit = max(1, min(env_int(SKLADBOT_RETURN_STALE_RESET_LIMIT_ENV, 100), 1000))
    stmt = (
        select(PendingEvent)
        .where(PendingEvent.event_type == SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE)
        .where(PendingEvent.status == "processing")
        .where(PendingEvent.updated_at < cutoff)
        .where((PendingEvent.lease_owner.is_(None)) | (PendingEvent.lease_expires_at <= now))
        .order_by(PendingEvent.updated_at, PendingEvent.created_at, PendingEvent.id)
        .limit(limit)
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
            .where(PendingEvent.event_type == SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE)
            .where(PendingEvent.status == "processing")
            .where(PendingEvent.updated_at < cutoff)
            .where((PendingEvent.lease_owner.is_(None)) | (PendingEvent.lease_expires_at <= now))
            .values(
                status="pending",
                available_at=now,
                lease_owner=None,
                lease_expires_at=None,
                completed_at=None,
                last_error="stale SkladBot return create event reset",
                updated_at=now,
            )
            .returning(PendingEvent.id)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()
        if reset_id is None:
            continue
        event = db.get(PendingEvent, reset_id, populate_existing=True)
        update_event_payload(event, {"reset_at": now.isoformat()})
        reset_events.append(event)
        db.add(AuditLog(
            action="skladbot_return_request_create_stale_reset",
            entity_type="pending_event",
            entity_id=str(event.id),
            payload={
                "order_id": (event.payload or {}).get("order_id") or "",
                "idempotency_key": event.idempotency_key or "",
            },
        ))
    db.commit()
    return len(reset_events)


def count_pending_skladbot_return_create_events(db: Session) -> int:
    return int(db.execute(
        select(func.count(PendingEvent.id))
        .where(PendingEvent.event_type == SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE)
        .where(PendingEvent.status.in_(("pending", "failed")))
    ).scalar_one())


def process_skladbot_return_create_event(db: Session, event: PendingEvent, client: Any) -> dict[str, Any]:
    payload = dict(event.payload or {})
    ownership_error = skladbot_return_create_event_ownership_error(event, payload)
    if ownership_error:
        return mark_skladbot_return_manual_review(
            db,
            event,
            ownership_error,
            reason="ownership_invalid",
            post_state="ownership_invalid",
            result_status="blocked",
        )
    order_uuid = parse_uuid(payload.get("order_id"))

    order = db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id == order_uuid)
    ).scalars().unique().one_or_none()
    if order is None:
        return mark_skladbot_return_manual_review(
            db,
            event,
            "SkladBot return create order not found",
            reason="order_not_found",
            post_state="ownership_invalid",
            result_status="blocked",
        )
    if order.id != order_uuid:
        return mark_skladbot_return_manual_review(
            db,
            event,
            "SkladBot return create loaded order ownership mismatch",
            reason="loaded_order_mismatch",
            post_state="ownership_invalid",
            result_status="blocked",
        )

    raw_payload = dict(order.raw_payload or {})
    existing_id_raw = normalize_text(raw_payload.get("skladbot_return_request_id"))
    existing_number_raw = normalize_text(raw_payload.get("skladbot_return_request_number"))
    existing_id = canonical_skladbot_return_request_id(existing_id_raw)
    existing_number = canonical_skladbot_return_request_number(existing_number_raw)
    durable_response_id_value = payload.get("post_response_request_id")
    durable_response_id_present = durable_response_id_value is not None and str(
        durable_response_id_value
    ) != ""
    response_request_id = canonical_skladbot_return_request_id(durable_response_id_value)
    if existing_id > 0 and existing_number:
        if durable_response_id_present and response_request_id <= 0:
            return mark_skladbot_return_manual_review(
                db,
                event,
                "Existing SkladBot return link has malformed durable response request id",
                order=order,
                reason="existing_link_response_id_malformed",
                post_state="response_received",
            )
        if response_request_id > 0 and response_request_id != existing_id:
            return mark_skladbot_return_manual_review(
                db,
                event,
                "Existing SkladBot return link conflicts with durable response request id",
                order=order,
                reason="existing_link_response_id_mismatch",
                post_state="response_received",
            )
        update_event_payload(event, {
            "create_status": "already_linked",
            "created_request_id": str(existing_id),
            "created_request_number": existing_number,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        resolve_skladbot_return_create_incidents(
            db,
            event,
            order_id=str(order.id),
            reason="already_linked",
        )
        return {
            "status": "already_linked",
            "request_id": str(existing_id),
            "request_number": existing_number,
            "order_id": str(order.id),
        }
    if existing_id_raw or existing_number_raw:
        if existing_id > 0 and not existing_number_raw and response_request_id == existing_id:
            request_payload = payload.get("request_payload") if isinstance(payload.get("request_payload"), dict) else {}
            return recover_skladbot_return_request_by_exact_id(
                db,
                order,
                event,
                client,
                request_payload,
                response_request_id,
                status="created_recovered",
            )
        reason = "existing_return_link_incomplete"
        if response_request_id > 0 and existing_id > 0 and response_request_id != existing_id:
            reason = "existing_link_response_id_mismatch"
        return mark_skladbot_return_manual_review(
            db,
            event,
            "Existing SkladBot return link is incomplete or conflicts with durable evidence",
            order=order,
            reason=reason,
            post_state="response_received" if response_request_id > 0 else "ambiguous",
        )

    request_payload = payload.get("request_payload") if isinstance(payload.get("request_payload"), dict) else {}
    if not request_payload:
        confirmed_items = payload.get("confirmed_items") or raw_payload.get("skladbot_return_confirmed_items") or []
        request_payload, blocked_errors = build_skladbot_return_payload(
            order,
            confirmed_items,
            representative_contact=find_representative_contact(db, order.representative),
        )
        if blocked_errors:
            error = "; ".join(blocked_errors)
            mark_order_skladbot_return_create_failed(order, event, error, status="blocked")
            ensure_skladbot_return_create_incident(db, event, error, order=order, status="manual_review")
            return {"status": "blocked", "error": error, "order_id": str(order.id)}

    request_payload = markerless_skladbot_request_payload(request_payload)

    update_event_payload(event, {
        "request_payload": request_payload,
        "request_payload_hash": stable_payload_hash(request_payload),
    })
    payload = dict(event.payload or {})
    post_state = normalize_text(payload.get("post_state"))
    response_request_id = canonical_skladbot_return_request_id(payload.get("post_response_request_id"))

    if response_request_id > 0:
        return recover_skladbot_return_request_by_exact_id(
            db,
            order,
            event,
            client,
            request_payload,
            response_request_id,
            status="created_recovered",
        )
    if post_state in {"started", "ambiguous", "response_received"}:
        return mark_skladbot_return_manual_review(
            db,
            event,
            "SkladBot return POST outcome is ambiguous and has no response request id",
            order=order,
            reason="ambiguous_without_response_id",
            post_state="ambiguous",
        )
    if int(event.attempts or 0) > 1 and post_state != "retry_scheduled":
        return mark_skladbot_return_manual_review(
            db,
            event,
            "Legacy SkladBot return create attempt has no durable POST state",
            order=order,
            reason="legacy_attempt_without_post_state",
            post_state="ambiguous",
        )
    if post_state not in {"", "queued", "retry_scheduled"}:
        return mark_skladbot_return_manual_review(
            db,
            event,
            "Unknown SkladBot return create POST state",
            order=order,
            reason="unknown_post_state",
            post_state="ambiguous",
        )

    started_at = datetime.now(timezone.utc).isoformat()
    payload_hash = stable_payload_hash(request_payload)
    update_event_payload(event, {
        "post_state": "started",
        "post_started_at": started_at,
        "request_payload_hash": payload_hash,
        "post_evidence": {
            "idempotency_key": event.idempotency_key or "",
            "request_payload_hash": payload_hash,
        },
    })
    db.add(AuditLog(
        action="skladbot_return_request_post_started",
        entity_type="pending_event",
        entity_id=str(event.id),
        payload={
            "order_id": str(order.id),
            "idempotency_key": event.idempotency_key or "",
            "request_payload_hash": payload_hash,
        },
    ))
    db.commit()

    try:
        response = client.create_request(request_payload)
    except Exception as exc:
        classification = classify_skladbot_create_exception(exc)
        error = sanitize_skladbot_error(exc)
        if classification == "rate_limited":
            retry_at = datetime.now(timezone.utc) + SKLADBOT_RETURN_DETAIL_RETRY_DELAY
            event.available_at = retry_at
            update_event_payload(event, {
                "post_state": "retry_scheduled",
                "create_status": "queued",
                "retry_at": retry_at.isoformat(),
                "error": error,
            })
            return {"status": "retry_scheduled", "error": error, "order_id": str(order.id)}
        if classification == "ambiguous":
            return mark_skladbot_return_manual_review(
                db,
                event,
                error or "SkladBot return POST result is ambiguous",
                order=order,
                reason="post_outcome_ambiguous",
                post_state="ambiguous",
            )
        return mark_skladbot_return_manual_review(
            db,
            event,
            error or "SkladBot return create was rejected",
            order=order,
            reason=f"post_rejected_{classification}",
            post_state="rejected",
            result_status="blocked",
        )

    response_request = normalize_created_request_response(response)
    request_id = canonical_skladbot_return_request_id(response_request.get("id"))
    if request_id <= 0:
        return mark_skladbot_return_manual_review(
            db,
            event,
            "SkladBot return create response did not include request id",
            order=order,
            reason="malformed_create_response",
            post_state="ambiguous",
        )

    update_event_payload(event, {
        "post_state": "response_received",
        "post_response_request_id": request_id,
        "post_response_received_at": datetime.now(timezone.utc).isoformat(),
        "response_summary": safe_skladbot_response_summary(response),
    })
    db.add(AuditLog(
        action="skladbot_return_request_response_received",
        entity_type="pending_event",
        entity_id=str(event.id),
        payload={
            "order_id": str(order.id),
            "request_id": request_id,
            "request_payload_hash": payload_hash,
        },
    ))
    db.commit()
    return recover_skladbot_return_request_by_exact_id(
        db,
        order,
        event,
        client,
        request_payload,
        request_id,
        status="created",
        response=response,
    )


def skladbot_return_create_event_ownership_error(event: PendingEvent, payload: dict[str, Any]) -> str:
    aggregate_uuid = parse_uuid(event.aggregate_id)
    payload_uuid = parse_uuid(payload.get("order_id"))
    if event.event_type != SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE:
        return "SkladBot return create event type ownership mismatch"
    if normalize_text(event.aggregate_type) != "order":
        return "SkladBot return create aggregate type ownership mismatch"
    if aggregate_uuid is None or payload_uuid is None or aggregate_uuid != payload_uuid:
        return "SkladBot return create order ownership mismatch"
    expected_key = skladbot_return_create_idempotency_key(str(payload_uuid))
    if normalize_text(event.idempotency_key) != expected_key:
        return "SkladBot return create idempotency ownership mismatch"
    return ""


def return_detail_retry_limit() -> int:
    return max(1, min(env_int("SKLADBOT_API_MAX_RETRIES", 2) + 1, 10))


def recover_skladbot_return_request_by_exact_id(
    db: Session,
    order: Order,
    event: PendingEvent,
    client: Any,
    request_payload: dict[str, Any],
    request_id: int,
    *,
    status: str,
    response: Any | None = None,
) -> dict[str, Any]:
    try:
        detail = client.get_request_detail(request_id)
    except Exception as exc:
        detail_attempts = parse_int((event.payload or {}).get("detail_recovery_attempts")) + 1
        error = f"SkladBot return request {request_id} exact detail failed: {sanitize_skladbot_error(exc)}"
        if isinstance(exc, SkladBotApiError) and exc.kind in {
            SkladBotErrorKind.AUTH,
            SkladBotErrorKind.CLIENT,
            SkladBotErrorKind.STOCK_SHORTAGE,
        }:
            return mark_skladbot_return_manual_review(
                db,
                event,
                error,
                order=order,
                reason=f"exact_detail_rejected_{exc.kind.value}",
                post_state="response_received",
                result_status="blocked",
            )
        if detail_attempts < return_detail_retry_limit():
            retry_at = datetime.now(timezone.utc) + SKLADBOT_RETURN_DETAIL_RETRY_DELAY
            event.available_at = retry_at
            update_event_payload(event, {
                "post_state": "response_received",
                "create_status": "detail_retry_scheduled",
                "detail_recovery_attempts": detail_attempts,
                "detail_retry_at": retry_at.isoformat(),
                "error": error,
            })
            ensure_skladbot_return_create_incident(db, event, error, order=order, status="open")
            return {"status": "retry_scheduled", "error": error, "order_id": str(order.id)}
        update_event_payload(event, {
            "detail_recovery_attempts": detail_attempts,
            "detail_retry_exhausted_at": datetime.now(timezone.utc).isoformat(),
        })
        return mark_skladbot_return_manual_review(
            db,
            event,
            error,
            order=order,
            reason="exact_detail_retry_exhausted",
            post_state="response_received",
        )

    if not isinstance(detail, dict) or not detail:
        return mark_skladbot_return_manual_review(
            db,
            event,
            f"SkladBot return request {request_id} canonical detail is empty",
            order=order,
            reason="canonical_detail_empty",
            post_state="response_received",
        )
    detail_request_id = canonical_skladbot_return_request_id(detail.get("id"))
    if detail_request_id != request_id:
        return mark_skladbot_return_manual_review(
            db,
            event,
            f"SkladBot return canonical detail id mismatch for request {request_id}",
            order=order,
            reason="canonical_detail_id_mismatch",
            post_state="response_received",
        )
    request = normalize_request_payload({"id": request_id}, detail)
    if (
        canonical_skladbot_return_request_id(request.get("id")) != request_id
        or not canonical_skladbot_return_request_number(request.get("number"))
    ):
        return mark_skladbot_return_manual_review(
            db,
            event,
            f"SkladBot return request {request_id} canonical WH-R/WR number is empty",
            order=order,
            reason="canonical_wh_r_empty",
            post_state="response_received",
        )
    return save_skladbot_return_create_result(
        db,
        order,
        event,
        request_payload,
        request,
        status=status,
        response=response,
    )


def find_existing_skladbot_return_request_for_order(order: Order, client: Any) -> dict[str, Any] | None:
    """Diagnostic-only fuzzy lookup; never used as automated create proof."""
    try:
        try:
            list_items = client.list_requests(type_id=SKLADBOT_RETURN_REQUEST_TYPE_ID)
        except TypeError:
            list_items = client.list_requests()
    except Exception:
        return None

    detail_limit = max(1, min(env_int("SKLADBOT_RETURN_CREATE_RECONCILE_DETAIL_LIMIT", 30), 100))
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


def ensure_skladbot_return_create_incident(
    db: Session,
    event: PendingEvent,
    error: str,
    *,
    order: Order | None = None,
    status: str = "manual_review",
) -> Incident:
    existing = db.execute(
        select(Incident)
        .where(Incident.pending_event_id == event.id)
        .where(Incident.source == "skladbot_return_create")
    ).scalar_one_or_none()
    evidence = {
        "event_id": str(event.id),
        "order_id": str(order.id) if order is not None else "",
        "idempotency_key": event.idempotency_key or "",
        "request_payload_hash": normalize_text((event.payload or {}).get("request_payload_hash")),
        "post_state": normalize_text((event.payload or {}).get("post_state")),
        "post_response_request_id": canonical_skladbot_return_request_id(
            (event.payload or {}).get("post_response_request_id")
        ),
        "error": normalize_text(error),
    }
    if existing is not None:
        existing.status = status
        existing.severity = "critical"
        existing.message = normalize_text(error)
        existing.raw_payload = {**(existing.raw_payload or {}), **evidence}
        return existing
    incident = Incident(
        source="skladbot_return_create",
        severity="critical",
        status=status,
        title="SkladBot return request create requires review",
        message=normalize_text(error),
        entity_type="order" if order is not None else "pending_event",
        entity_id=str(order.id) if order is not None else str(event.id),
        pending_event_id=event.id,
        order_id=order.id if order is not None else None,
        raw_payload=evidence,
    )
    db.add(incident)
    db.add(AuditLog(
        action="skladbot_return_create_incident_created",
        entity_type="pending_event",
        entity_id=str(event.id),
        payload={
            "order_id": evidence["order_id"],
            "status": status,
            "idempotency_key": evidence["idempotency_key"],
            "request_payload_hash": evidence["request_payload_hash"],
            "post_state": evidence["post_state"],
        },
    ))
    db.flush()
    return incident


def mark_skladbot_return_manual_review(
    db: Session,
    event: PendingEvent,
    error: str,
    *,
    order: Order | None = None,
    reason: str,
    post_state: str,
    result_status: str = "ambiguous",
) -> dict[str, Any]:
    update_event_payload(event, {
        "create_status": "ambiguous" if result_status == "ambiguous" else "blocked",
        "post_state": post_state,
        "manual_review_reason": reason,
        "manual_review_at": datetime.now(timezone.utc).isoformat(),
        "error": normalize_text(error),
    })
    if not event.lease_owner:
        event.status = "blocked"
        event.last_error = normalize_text(error)
        event.completed_at = datetime.now(timezone.utc)
    incident = ensure_skladbot_return_create_incident(
        db,
        event,
        error,
        order=order,
        status="manual_review",
    )
    db.add(AuditLog(
        action="skladbot_return_create_manual_review",
        entity_type="pending_event",
        entity_id=str(event.id),
        payload={
            "order_id": str(order.id) if order is not None else "",
            "reason": reason,
            "post_state": post_state,
            "request_id": canonical_skladbot_return_request_id(
                (event.payload or {}).get("post_response_request_id")
            ),
            "incident_id": str(incident.id),
        },
    ))
    return {
        "status": result_status,
        "error": normalize_text(error),
        "order_id": str(order.id) if order is not None else "",
        "incident_id": str(incident.id),
    }


def resolve_skladbot_return_create_incidents(
    db: Session,
    event: PendingEvent,
    *,
    order_id: str,
    reason: str,
) -> int:
    resolved_at = datetime.now(timezone.utc)
    resolved = 0
    for incident in db.execute(
        select(Incident)
        .where(Incident.pending_event_id == event.id)
        .where(Incident.source == "skladbot_return_create")
    ).scalars().all():
        if incident.status == "resolved" and incident.resolved_at is not None:
            continue
        incident.status = "resolved"
        incident.resolved_at = resolved_at
        resolved += 1
    if resolved:
        db.add(AuditLog(
            action="skladbot_return_create_incidents_resolved",
            entity_type="pending_event",
            entity_id=str(event.id),
            payload={
                "order_id": order_id,
                "reason": reason,
                "resolved_count": resolved,
            },
        ))
    return resolved


def save_skladbot_return_create_result(
    db: Session,
    order: Order,
    event: PendingEvent,
    request_payload: dict[str, Any],
    request: dict[str, Any],
    status: str,
    response: Any | None = None,
) -> dict[str, Any]:
    checked_at = datetime.now(timezone.utc).isoformat()
    request_id = canonical_skladbot_return_request_id(request.get("id"))
    request_number = canonical_skladbot_return_request_number(request.get("number"))
    if request_id <= 0 or not request_number:
        return mark_skladbot_return_manual_review(
            db,
            event,
            "Canonical SkladBot return request id and WH-R/WR number are required",
            order=order,
            reason="canonical_link_incomplete",
            post_state="response_received",
        )
    raw_payload = dict(order.raw_payload or {})
    raw_payload.update({
        "skladbot_return_request_id": str(request_id),
        "skladbot_return_request_number": request_number,
        "skladbot_return_request_status": status,
        "skladbot_return_checked_at": checked_at,
        "skladbot_return_created_at": checked_at,
        "skladbot_return_create_idempotency_key": event.idempotency_key or "",
        "skladbot_return_create_payload_hash": stable_payload_hash(request_payload),
        "skladbot_return_create_event_id": str(event.id),
        "skladbot_return_create_request_payload": request_payload,
        "skladbot_return_create_response": safe_skladbot_response_summary(response),
        "skladbot_return_raw": request.get("raw") or {},
    })
    raw_payload.pop("skladbot_return_error", None)
    order.raw_payload = raw_payload
    update_event_payload(event, {
        "create_status": status,
        "post_state": "completed",
        "created_request_id": str(request_id),
        "created_request_number": request_number,
        "completed_at": checked_at,
        "response_summary": safe_skladbot_response_summary(response),
    })
    resolve_skladbot_return_create_incidents(
        db,
        event,
        order_id=str(order.id),
        reason=status,
    )
    return {
        "status": status,
        "request_id": str(request_id),
        "request_number": request_number,
        "order_id": str(order.id),
    }


def mark_order_skladbot_return_create_failed(order: Order, event: PendingEvent, error: str, status: str = "create_failed") -> None:
    raw_payload = dict(order.raw_payload or {})
    raw_payload["skladbot_return_request_status"] = status
    raw_payload["skladbot_return_checked_at"] = datetime.now(timezone.utc).isoformat()
    raw_payload["skladbot_return_error"] = normalize_text(error)
    raw_payload["skladbot_return_create_event_id"] = str(event.id)
    raw_payload["skladbot_return_create_idempotency_key"] = event.idempotency_key or ""
    order.raw_payload = raw_payload
    update_event_payload(event, {
        "create_status": status,
        "error": normalize_text(error),
    })


def finish_skladbot_return_create_event(
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
    elif status in {"blocked", "ambiguous"}:
        final_status = "blocked"
        final_error = normalize_text(event_result.get("error"))
        result["blocked"] += 1
        if status == "ambiguous":
            result["ambiguous"] += 1
    elif status == "retry_scheduled":
        final_status = "pending"
        final_error = normalize_text(event_result.get("error"))
    else:
        final_error = normalize_text(event_result.get("error")) or "SkladBot return request create failed"
        result["failed"] += 1
        result["errors"].append({
            "event_id": str(event.id),
            "order_id": (event.payload or {}).get("order_id") or "",
            "error": final_error,
        })
    db.add(AuditLog(
        action="skladbot_return_request_create_processed",
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
            available_at=event.available_at if final_status == "pending" else None,
        )
    else:
        event.payload = event_payload
        event.status = final_status
        event.last_error = final_error
        event.completed_at = datetime.now(timezone.utc) if final_status in {"completed", "blocked"} else None
        db.commit()


def parse_uuid(value: Any):
    try:
        import uuid
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
