import json
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .event_queue_service import list_event_queue_diagnostics
from .models import AuditLog, ImportJob
from .redaction import redact_secrets


DIAGNOSTIC_AUDIT_ACTIONS = {
    "orders_imported",
    "skladbot_worker_sync",
    "skladbot_google_sheets_export",
    "order_returned",
    "order_archived_without_kiz",
    "order_cancelled",
    "order_deleted_from_active",
    "order_completed_without_kiz",
    "order_reset_for_rescan",
    "order_restored",
    "order_google_resync_requested",
    "order_skladbot_resync_requested",
}


def compact_json(value):
    try:
        return redact_secrets(json.dumps(value or {}, ensure_ascii=False, sort_keys=True))
    except TypeError:
        return redact_secrets(value)


def build_backend_diagnostics_log(db: Session, limit=100):
    limit = max(1, min(int(limit or 100), 500))
    now = datetime.now(timezone.utc)
    queue_diagnostics = list_event_queue_diagnostics(db, limit=limit)
    lines = [
        "TakSklad backend diagnostics",
        f"Generated at: {now.isoformat()}",
        "",
        "Event Queue Summary",
        "-------------------",
    ]
    summary = queue_diagnostics.get("summary") or {}
    by_type = summary.get("by_type") or {}
    if not by_type:
        lines.append("none")
    for event_type, statuses in sorted(by_type.items()):
        status_text = ", ".join(f"{status}={count}" for status, count in sorted((statuses or {}).items()))
        lines.append(f"{event_type}: {status_text}")
    lines.extend([
        f"total={summary.get('total', 0)} active={summary.get('active', 0)} terminal={summary.get('terminal', 0)}",
        "",
        "Failed/Pending Events",
        "---------------------",
    ])

    visible_statuses = {
        "pending",
        "processing",
        "failed",
        "error",
        "blocked",
        "waiting_shipment_date",
        "waiting_date_choice",
    }
    events = [
        event
        for event in queue_diagnostics.get("recent_events") or []
        if str(event.get("status") or "") in visible_statuses
    ]
    if not events:
        lines.append("none")
    for event in events:
        lines.append(
            " | ".join([
                str(event.get("updated_at") or event.get("created_at") or ""),
                f"type={event.get('event_type') or ''}",
                f"status={event.get('status') or ''}",
                f"attempts={event.get('attempts', 0)}",
                f"idempotency_key={redact_secrets(event.get('idempotency_key') or '')}",
                f"next_attempt_at={event.get('next_attempt_at') or ''}",
                f"age_seconds={event.get('age_seconds', 0)}",
                f"error={redact_secrets(event.get('last_error') or '')}",
            ])
        )
    stale_events = queue_diagnostics.get("stale_processing") or []
    lines.extend(["", "Stale Processing Events", "-----------------------"])
    if not stale_events:
        lines.append("none")
    for event in stale_events:
        lines.append(
            " | ".join([
                str(event.get("updated_at") or event.get("created_at") or ""),
                f"type={event.get('event_type') or ''}",
                f"attempts={event.get('attempts', 0)}",
                f"idempotency_key={redact_secrets(event.get('idempotency_key') or '')}",
                f"age_seconds={event.get('age_seconds', 0)}",
                f"error={redact_secrets(event.get('last_error') or '')}",
            ])
        )
    lines.extend(["", "Import Errors", "-------------"])

    imports = db.execute(
        select(ImportJob)
        .where(ImportJob.status.in_(("failed", "completed_with_errors")))
        .order_by(desc(ImportJob.created_at))
        .limit(limit)
    ).scalars().all()
    if not imports:
        lines.append("none")
    for item in imports:
        payload = dict(item.raw_payload or {})
        errors = payload.get("errors") or []
        lines.append(
            " | ".join([
                str(item.created_at or ""),
                f"status={item.status}",
                f"source={item.source}",
                f"filename={redact_secrets(payload.get('filename'))}",
                f"rows={item.rows_imported}/{item.rows_total}",
                f"invalid={payload.get('invalid_rows', 0)}",
                f"duplicates={payload.get('duplicate_rows', 0)}",
            ])
        )
        for error in errors[:5]:
            lines.append(f"  - {redact_secrets(error)}")
    lines.extend(["", "Recent Operational Audit", "------------------------"])

    audit_logs = db.execute(
        select(AuditLog)
        .where(AuditLog.action.in_(DIAGNOSTIC_AUDIT_ACTIONS))
        .order_by(desc(AuditLog.created_at))
        .limit(limit)
    ).scalars().all()
    if not audit_logs:
        lines.append("none")
    for audit in audit_logs:
        lines.append(
            " | ".join([
                str(audit.created_at or ""),
                f"action={audit.action}",
                f"entity={audit.entity_type or ''}:{audit.entity_id or ''}",
                f"payload={compact_json(audit.payload)}",
            ])
        )

    content = "\n".join(lines).encode("utf-8")
    filename = f"TakSklad_backend_diagnostics_{now.strftime('%Y-%m-%d_%H%M%S')}.txt"
    return content, filename
