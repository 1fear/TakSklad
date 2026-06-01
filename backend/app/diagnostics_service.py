import json
import re
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .models import AuditLog, ImportJob, PendingEvent


SECRET_PATTERNS = [
    re.compile(r"(bot\d+:[A-Za-z0-9_-]+)"),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"(token|password|secret|authorization)([\"'=:\s]+)([^\"'\s,}]+)", re.IGNORECASE),
]
DIAGNOSTIC_AUDIT_ACTIONS = {
    "orders_imported",
    "skladbot_worker_sync",
    "skladbot_google_sheets_export",
    "order_returned",
}


def redact_secrets(value):
    text = str(value or "")
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 3:
            text = pattern.sub(r"\1\2***", text)
        elif pattern.groups == 2:
            text = pattern.sub(r"\1***", text)
        elif pattern.groups == 1:
            text = pattern.sub(r"\1***", text)
        else:
            text = pattern.sub("***", text)
    return text


def compact_json(value):
    try:
        return redact_secrets(json.dumps(value or {}, ensure_ascii=False, sort_keys=True))
    except TypeError:
        return redact_secrets(value)


def build_backend_diagnostics_log(db: Session, limit=100):
    limit = max(1, min(int(limit or 100), 500))
    now = datetime.now(timezone.utc)
    lines = [
        "TakSklad backend diagnostics",
        f"Generated at: {now.isoformat()}",
        "",
        "Failed/Pending Events",
        "---------------------",
    ]

    failed_events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.status.in_(("failed", "error")))
        .order_by(desc(PendingEvent.updated_at), desc(PendingEvent.created_at))
        .limit(limit)
    ).scalars().all()
    if not failed_events:
        lines.append("none")
    for event in failed_events:
        lines.append(
            " | ".join([
                str(event.updated_at or event.created_at or ""),
                f"type={event.event_type}",
                f"status={event.status}",
                f"attempts={event.attempts}",
                f"error={redact_secrets(event.last_error)}",
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
