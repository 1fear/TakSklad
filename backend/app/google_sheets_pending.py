import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .google_sheets_exporter import (
    append_import_records_to_google_sheets,
    archive_backend_order_to_google_sheets,
    mark_backend_order_returned_in_google_sheets,
    sync_backend_order_item_to_google_sheets,
)
from .models import AuditLog, Order, OrderItem, PendingEvent


GOOGLE_SHEETS_EXPORT_EVENT_TYPE = "google_sheets_export"
RETRYABLE_EXPORT_STATUSES = {"disabled", "error"}
SUCCESS_EXPORT_STATUSES = {"completed", "skipped"}
logger = logging.getLogger(__name__)


def should_queue_google_sheets_export(result):
    status = str((result or {}).get("status") or "").strip().lower()
    return status in RETRYABLE_EXPORT_STATUSES


def queue_google_sheets_export(db: Session, action, entity_type, entity_id, result=None, payload=None):
    action = str(action or "").strip()
    entity_type = str(entity_type or "").strip()
    entity_id = str(entity_id or "").strip()
    if not action:
        return None

    event_payload = {
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "last_result": result or {},
        **(payload or {}),
    }
    candidate_events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == GOOGLE_SHEETS_EXPORT_EVENT_TYPE)
        .where(PendingEvent.status.in_(("pending", "failed")))
    ).scalars().all()
    existing = next(
        (
            event
            for event in candidate_events
            if (event.payload or {}).get("action") == action
            and str((event.payload or {}).get("entity_id") or "") == entity_id
        ),
        None,
    )
    if existing is not None:
        existing.payload = {**(existing.payload or {}), **event_payload}
        existing.status = "pending"
        existing.last_error = format_export_error(result)
        return existing

    event = PendingEvent(
        event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
        status="pending",
        attempts=0,
        payload=event_payload,
        last_error=format_export_error(result),
    )
    db.add(event)
    db.flush()
    return event


def mark_google_sheets_export_synced(db: Session, action, entity_id, result=None):
    action = str(action or "").strip()
    entity_id = str(entity_id or "").strip()
    if not action or not entity_id:
        return 0

    candidate_events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == GOOGLE_SHEETS_EXPORT_EVENT_TYPE)
        .where(PendingEvent.status.in_(("pending", "failed")))
    ).scalars().all()
    events = [
        event
        for event in candidate_events
        if (event.payload or {}).get("action") == action
        and str((event.payload or {}).get("entity_id") or "") == entity_id
    ]
    for event in events:
        event.status = "completed"
        event.last_error = ""
        event.payload = {**(event.payload or {}), "last_result": result or {}}
    return len(events)


def process_pending_google_sheets_exports(db: Session, limit=50):
    limit = max(1, min(int(limit or 50), 200))
    events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == GOOGLE_SHEETS_EXPORT_EVENT_TYPE)
        .where(PendingEvent.status.in_(("pending", "failed")))
        .order_by(PendingEvent.created_at, PendingEvent.id)
        .limit(limit)
    ).scalars().all()
    result = {
        "status": "completed",
        "checked": len(events),
        "synced": 0,
        "failed": 0,
        "remaining": 0,
        "errors": [],
    }
    if not events:
        return result

    for event in events:
        event.status = "processing"
        event.attempts = int(event.attempts or 0) + 1
        db.commit()

        try:
            export_result = run_google_sheets_export_event(db, event)
        except Exception as exc:
            logger.exception("Pending Google Sheets export failed")
            export_result = {"status": "error", "error": str(exc)}

        status = str(export_result.get("status") or "").strip().lower()
        event.payload = {**(event.payload or {}), "last_result": export_result}
        if status in SUCCESS_EXPORT_STATUSES:
            event.status = "completed"
            event.last_error = ""
            result["synced"] += 1
        else:
            event.status = "failed"
            event.last_error = format_export_error(export_result)
            result["failed"] += 1
            result["errors"].append({
                "id": str(event.id),
                "action": (event.payload or {}).get("action") or "",
                "entity_id": (event.payload or {}).get("entity_id") or "",
                "status": export_result.get("status") or "error",
                "error": event.last_error,
            })

        db.add(AuditLog(
            action="google_sheets_pending_export_processed",
            entity_type="pending_event",
            entity_id=str(event.id),
            payload={
                "event_action": (event.payload or {}).get("action") or "",
                "event_entity_id": (event.payload or {}).get("entity_id") or "",
                "result": export_result,
            },
        ))
        db.commit()

    result["remaining"] = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == GOOGLE_SHEETS_EXPORT_EVENT_TYPE)
        .where(PendingEvent.status.in_(("pending", "failed")))
    ).scalars().unique().all()
    result["remaining"] = len(result["remaining"])
    if result["failed"]:
        result["status"] = "completed_with_errors"
    return result


def run_google_sheets_export_event(db: Session, event: PendingEvent):
    payload = event.payload or {}
    action = payload.get("action") or ""
    entity_id = payload.get("entity_id") or ""
    entity_uuid = parse_uuid(entity_id)

    if action == "google_sheets_scan_export":
        if entity_uuid is None:
            return {"status": "missing", "error": "invalid order item id"}
        item = db.execute(
            select(OrderItem)
            .options(selectinload(OrderItem.order), selectinload(OrderItem.scan_codes))
            .where(OrderItem.id == entity_uuid)
        ).scalar_one_or_none()
        if item is None:
            return {"status": "missing", "error": "order item not found"}
        return sync_backend_order_item_to_google_sheets(item)

    if action == "google_sheets_archive_export":
        if entity_uuid is None:
            return {"status": "missing", "error": "invalid order id"}
        order = db.execute(
            select(Order)
            .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
            .where(Order.id == entity_uuid)
        ).scalar_one_or_none()
        if order is None:
            return {"status": "missing", "error": "order not found"}
        return archive_backend_order_to_google_sheets(order)

    if action == "google_sheets_return_export":
        if entity_uuid is None:
            return {"status": "missing", "error": "invalid order id"}
        order = db.execute(
            select(Order)
            .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
            .where(Order.id == entity_uuid)
        ).scalar_one_or_none()
        if order is None:
            return {"status": "missing", "error": "order not found"}
        return mark_backend_order_returned_in_google_sheets(order)

    if action == "google_sheets_import_export":
        records = payload.get("records") or []
        if not records:
            return {"status": "missing", "error": "import records not found"}
        return append_import_records_to_google_sheets(records)

    return {"status": "missing", "error": f"unknown google export action: {action}"}


def format_export_error(result):
    if not result:
        return ""
    error = str(result.get("error") or "").strip()
    if error:
        return error
    status = str(result.get("status") or "").strip()
    return f"Google Sheets export status: {status}" if status else ""


def parse_uuid(value):
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
