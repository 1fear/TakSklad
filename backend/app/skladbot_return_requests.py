from datetime import datetime, timedelta, timezone
from typing import Any, Callable
import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .event_leases import claim_event_leases, event_leases_enabled, finalize_event_leases
from .models import AuditLog, Order, PendingEvent
from .observability_context import bind_pending_event, log_trace
from .outbox_service import queue_outbox_event
from .representative_contacts import build_representative_comment, find_representative_contact
from .skladbot_request_dry_run import (
    SKLADBOT_CUSTOMER_ID,
    build_product_dry_run,
    normalize_created_request_response,
    safe_skladbot_response_summary,
    stable_payload_hash,
    update_event_payload,
)
from .skladbot_client import SkladBotClient, env_int, notify_skladbot_progress, sanitize_skladbot_error
from .skladbot_contracts import (
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
logger = logging.getLogger(__name__)


def skladbot_return_create_idempotency_key(order_id: str) -> str:
    return f"skladbot:return_create:v1:order:{normalize_text(order_id)}"


def queue_skladbot_return_request_create(
    db: Session,
    order: Order,
    confirmed_items: list[dict[str, Any]],
) -> PendingEvent | None:
    raw_payload = dict(order.raw_payload or {})
    if normalize_text(raw_payload.get("skladbot_return_request_number")) or normalize_text(raw_payload.get("skladbot_return_request_id")):
        return None

    order_id = str(order.id)
    idempotency_key = skladbot_return_create_idempotency_key(order_id)
    now = datetime.now(timezone.utc).isoformat()
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
    )
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

    comment = build_representative_comment(order.payment_type, order.representative, representative_contact)
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
    if event_leases_enabled():
        events = claim_event_leases(
            db,
            event_types=(SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE,),
            owner=f"skladbot-return:{uuid.uuid4()}",
            limit=limit,
        )
    else:
        reset_stale_skladbot_return_create_events(db)
        events = select_pending_skladbot_return_create_events(db, limit)
    result = default_return_create_processing_result(status="completed")
    result["checked"] = len(events)
    if not events:
        return result

    for index, event in enumerate(events, start=1):
        if not event.lease_owner:
            event.status = "processing"
            event.attempts = int(event.attempts or 0) + 1
            db.commit()
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
    )
    if db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    events = db.execute(stmt).scalars().all()
    if not events:
        return 0
    for event in events:
        event.status = "pending"
        event.available_at = now
        event.lease_owner = None
        event.lease_expires_at = None
        event.completed_at = None
        event.last_error = "stale SkladBot return create event reset"
        update_event_payload(event, {
            "create_status": "queued",
            "reset_at": now.isoformat(),
        })
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
    return len(events)


def count_pending_skladbot_return_create_events(db: Session) -> int:
    return int(db.execute(
        select(func.count(PendingEvent.id))
        .where(PendingEvent.event_type == SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE)
        .where(PendingEvent.status.in_(("pending", "failed")))
    ).scalar_one())


def process_skladbot_return_create_event(db: Session, event: PendingEvent, client: Any) -> dict[str, Any]:
    payload = event.payload or {}
    order_uuid = parse_uuid(payload.get("order_id"))
    if order_uuid is None:
        return {"status": "create_failed", "error": "invalid order id"}

    order = db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id == order_uuid)
    ).scalars().unique().one_or_none()
    if order is None:
        return {"status": "create_failed", "error": "order not found"}

    raw_payload = dict(order.raw_payload or {})
    existing_number = normalize_text(raw_payload.get("skladbot_return_request_number"))
    existing_id = normalize_text(raw_payload.get("skladbot_return_request_id"))
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

    confirmed_items = payload.get("confirmed_items") or raw_payload.get("skladbot_return_confirmed_items") or []
    request_payload, blocked_errors = build_skladbot_return_payload(
        order,
        confirmed_items,
        representative_contact=find_representative_contact(db, order.representative),
    )
    if blocked_errors:
        error = "; ".join(blocked_errors)
        mark_order_skladbot_return_create_failed(order, event, error, status="blocked")
        return {"status": "blocked", "error": error, "order_id": str(order.id)}

    update_event_payload(event, {
        "request_payload": request_payload,
        "request_payload_hash": stable_payload_hash(request_payload),
    })

    if int(event.attempts or 0) > 1:
        existing_request = find_existing_skladbot_return_request_for_order(order, client)
        if existing_request:
            return save_skladbot_return_create_result(
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
        existing_request = find_existing_skladbot_return_request_for_order(order, client)
        if existing_request:
            return save_skladbot_return_create_result(
                db,
                order,
                event,
                request_payload,
                existing_request,
                status="created_recovered",
            )
        error = sanitize_skladbot_error(exc)
        mark_order_skladbot_return_create_failed(order, event, error)
        return {"status": "create_failed", "error": error, "order_id": str(order.id)}

    response_request = normalize_created_request_response(response)
    request_id = parse_int(response_request.get("id"))
    if request_id <= 0:
        existing_request = find_existing_skladbot_return_request_for_order(order, client)
        if existing_request:
            return save_skladbot_return_create_result(
                db,
                order,
                event,
                request_payload,
                existing_request,
                status="created_recovered",
            )
        error = "SkladBot return create response did not include request id"
        mark_order_skladbot_return_create_failed(order, event, error)
        return {"status": "create_failed", "error": error, "order_id": str(order.id)}

    try:
        detail = client.get_request_detail(request_id)
    except Exception as exc:
        existing_request = find_existing_skladbot_return_request_for_order(order, client)
        if existing_request:
            return save_skladbot_return_create_result(
                db,
                order,
                event,
                request_payload,
                existing_request,
                status="created_recovered",
            )
        error = f"SkladBot created return request {request_id}, but canonical detail failed: {sanitize_skladbot_error(exc)}"
        mark_order_skladbot_return_create_failed(order, event, error)
        return {"status": "create_failed", "error": error, "order_id": str(order.id)}

    request = normalize_request_payload({"id": request_id}, detail)
    request_number = normalize_text(request.get("number"))
    if not request_number:
        existing_request = find_existing_skladbot_return_request_for_order(order, client)
        if existing_request and normalize_text(existing_request.get("number")):
            return save_skladbot_return_create_result(
                db,
                order,
                event,
                request_payload,
                existing_request,
                status="created_recovered",
            )
        error = f"SkladBot created return request {request_id}, but canonical WH-R is empty"
        mark_order_skladbot_return_create_failed(order, event, error)
        return {"status": "create_failed", "error": error, "order_id": str(order.id)}

    return save_skladbot_return_create_result(
        db,
        order,
        event,
        request_payload,
        request,
        status="created",
        response=response,
    )


def find_existing_skladbot_return_request_for_order(order: Order, client: Any) -> dict[str, Any] | None:
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
    request_id = normalize_text(request.get("id"))
    request_number = normalize_text(request.get("number"))
    raw_payload = dict(order.raw_payload or {})
    raw_payload.update({
        "skladbot_return_request_id": request_id,
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
    elif status == "blocked":
        final_status = "blocked"
        final_error = normalize_text(event_result.get("error"))
        result["blocked"] += 1
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
