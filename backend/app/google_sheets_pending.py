import logging
from threading import Lock
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .google_sheets_exporter import (
    append_import_records_to_google_sheets,
    archive_backend_order_to_google_sheets,
    archive_backend_orders_to_google_sheets,
    archive_backend_order_without_kiz_to_google_sheets,
    cancel_backend_order_in_google_sheets,
    mark_backend_order_returned_in_google_sheets,
    restore_import_records_to_google_sheets,
    sync_backend_orders_skladbot_to_google_sheets,
    sync_backend_order_items_to_google_sheets,
    sync_backend_order_item_to_google_sheets,
)
from .models import AuditLog, Order, OrderItem, PendingEvent


GOOGLE_SHEETS_EXPORT_EVENT_TYPE = "google_sheets_export"
RETRYABLE_EXPORT_STATUSES = {"disabled", "error"}
SUCCESS_EXPORT_STATUSES = {"completed", "skipped"}
SKLADBOT_EXPORT_INACTIVE_ORDER_STATUSES = (
    "completed",
    "done",
    "closed",
    "returned",
    "archived_no_kiz",
    "cancelled",
)
SCAN_EXPORT_TERMINAL_STATUSES = {
    "completed",
    "done",
    "closed",
    "returned",
    "archived_no_kiz",
    "cancelled",
    "removed_from_google_sheet",
}
LOCAL_EXPORT_LOCK = Lock()
STALE_PROCESSING_EXPORT_TIMEOUT = timedelta(minutes=10)
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
    result = {
        "status": "completed",
        "checked": 0,
        "synced": 0,
        "failed": 0,
        "remaining": 0,
        "errors": [],
    }
    if not acquire_google_sheets_export_lock(db):
        return {**result, "status": "busy", "message": "Google Sheets export is already running"}

    try:
        reset_stale_processing_export_events(db)
        events = select_pending_export_events(db, limit)
        result["checked"] = len(events)
        if not events:
            return result
        batch_result = process_scan_export_events_batch(db, events, result)
        if batch_result.get("paused"):
            result["remaining"] = count_pending_export_events(db)
            return {**result, "status": "paused"}
        events = batch_result["remaining_events"]
        batch_result = process_archive_export_events_batch(db, events, result)
        if batch_result.get("paused"):
            result["remaining"] = count_pending_export_events(db)
            return {**result, "status": "paused"}
        events = batch_result["remaining_events"]

        for event in events:
            event.status = "processing"
            event.attempts = int(event.attempts or 0) + 1
            db.commit()

            try:
                export_result = run_google_sheets_export_event(db, event)
            except Exception as exc:
                if is_google_rate_limit_error(exc):
                    logger.warning("Pending Google Sheets export paused after rate limit: %s", exc)
                    export_result = {"status": "rate_limited", "error": str(exc)}
                    event.status = "pending"
                    event.last_error = format_export_error(export_result)
                    event.payload = {**(event.payload or {}), "last_result": export_result}
                    result["errors"].append({
                        "id": str(event.id),
                        "action": (event.payload or {}).get("action") or "",
                        "entity_id": (event.payload or {}).get("entity_id") or "",
                        "status": export_result["status"],
                        "error": event.last_error,
                    })
                    db.add(AuditLog(
                        action="google_sheets_pending_export_rate_limited",
                        entity_type="pending_event",
                        entity_id=str(event.id),
                        payload={
                            "event_action": (event.payload or {}).get("action") or "",
                            "event_entity_id": (event.payload or {}).get("entity_id") or "",
                            "result": export_result,
                        },
                    ))
                    db.commit()
                    result["remaining"] = count_pending_export_events(db)
                    return {**result, "status": "paused"}
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

        result["remaining"] = count_pending_export_events(db)
        if result["failed"]:
            result["status"] = "completed_with_errors"
        return result
    finally:
        release_google_sheets_export_lock(db)


def process_scan_export_events_batch(db: Session, events, result):
    scan_events = [
        event
        for event in events
        if (event.payload or {}).get("action") == "google_sheets_scan_export"
    ]
    if not scan_events:
        return {"remaining_events": events, "paused": False}

    remaining_events = [event for event in events if event not in scan_events]
    events_by_item_id = {}
    invalid_events = []
    for event in scan_events:
        event.status = "processing"
        event.attempts = int(event.attempts or 0) + 1
        entity_uuid = parse_uuid((event.payload or {}).get("entity_id") or "")
        if entity_uuid is None:
            invalid_events.append(event)
        else:
            events_by_item_id.setdefault(entity_uuid, []).append(event)
    db.commit()

    for event in invalid_events:
        finish_export_event(db, event, {"status": "missing", "error": "invalid order item id"}, result)

    item_ids = list(events_by_item_id)
    if not item_ids:
        return {"remaining_events": remaining_events, "paused": False}

    items = db.execute(
        select(OrderItem)
        .options(selectinload(OrderItem.order), selectinload(OrderItem.scan_codes))
        .where(OrderItem.id.in_(item_ids))
    ).scalars().all()
    items_by_id = {item.id: item for item in items}

    found_events = []
    for item_id, item_events in events_by_item_id.items():
        if item_id not in items_by_id:
            for event in item_events:
                finish_export_event(db, event, {"status": "missing", "error": "order item not found"}, result)
            continue
        found_events.extend(item_events)

    if not items_by_id:
        return {"remaining_events": remaining_events, "paused": False}

    try:
        export_result = sync_backend_order_items_to_google_sheets(items_by_id.values())
    except Exception as exc:
        if is_google_rate_limit_error(exc):
            logger.warning("Pending Google Sheets scan export batch paused after rate limit: %s", exc)
            export_result = {"status": "rate_limited", "error": str(exc)}
            for event in found_events:
                event.status = "pending"
                event.last_error = format_export_error(export_result)
                event.payload = {**(event.payload or {}), "last_result": export_result}
                add_export_audit(db, event, "google_sheets_pending_export_rate_limited", export_result)
            db.commit()
            return {"remaining_events": remaining_events, "paused": True}
        logger.exception("Pending Google Sheets scan export batch failed")
        export_result = {"status": "error", "error": str(exc)}

    missing_export = str((export_result or {}).get("status") or "").strip().lower() == "missing"
    for event in found_events:
        event_result = export_result
        if missing_export:
            entity_uuid = parse_uuid((event.payload or {}).get("entity_id") or "")
            item = items_by_id.get(entity_uuid)
            if item and is_terminal_scan_export_item(item):
                event_result = {
                    "status": "skipped",
                    "error": "order item already left active Google sheet",
                }
        finish_export_event(db, event, event_result, result)
    return {"remaining_events": remaining_events, "paused": False}


def process_archive_export_events_batch(db: Session, events, result):
    archive_events = []
    for event in events:
        if (event.payload or {}).get("action") != "google_sheets_archive_export":
            break
        archive_events.append(event)
    if not archive_events:
        return {"remaining_events": events, "paused": False}

    remaining_events = events[len(archive_events):]
    events_by_order_id = {}
    invalid_events = []
    for event in archive_events:
        event.status = "processing"
        event.attempts = int(event.attempts or 0) + 1
        entity_uuid = parse_uuid((event.payload or {}).get("entity_id") or "")
        if entity_uuid is None:
            invalid_events.append(event)
        else:
            events_by_order_id.setdefault(entity_uuid, []).append(event)
    db.commit()

    for event in invalid_events:
        finish_export_event(db, event, {"status": "missing", "error": "invalid order id"}, result)

    order_ids = list(events_by_order_id)
    if not order_ids:
        return {"remaining_events": remaining_events, "paused": False}

    orders = db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(Order.id.in_(order_ids))
    ).scalars().all()
    orders_by_id = {order.id: order for order in orders}

    found_events = []
    for order_id, order_events in events_by_order_id.items():
        if order_id not in orders_by_id:
            for event in order_events:
                finish_export_event(db, event, {"status": "missing", "error": "order not found"}, result)
            continue
        found_events.extend(order_events)

    if not orders_by_id:
        return {"remaining_events": remaining_events, "paused": False}

    try:
        export_result = archive_backend_orders_to_google_sheets(orders_by_id.values())
    except Exception as exc:
        if is_google_rate_limit_error(exc):
            logger.warning("Pending Google Sheets archive export batch paused after rate limit: %s", exc)
            export_result = {"status": "rate_limited", "error": str(exc)}
            for event in found_events:
                event.status = "pending"
                event.last_error = format_export_error(export_result)
                event.payload = {**(event.payload or {}), "last_result": export_result}
                add_export_audit(db, event, "google_sheets_pending_export_rate_limited", export_result)
            db.commit()
            return {"remaining_events": remaining_events, "paused": True}
        logger.exception("Pending Google Sheets archive export batch failed")
        export_result = {"status": "error", "error": str(exc)}

    order_results = (export_result or {}).get("orders") or {}
    for event in found_events:
        entity_id = str((event.payload or {}).get("entity_id") or "")
        event_result = order_results.get(entity_id) or export_result
        finish_export_event(db, event, event_result, result)
    return {"remaining_events": remaining_events, "paused": False}


def finish_export_event(db: Session, event: PendingEvent, export_result, result):
    status = str((export_result or {}).get("status") or "").strip().lower()
    event.payload = {**(event.payload or {}), "last_result": export_result or {}}
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
            "status": (export_result or {}).get("status") or "error",
            "error": event.last_error,
        })
    add_export_audit(db, event, "google_sheets_pending_export_processed", export_result or {})
    db.commit()


def add_export_audit(db: Session, event: PendingEvent, action, export_result):
    db.add(AuditLog(
        action=action,
        entity_type="pending_event",
        entity_id=str(event.id),
        payload={
            "event_action": (event.payload or {}).get("action") or "",
            "event_entity_id": (event.payload or {}).get("entity_id") or "",
            "result": export_result,
        },
    ))


def is_terminal_scan_export_item(item):
    item_status = str(getattr(item, "status", "") or "").strip().lower()
    order_status = str(getattr(getattr(item, "order", None), "status", "") or "").strip().lower()
    return item_status in SCAN_EXPORT_TERMINAL_STATUSES or order_status in SCAN_EXPORT_TERMINAL_STATUSES


def select_pending_export_events(db: Session, limit):
    stmt = (
        select(PendingEvent)
        .where(PendingEvent.event_type == GOOGLE_SHEETS_EXPORT_EVENT_TYPE)
        .where(PendingEvent.status.in_(("pending", "failed")))
        .order_by(PendingEvent.created_at, PendingEvent.id)
        .limit(limit)
    )
    if db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    return db.execute(stmt).scalars().all()


def reset_stale_processing_export_events(db: Session):
    cutoff = datetime.now(timezone.utc) - STALE_PROCESSING_EXPORT_TIMEOUT
    events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == GOOGLE_SHEETS_EXPORT_EVENT_TYPE)
        .where(PendingEvent.status == "processing")
        .where(PendingEvent.updated_at < cutoff)
    ).scalars().all()
    if not events:
        return 0
    for event in events:
        event.status = "pending"
        event.payload = {
            **(event.payload or {}),
            "last_result": {"status": "reset", "error": "stale processing export reset"},
        }
        db.add(AuditLog(
            action="google_sheets_pending_export_stale_reset",
            entity_type="pending_event",
            entity_id=str(event.id),
            payload={
                "event_action": (event.payload or {}).get("action") or "",
                "event_entity_id": (event.payload or {}).get("entity_id") or "",
            },
        ))
    db.commit()
    return len(events)


def count_pending_export_events(db: Session):
    events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == GOOGLE_SHEETS_EXPORT_EVENT_TYPE)
        .where(PendingEvent.status.in_(("pending", "failed")))
    ).scalars().unique().all()
    return len(events)


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

    if action == "google_sheets_archive_no_kiz_export":
        if entity_uuid is None:
            return {"status": "missing", "error": "invalid order id"}
        order = db.execute(
            select(Order)
            .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
            .where(Order.id == entity_uuid)
        ).scalar_one_or_none()
        if order is None:
            return {"status": "missing", "error": "order not found"}
        return archive_backend_order_without_kiz_to_google_sheets(order)

    if action == "google_sheets_cancel_export":
        if entity_uuid is None:
            return {"status": "missing", "error": "invalid order id"}
        order = db.execute(
            select(Order)
            .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
            .where(Order.id == entity_uuid)
        ).scalar_one_or_none()
        if order is None:
            return {"status": "missing", "error": "order not found"}
        return cancel_backend_order_in_google_sheets(order)

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

    if action == "google_sheets_restore_order_export":
        records = payload.get("records") or []
        if not records:
            return {"status": "missing", "error": "restore records not found"}
        return restore_import_records_to_google_sheets(records)

    if action == "google_sheets_skladbot_export":
        orders = load_skladbot_export_orders(db, payload)
        if not orders:
            return {"status": "skipped", "error": "orders not found"}
        return sync_backend_orders_skladbot_to_google_sheets(
            orders,
            include_archive=bool(payload.get("include_archive")),
        )

    return {"status": "missing", "error": f"unknown google export action: {action}"}


def load_skladbot_export_orders(db: Session, payload):
    order_ids = [parse_uuid(value) for value in (payload.get("order_ids") or [])]
    order_ids = [value for value in order_ids if value is not None]
    include_inactive = bool(payload.get("include_inactive")) and bool(order_ids)
    stmt = (
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
    )
    if not include_inactive:
        stmt = stmt.where(~Order.status.in_(SKLADBOT_EXPORT_INACTIVE_ORDER_STATUSES))
    if order_ids:
        stmt = stmt.where(Order.id.in_(order_ids))
    return db.execute(stmt).scalars().all()


def acquire_google_sheets_export_lock(db: Session):
    if db.bind.dialect.name == "postgresql":
        return True
    return LOCAL_EXPORT_LOCK.acquire(blocking=False)


def release_google_sheets_export_lock(db: Session):
    if db.bind.dialect.name == "postgresql":
        return
    if LOCAL_EXPORT_LOCK.locked():
        LOCAL_EXPORT_LOCK.release()


def format_export_error(result):
    if not result:
        return ""
    error = str(result.get("error") or "").strip()
    if error:
        return error
    status = str(result.get("status") or "").strip()
    if status in {"queued", "completed", "skipped"}:
        return ""
    return f"Google Sheets export status: {status}" if status else ""


def is_google_rate_limit_error(exc):
    message = str(exc or "").casefold()
    return "429" in message or "quota" in message or "rate limit" in message or "rate_limit" in message


def parse_uuid(value):
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
