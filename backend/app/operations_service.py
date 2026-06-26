from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .health_service import build_readiness_report, event_age_seconds
from .models import ImportJob, Incident, PendingEvent
from .redaction import redact_secrets


TERMINAL_INCIDENT_STATUSES = ("resolved", "ignored", "cancelled")
ACTIVE_EVENT_STATUSES = ("pending", "failed", "processing", "blocked", "error")


def build_operations_attention(db: Session, app_settings):
    now = datetime.now(timezone.utc)
    readiness = build_readiness_report(db, app_settings)
    items = []
    items.extend(queue_attention_items(db, now=now))
    items.extend(incident_attention_items(db, now=now))
    items.extend(import_attention_items(db, now=now))
    items.sort(key=lambda item: (severity_order(item["severity"]), -int(item["oldest_age_seconds"] or 0), item["category"]))
    summary = {
        "total": len(items),
        "hot_path": sum(1 for item in items if item["impact"] == "hot_path"),
        "mirror": sum(1 for item in items if item["impact"] == "mirror"),
        "telegram": sum(1 for item in items if item["category"] == "telegram"),
        "incidents": sum(1 for item in items if item["category"] == "incident"),
        "imports": sum(1 for item in items if item["category"] == "import"),
    }
    return {
        "generated_at": now.isoformat(),
        "status": "requires_attention" if items else "ok",
        "summary": summary,
        "items": items,
        "readiness_status": readiness.get("status") or "",
        "google_mirror_status": (readiness.get("google_mirror") or {}).get("status") or "",
        "shadow_diagnostics": build_shadow_diagnostics(readiness),
        "telegram_summary": build_operations_telegram_summary(items),
    }


def build_shadow_diagnostics(readiness):
    readiness = readiness if isinstance(readiness, dict) else {}
    queue = readiness.get("queue") if isinstance(readiness.get("queue"), dict) else {}
    google_mirror = (
        readiness.get("google_mirror")
        if isinstance(readiness.get("google_mirror"), dict)
        else {}
    )
    mirror_summary = google_mirror.get("summary") if isinstance(google_mirror.get("summary"), dict) else {}
    by_type = queue.get("summary", {}).get("by_type", {}) if isinstance(queue.get("summary"), dict) else {}
    telegram_events = count_event_types(
        by_type,
        ("telegram_excel_import", "telegram_notification"),
    )
    return {
        "backend_active_orders_source": "postgres_backend",
        "readiness_status": readiness.get("status") or "",
        "google_mirror_status": google_mirror.get("status") or "",
        "google_mirror_lag_seconds": int(google_mirror.get("oldest_pending_age_seconds") or 0),
        "google_mirror_pending_exports": int(mirror_summary.get("pending") or 0),
        "google_mirror_failed_exports": int(mirror_summary.get("failed") or 0),
        "google_mirror_processing_exports": int(mirror_summary.get("processing") or 0),
        "google_mirror_paused": bool(google_mirror.get("paused")),
        "queue_stale_processing": int(queue.get("stale_processing_count") or 0),
        "hot_path_stale_processing": int(queue.get("hot_path_stale_processing_count") or 0),
        "telegram_worker_state": "requires_attention" if telegram_events else "ok",
        "telegram_pending_events": telegram_events,
    }


def count_event_types(by_type, event_types):
    by_type = by_type if isinstance(by_type, dict) else {}
    total = 0
    for event_type in event_types:
        statuses = by_type.get(event_type) if isinstance(by_type.get(event_type), dict) else {}
        total += sum(int(value or 0) for value in statuses.values())
    return total


def queue_attention_items(db: Session, now):
    events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.status.in_(ACTIVE_EVENT_STATUSES))
        .order_by(PendingEvent.created_at, PendingEvent.id)
    ).scalars().all()
    grouped = {}
    for event in events:
        category, impact, title, next_action = classify_event(event)
        key = (category, impact, title, next_action)
        item = grouped.setdefault(key, {
            "category": category,
            "impact": impact,
            "severity": "warning" if impact == "mirror" else "critical",
            "title": title,
            "count": 0,
            "oldest_age_seconds": 0,
            "next_action": next_action,
            "details": [],
        })
        item["count"] += 1
        age = event_age_seconds(event, now, field="created_at")
        item["oldest_age_seconds"] = max(item["oldest_age_seconds"], age)
        if len(item["details"]) < 3:
            item["details"].append(event_detail(event))
    return list(grouped.values())


def classify_event(event):
    event_type = str(event.event_type or "")
    payload = event.payload if isinstance(event.payload, dict) else {}
    action = str(payload.get("action") or "")
    if event_type == "google_sheets_export":
        return (
            "google_mirror",
            "mirror",
            "Google mirror export lag",
            "Проверить Google quota/доступ; retry export в admin events после восстановления.",
        )
    if event_type in {"telegram_excel_import", "telegram_notification"}:
        return (
            "telegram",
            "hot_path",
            "Telegram processing requires attention",
            "Открыть admin events/incidents, проверить файл/уведомление и retry только после проверки причины.",
        )
    if event_type in {"skladbot_request_create", "skladbot_return_request_create", "skladbot_daily_report_send"}:
        return (
            "skladbot",
            "hot_path",
            "SkladBot queue requires attention",
            "Проверить SkladBot stock/status, затем retry события из admin events.",
        )
    if action:
        return (
            "queue",
            "hot_path",
            f"Queue action requires attention: {action}",
            "Открыть admin events, проверить last_error и retry/resolve после ручной проверки.",
        )
    return (
        "queue",
        "hot_path",
        f"Queue event requires attention: {event_type or 'unknown'}",
        "Открыть admin events, проверить last_error и retry/resolve после ручной проверки.",
    )


def event_detail(event):
    payload = event.payload if isinstance(event.payload, dict) else {}
    action = str(payload.get("action") or "")
    next_attempt_at = str(payload.get("next_attempt_at") or "")
    parts = [
        f"type={redact_secrets(event.event_type or '')}",
        f"status={event.status or ''}",
        f"attempts={int(event.attempts or 0)}",
    ]
    if action:
        parts.append(f"action={redact_secrets(action)}")
    if next_attempt_at:
        parts.append(f"next_attempt_at={redact_secrets(next_attempt_at)}")
    if event.last_error:
        parts.append("error=present")
    return " ".join(parts)


def incident_attention_items(db: Session, now):
    incidents = db.execute(
        select(Incident)
        .where(~Incident.status.in_(TERMINAL_INCIDENT_STATUSES))
        .order_by(Incident.created_at, Incident.id)
        .limit(20)
    ).scalars().all()
    if not incidents:
        return []
    oldest = max(event_age_seconds(incident, now, field="created_at") for incident in incidents)
    return [{
        "category": "incident",
        "impact": "hot_path",
        "severity": "critical" if any(incident.severity == "critical" for incident in incidents) else "warning",
        "title": "Open incidents require review",
        "count": len(incidents),
        "oldest_age_seconds": oldest,
        "next_action": "Открыть вкладку Инциденты, закрыть resolved только после проверки причины.",
        "details": [
            f"{redact_secrets(incident.source)} status={incident.status} severity={incident.severity}"
            for incident in incidents[:3]
        ],
    }]


def import_attention_items(db: Session, now):
    imports = db.execute(
        select(ImportJob)
        .where(ImportJob.status.in_(("failed", "completed_with_errors")))
        .order_by(ImportJob.created_at, ImportJob.id)
        .limit(20)
    ).scalars().all()
    if not imports:
        return []
    oldest = max(event_age_seconds(item, now, field="created_at") for item in imports)
    return [{
        "category": "import",
        "impact": "hot_path",
        "severity": "critical",
        "title": "Failed imports require review",
        "count": len(imports),
        "oldest_age_seconds": oldest,
        "next_action": "Проверить файл/строки импорта; не отправлять повторно до сверки web-панели.",
        "details": [
            f"source={redact_secrets(item.source)} status={item.status} rows={item.rows_imported}/{item.rows_total} file_attached={bool((item.raw_payload or {}).get('filename'))}"
            for item in imports[:3]
        ],
    }]


def build_operations_telegram_summary(items):
    if not items:
        return "TakSklad: внимания не требуется."
    lines = ["TakSklad: требуется внимание", ""]
    for item in items[:6]:
        lines.append(
            f"- {item['title']}: {item['count']} шт.; "
            f"impact={item['impact']}; action={item['next_action']}"
        )
    return "\n".join(lines)


def severity_order(value):
    return {"critical": 0, "warning": 1, "info": 2}.get(str(value or ""), 3)
