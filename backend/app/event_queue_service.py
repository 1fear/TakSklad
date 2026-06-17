from collections import defaultdict
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from .models import AuditLog, PendingEvent
from .redaction import redact_secrets


EVENT_QUEUE_ACTIVE_STATUSES = ("pending", "failed", "processing")
EVENT_QUEUE_TERMINAL_STATUSES = ("completed", "blocked", "dead", "cancelled")
EVENT_QUEUE_RETRYABLE_TYPES = (
    "google_sheets_export",
    "telegram_excel_import",
    "telegram_notification",
    "skladbot_request_create",
    "skladbot_return_request_create",
    "skladbot_daily_report_send",
)
EVENT_QUEUE_RETRYABLE_STATUSES = ("failed", "pending")
STALE_PROCESSING_TIMEOUT = timedelta(minutes=10)


class EventQueueApiError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def list_event_queue_diagnostics(db: Session, limit=100):
    limit = max(1, min(int(limit or 100), 500))
    now = datetime.now(timezone.utc)
    summary = build_event_queue_summary(db)
    stale_processing = list_stale_processing_events(db, now=now, limit=limit)
    recent_events = db.execute(
        select(PendingEvent)
        .order_by(desc(PendingEvent.updated_at), desc(PendingEvent.created_at), desc(PendingEvent.id))
        .limit(limit)
    ).scalars().all()
    return {
        "generated_at": now.isoformat(),
        "summary": summary,
        "stale_processing": [event_to_queue_read(event, now=now) for event in stale_processing],
        "recent_events": [event_to_queue_read(event, now=now) for event in recent_events],
    }


def build_event_queue_summary(db: Session):
    rows = db.execute(
        select(PendingEvent.event_type, PendingEvent.status, func.count(PendingEvent.id))
        .group_by(PendingEvent.event_type, PendingEvent.status)
        .order_by(PendingEvent.event_type, PendingEvent.status)
    ).all()
    by_type = defaultdict(lambda: defaultdict(int))
    total = 0
    for event_type, status, count in rows:
        count = int(count or 0)
        by_type[event_type or ""][(status or "")] += count
        total += count
    return {
        "total": total,
        "active": sum(sum(statuses.get(status, 0) for status in EVENT_QUEUE_ACTIVE_STATUSES) for statuses in by_type.values()),
        "terminal": sum(sum(statuses.get(status, 0) for status in EVENT_QUEUE_TERMINAL_STATUSES) for statuses in by_type.values()),
        "by_type": {
            event_type: dict(statuses)
            for event_type, statuses in sorted(by_type.items())
        },
    }


def list_stale_processing_events(db: Session, now=None, limit=100):
    now = now or datetime.now(timezone.utc)
    cutoff = now - STALE_PROCESSING_TIMEOUT
    return db.execute(
        select(PendingEvent)
        .where(PendingEvent.status == "processing")
        .where(PendingEvent.updated_at < cutoff)
        .order_by(PendingEvent.updated_at, PendingEvent.created_at)
        .limit(limit)
    ).scalars().all()


def reset_stale_processing_events(
    db: Session,
    *,
    event_types,
    action,
    last_error,
    now=None,
    timeout=STALE_PROCESSING_TIMEOUT,
):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timeout
    event_types = tuple(event_types or ())
    if not event_types:
        return 0
    events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type.in_(event_types))
        .where(PendingEvent.status == "processing")
        .where(PendingEvent.updated_at < cutoff)
    ).scalars().all()
    for event in events:
        event.status = "pending"
        event.last_error = last_error
        event.payload = {
            **(event.payload or {}),
            "reset_at": now.isoformat(),
            "reset_reason": last_error,
        }
        db.add(AuditLog(
            action=action,
            entity_type="pending_event",
            entity_id=str(event.id),
            payload={
                "event_type": event.event_type,
                "idempotency_key": event.idempotency_key or "",
                "attempts": int(event.attempts or 0),
            },
        ))
    if events:
        db.commit()
    return len(events)


def get_event_queue_detail(db: Session, event_id):
    event = db.get(PendingEvent, parse_required_uuid(event_id, "event_id"))
    if event is None:
        raise EventQueueApiError(404, "Event not found")
    return event_to_queue_read(event)


def retry_event_queue_event(db: Session, event_id, payload):
    event = db.get(PendingEvent, parse_required_uuid(event_id, "event_id"))
    if event is None:
        raise EventQueueApiError(404, "Event not found")
    if not is_retryable_event(event):
        raise EventQueueApiError(409, {
            "message": "Event is not retryable",
            "event_type": event.event_type,
            "status": event.status,
        })

    reason = normalize_required(getattr(payload, "reason", ""), "reason")
    ensure_event_retry_source_available(event)
    actor = normalize_text(getattr(payload, "actor", "")) or "web"
    source = normalize_text(getattr(payload, "source", "")) or actor
    now = datetime.now(timezone.utc)
    old_status = event.status
    current_payload = dict(event.payload or {})
    current_payload.pop("next_attempt_at", None)
    current_payload.update({
        "manual_retry_at": now.isoformat(),
        "manual_retry_reason": reason,
        "manual_retry_actor": actor,
        "manual_retry_source": source,
    })
    event.status = "pending"
    event.last_error = ""
    event.payload = current_payload
    db.add(AuditLog(
        action="pending_event_retry_requested",
        entity_type="pending_event",
        entity_id=str(event.id),
        payload={
            "event_type": event.event_type,
            "old_status": old_status,
            "new_status": event.status,
            "attempts": int(event.attempts or 0),
            "actor": actor,
            "source": source,
            "reason": reason,
            "idempotency_key": normalize_text(getattr(payload, "idempotency_key", "")),
        },
    ))
    db.commit()
    db.refresh(event)
    return event_to_queue_read(event)


def event_to_queue_read(event: PendingEvent, now=None):
    now = now or datetime.now(timezone.utc)
    payload = event.payload or {}
    updated_at = ensure_aware_utc(event.updated_at or event.created_at)
    age_seconds = int(max(0, (now - updated_at).total_seconds())) if updated_at else 0
    return {
        "id": str(event.id),
        "event_type": event.event_type,
        "status": event.status,
        "attempts": int(event.attempts or 0),
        "last_error": redact_secrets(event.last_error or ""),
        "idempotency_key": event.idempotency_key or "",
        "next_attempt_at": str(payload.get("next_attempt_at") or ""),
        "payload_status": str(payload.get("create_status") or payload.get("status") or ""),
        "retryable": is_retryable_event(event),
        "linked_order_id": linked_value(payload, "order_id", "order_ids"),
        "linked_import_id": linked_value(payload, "import_id"),
        "linked_entity_type": str(payload.get("entity_type") or ""),
        "linked_entity_id": linked_value(payload, "entity_id"),
        "raw_payload": redact_payload(payload),
        "age_seconds": age_seconds,
        "created_at": event.created_at,
        "updated_at": event.updated_at,
    }


def is_retryable_event(event: PendingEvent):
    return (
        event.event_type in EVENT_QUEUE_RETRYABLE_TYPES
        and event.status in EVENT_QUEUE_RETRYABLE_STATUSES
    )


def ensure_event_retry_source_available(event: PendingEvent):
    if event.event_type != "telegram_excel_import":
        return
    payload = event.payload or {}
    document = payload.get("document") if isinstance(payload, dict) else {}
    if isinstance(document, dict) and normalize_text(document.get("file_id")):
        return
    raise EventQueueApiError(409, {
        "message": "Original Telegram file is unavailable for retry",
        "event_type": event.event_type,
        "status": event.status,
    })


def linked_value(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return ", ".join(str(item) for item in value[:3])
        if value:
            return str(value)
    return ""


def redact_payload(value):
    if isinstance(value, dict):
        return {
            key: "***" if is_secret_key(key) else redact_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value


def is_secret_key(key):
    normalized = str(key or "").casefold()
    return any(marker in normalized for marker in ("token", "password", "secret", "authorization"))


def normalize_text(value):
    return str(value or "").strip()


def normalize_required(value, field):
    text = normalize_text(value)
    if not text:
        raise EventQueueApiError(422, f"{field} is required")
    return text


def parse_required_uuid(value, field):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise EventQueueApiError(422, f"Invalid {field}") from exc


def ensure_aware_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
