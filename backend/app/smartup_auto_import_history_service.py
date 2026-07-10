from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .admin_service import sanitize_payload
from .event_queue_service import event_to_queue_read
from .models import AuditLog, PendingEvent
from .schemas import AdminActivityRead
from .smartup_auto_import import SMARTUP_AUTO_IMPORT_EVENT_TYPE


DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 200


def list_smartup_auto_import_history(db: Session, limit: int | None = None) -> dict[str, Any]:
    limit_value = normalize_limit(limit)
    generated_at = datetime.now(timezone.utc)
    events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == SMARTUP_AUTO_IMPORT_EVENT_TYPE)
        .order_by(PendingEvent.created_at.desc(), PendingEvent.id.desc())
        .limit(limit_value)
    ).scalars().all()
    audit_rows = db.execute(
        select(AuditLog)
        .where(or_(
            AuditLog.entity_type == "smartup_auto_import",
            AuditLog.action.like("smartup_auto_import%"),
        ))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit_value)
    ).scalars().all()
    runs = [smartup_event_to_run_read(event) for event in events]
    return {
        "generated_at": generated_at,
        "summary": build_history_summary(runs),
        "runs": runs,
        "events": [event_to_queue_read(event, generated_at) for event in events],
        "audit": [
            AdminActivityRead(
                id=str(row.id),
                action=row.action,
                entity_type=row.entity_type or "",
                entity_id=row.entity_id or "",
                actor_subject=row.actor_subject or "",
                actor_user_id=str(row.actor_user_id or ""),
                actor_service_principal_id=str(row.actor_service_principal_id or ""),
                payload=sanitize_history_payload(row.payload),
                created_at=row.created_at,
            )
            for row in audit_rows
        ],
    }


def smartup_event_to_run_read(event: PendingEvent) -> dict[str, Any]:
    payload = event.payload or {}
    result = record_field(payload, "result")
    imports = list_field(result, "imports")
    status_change = record_field(result, "status_change")
    skladbot_processing = record_field(result, "skladbot_processing")
    return {
        "id": str(event.id),
        "status": event.status,
        "export_date": string_field(result, "export_date") or string_field(payload, "export_date"),
        "slot": string_field(result, "slot") or string_field(payload, "slot"),
        "part": int_field(result, "part") or None,
        "filename": string_field(result, "filename"),
        "export_path": string_field(result, "export_path"),
        "audit_path": string_field(result, "audit_path"),
        "selected_orders": int_field(result, "selected_orders"),
        "rows": int_field(result, "rows"),
        "delivery_dates": string_list_field(result, "delivery_dates"),
        "imports_count": len(imports),
        "orders_created": sum(int_field(item, "orders_created") for item in imports),
        "items_created": sum(int_field(item, "items_created") for item in imports),
        "duplicate_rows": sum(int_field(item, "duplicate_rows") for item in imports),
        "status_change_submitted": int_field(status_change, "submitted"),
        "skladbot_status": string_field(skladbot_processing, "status"),
        "logistics_reports": list_field(result, "logistics_reports"),
        "error": string_field(payload, "error") or str(event.last_error or ""),
        "created_at": event.created_at,
        "updated_at": event.updated_at,
        "completed_at": string_field(payload, "completed_at"),
        "failed_at": string_field(payload, "failed_at"),
    }


def build_history_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for run in runs:
        status = string_field(run, "status") or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
    last_run = runs[0] if runs else {}
    return {
        "total": len(runs),
        "completed": status_counts.get("completed", 0),
        "failed": status_counts.get("failed", 0),
        "processing": status_counts.get("processing", 0),
        "by_status": status_counts,
        "orders_created": sum(int_field(run, "orders_created") for run in runs),
        "items_created": sum(int_field(run, "items_created") for run in runs),
        "last_status": string_field(last_run, "status"),
        "last_export_date": string_field(last_run, "export_date"),
        "last_slot": string_field(last_run, "slot"),
    }


def normalize_limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_HISTORY_LIMIT
    try:
        return min(MAX_HISTORY_LIMIT, max(1, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_HISTORY_LIMIT


def sanitize_history_payload(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in {"token", "password", "secret", "authorization", "telegram_bot_token"}:
                result[key_text] = "***"
            else:
                result[key_text] = sanitize_history_payload(item)
        return result
    if isinstance(value, list):
        return [sanitize_history_payload(item) for item in value]
    return sanitize_payload(value)


def record_field(record: Any, key: str) -> dict[str, Any]:
    if isinstance(record, dict) and isinstance(record.get(key), dict):
        return record[key]
    return {}


def list_field(record: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(record, dict) or not isinstance(record.get(key), list):
        return []
    return [item for item in record[key] if isinstance(item, dict)]


def string_list_field(record: Any, key: str) -> list[str]:
    if not isinstance(record, dict) or not isinstance(record.get(key), list):
        return []
    return [str(item) for item in record[key] if str(item or "").strip()]


def string_field(record: Any, key: str) -> str:
    if not isinstance(record, dict):
        return ""
    value = record.get(key)
    return str(value or "").strip() if value is not None else ""


def int_field(record: Any, key: str) -> int:
    if not isinstance(record, dict):
        return 0
    value = record.get(key)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
