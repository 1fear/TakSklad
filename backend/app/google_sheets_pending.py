import hashlib
import json
import logging
import re
from threading import Lock
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .event_leases import claim_event_leases, event_leases_enabled, finalize_event_leases
from .google_sheets_exporter import (
    append_import_records_to_google_sheets,
    archive_backend_order_to_google_sheets,
    archive_backend_orders_to_google_sheets,
    archive_backend_order_without_kiz_to_google_sheets,
    cancel_backend_order_in_google_sheets,
    delete_import_records_from_google_sheets,
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
PAYLOAD_IDEMPOTENT_EXPORT_ACTIONS = {
    "google_sheets_import_export",
    "google_sheets_restore_order_export",
    "google_sheets_delete_import_records_export",
}


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
    idempotency_key = google_sheets_export_idempotency_key(action, entity_type, entity_id, payload)
    if idempotency_key:
        event_payload["idempotency_key"] = idempotency_key
        existing_by_key = db.execute(
            select(PendingEvent).where(PendingEvent.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing_by_key is not None:
            if existing_by_key.status in ("pending", "failed"):
                existing_by_key.payload = {**(existing_by_key.payload or {}), **event_payload}
                existing_by_key.status = "pending"
                existing_by_key.available_at = datetime.now(timezone.utc)
                existing_by_key.lease_owner = None
                existing_by_key.lease_expires_at = None
                existing_by_key.completed_at = None
                existing_by_key.last_error = format_export_error(result)
            return existing_by_key

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
        existing.available_at = datetime.now(timezone.utc)
        existing.lease_owner = None
        existing.lease_expires_at = None
        existing.completed_at = None
        existing.last_error = format_export_error(result)
        return existing

    event = PendingEvent(
        event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
        idempotency_key=idempotency_key or None,
        status="pending",
        attempts=0,
        payload=event_payload,
        last_error=format_export_error(result),
    )
    db.add(event)
    db.flush()
    return event


def google_sheets_export_idempotency_key(action, entity_type, entity_id, payload):
    payload = payload or {}
    explicit = str(payload.get("idempotency_key") or "").strip()
    if explicit:
        return explicit
    if action not in PAYLOAD_IDEMPOTENT_EXPORT_ACTIONS:
        return ""
    payload_hash = stable_payload_hash(payload)
    return f"google_sheets:{action}:{entity_type}:{entity_id}:{payload_hash}"


def stable_payload_hash(payload):
    encoded = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
        if event_leases_enabled():
            events = claim_event_leases(
                db,
                event_types=(GOOGLE_SHEETS_EXPORT_EVENT_TYPE,),
                owner=f"google-sheets:{uuid.uuid4()}",
                limit=limit,
            )
        else:
            reset_stale_processing_export_events(db)
            events = select_pending_export_events(db, limit)
        result["checked"] = len(events)
        if not events:
            result["remaining"] = count_pending_export_events(db)
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
            if not event.lease_owner:
                event.status = "processing"
                event.attempts = int(event.attempts or 0) + 1
                db.commit()

            try:
                export_result = run_google_sheets_export_event(db, event)
            except Exception as exc:
                if is_google_rate_limit_error(exc):
                    logger.warning("Pending Google Sheets export paused after rate limit: %s", exc)
                    export_result = {"status": "rate_limited", "error": str(exc)}
                    event_error = format_export_error(export_result)
                    event_payload = with_retry_after_payload(event.payload, exc, export_result)
                    result["errors"].append({
                        "id": str(event.id),
                        "action": (event.payload or {}).get("action") or "",
                        "entity_id": (event.payload or {}).get("entity_id") or "",
                        "status": export_result["status"],
                        "error": event_error,
                    })
                    db.add(AuditLog(
                        action="google_sheets_pending_export_rate_limited",
                        entity_type="pending_event",
                        entity_id=str(event.id),
                        payload={
                            "event_action": event_payload.get("action") or "",
                            "event_entity_id": event_payload.get("entity_id") or "",
                            "result": export_result,
                        },
                    ))
                    save_export_event_outcome(
                        db, event, status="pending", last_error=event_error,
                        payload=event_payload, available_at=retry_available_at(event_payload),
                    )
                    result["remaining"] = count_pending_export_events(db)
                    return {**result, "status": "paused"}
                logger.exception("Pending Google Sheets export failed")
                export_result = {"status": "error", "error": str(exc)}

            status = str(export_result.get("status") or "").strip().lower()
            event_payload = without_retry_after_payload({**(event.payload or {}), "last_result": export_result})
            if status in SUCCESS_EXPORT_STATUSES:
                final_status = "completed"
                event_error = ""
                result["synced"] += 1
            else:
                final_status = "failed"
                event_error = format_export_error(export_result)
                result["failed"] += 1
                result["errors"].append({
                    "id": str(event.id),
                    "action": (event.payload or {}).get("action") or "",
                    "entity_id": (event.payload or {}).get("entity_id") or "",
                    "status": export_result.get("status") or "error",
                    "error": event_error,
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
            save_export_event_outcome(
                db, event, status=final_status, last_error=event_error, payload=event_payload,
            )

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
    legacy_marked = False
    for event in scan_events:
        if not event.lease_owner:
            event.status = "processing"
            event.attempts = int(event.attempts or 0) + 1
            legacy_marked = True
        entity_uuid = parse_uuid((event.payload or {}).get("entity_id") or "")
        if entity_uuid is None:
            invalid_events.append(event)
        else:
            events_by_item_id.setdefault(entity_uuid, []).append(event)
    if legacy_marked:
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
                event_error = format_export_error(export_result)
                event_payload = with_retry_after_payload(event.payload, exc, export_result)
                add_export_audit(db, event, "google_sheets_pending_export_rate_limited", export_result)
                save_export_event_outcome(
                    db, event, status="pending", last_error=event_error,
                    payload=event_payload, available_at=retry_available_at(event_payload),
                )
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
    legacy_marked = False
    for event in archive_events:
        if not event.lease_owner:
            event.status = "processing"
            event.attempts = int(event.attempts or 0) + 1
            legacy_marked = True
        entity_uuid = parse_uuid((event.payload or {}).get("entity_id") or "")
        if entity_uuid is None:
            invalid_events.append(event)
        else:
            events_by_order_id.setdefault(entity_uuid, []).append(event)
    if legacy_marked:
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
                event_error = format_export_error(export_result)
                event_payload = with_retry_after_payload(event.payload, exc, export_result)
                add_export_audit(db, event, "google_sheets_pending_export_rate_limited", export_result)
                save_export_event_outcome(
                    db, event, status="pending", last_error=event_error,
                    payload=event_payload, available_at=retry_available_at(event_payload),
                )
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
    event_payload = without_retry_after_payload({**(event.payload or {}), "last_result": export_result or {}})
    if status in SUCCESS_EXPORT_STATUSES:
        final_status = "completed"
        event_error = ""
        result["synced"] += 1
    else:
        final_status = "failed"
        event_error = format_export_error(export_result)
        result["failed"] += 1
        result["errors"].append({
            "id": str(event.id),
            "action": (event.payload or {}).get("action") or "",
            "entity_id": (event.payload or {}).get("entity_id") or "",
            "status": (export_result or {}).get("status") or "error",
            "error": event_error,
        })
    add_export_audit(db, event, "google_sheets_pending_export_processed", export_result or {})
    save_export_event_outcome(
        db, event, status=final_status, last_error=event_error, payload=event_payload,
    )


def save_export_event_outcome(db, event, *, status, last_error, payload, available_at=None):
    if event.lease_owner:
        finalize_event_leases(
            db,
            event_ids=(event.id,),
            owner=event.lease_owner,
            status=status,
            last_error=last_error,
            payload=payload,
            available_at=available_at,
        )
        return
    event.status = status
    event.last_error = last_error
    event.payload = payload
    event.available_at = available_at or datetime.now(timezone.utc)
    event.completed_at = datetime.now(timezone.utc) if status in {"completed", "blocked"} else None
    db.commit()


def retry_available_at(payload):
    value = str((payload or {}).get("next_attempt_at") or "").strip()
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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
    query_limit = min(max(limit * 5, limit), 1000)
    stmt = (
        select(PendingEvent)
        .where(PendingEvent.event_type == GOOGLE_SHEETS_EXPORT_EVENT_TYPE)
        .where(PendingEvent.status.in_(("pending", "failed")))
        .where(PendingEvent.available_at <= datetime.now(timezone.utc))
        .order_by(PendingEvent.created_at, PendingEvent.id)
        .limit(query_limit)
    )
    if db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    now = datetime.now(timezone.utc)
    ready_events = [
        event
        for event in db.execute(stmt).scalars().all()
        if google_sheets_export_event_ready(event, now=now)
    ]
    return ready_events[:limit]


def google_sheets_export_event_ready(event: PendingEvent, now=None):
    payload = event.payload or {}
    next_attempt_at = str(payload.get("next_attempt_at") or "").strip()
    if not next_attempt_at:
        return True
    now = now or datetime.now(timezone.utc)
    try:
        next_attempt = datetime.fromisoformat(next_attempt_at)
    except ValueError:
        return True
    if next_attempt.tzinfo is None:
        next_attempt = next_attempt.replace(tzinfo=timezone.utc)
    return next_attempt <= now


def google_sheets_export_cooldown_until(db: Session, now=None):
    now = now or datetime.now(timezone.utc)
    attempts = []
    events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == GOOGLE_SHEETS_EXPORT_EVENT_TYPE)
        .where(PendingEvent.status.in_(("pending", "failed")))
    ).scalars().all()
    for event in events:
        payload = event.payload or {}
        next_attempt_at = str(payload.get("next_attempt_at") or "").strip()
        if not next_attempt_at:
            continue
        try:
            next_attempt = datetime.fromisoformat(next_attempt_at)
        except ValueError:
            continue
        if next_attempt.tzinfo is None:
            next_attempt = next_attempt.replace(tzinfo=timezone.utc)
        if next_attempt > now:
            attempts.append(next_attempt)
    return min(attempts) if attempts else None


def reset_stale_processing_export_events(db: Session):
    now = datetime.now(timezone.utc)
    cutoff = now - STALE_PROCESSING_EXPORT_TIMEOUT
    events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == GOOGLE_SHEETS_EXPORT_EVENT_TYPE)
        .where(PendingEvent.status == "processing")
        .where(PendingEvent.updated_at < cutoff)
        .where((PendingEvent.lease_owner.is_(None)) | (PendingEvent.lease_expires_at <= now))
    ).scalars().all()
    if not events:
        return 0
    for event in events:
        event.status = "pending"
        event.available_at = now
        event.lease_owner = None
        event.lease_expires_at = None
        event.completed_at = None
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

    if action == "google_sheets_delete_import_records_export":
        records = payload.get("records") or []
        if not records:
            return {"status": "missing", "error": "delete records not found"}
        return delete_import_records_from_google_sheets(records)

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


def with_retry_after_payload(payload, exc, export_result):
    next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds_from_error(exc))
    return {
        **(payload or {}),
        "last_result": export_result,
        "next_attempt_at": next_attempt_at.isoformat(),
    }


def without_retry_after_payload(payload):
    payload = dict(payload or {})
    payload.pop("next_attempt_at", None)
    return payload


def retry_after_seconds_from_error(exc, default=60):
    text = str(exc or "")
    match = re.search(r"retry-after\s*[:=]\s*(\d+)", text, re.IGNORECASE)
    if match:
        return max(1, int(match.group(1)))
    return default


def parse_uuid(value):
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
