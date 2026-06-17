from datetime import datetime, timezone

from sqlalchemy import text, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .event_queue_service import (
    build_event_queue_summary,
    ensure_aware_utc,
    event_to_queue_read,
    list_stale_processing_events,
)
from .models import ImportJob, Incident, PendingEvent
from .redaction import redact_secrets
from .settings import APP_VERSION


EXPECTED_BASELINE_REVISION = "20260616_0001"
EXPECTED_HEAD_REVISION = "20260617_0002"
OK_MIGRATION_REVISIONS = {EXPECTED_BASELINE_REVISION, EXPECTED_HEAD_REVISION}
TERMINAL_INCIDENT_STATUSES = ("resolved", "ignored", "cancelled")


def build_readiness_report(db: Session, app_settings):
    now = datetime.now(timezone.utc)
    report = {
        "generated_at": now.isoformat(),
        "status": "ok",
        "service": app_settings.service_name,
        "version": APP_VERSION,
        "environment": app_settings.environment,
        "database": {"status": "unknown"},
        "migrations": {
            "status": "unknown",
            "expected_baseline": EXPECTED_BASELINE_REVISION,
            "expected_head": EXPECTED_HEAD_REVISION,
        },
        "queue": {
            "summary": {},
            "oldest_pending_age_seconds": 0,
            "stale_processing_count": 0,
            "stale_processing": [],
            "last_errors": [],
        },
        "imports": {"recent_errors": []},
    }

    try:
        db.execute(text("SELECT 1")).scalar_one()
        report["database"] = {
            "status": "ok",
            "dialect": db.bind.dialect.name if db.bind is not None else "",
        }
    except SQLAlchemyError as exc:
        report["status"] = "unhealthy"
        report["database"] = {"status": "error", "error": redact_secrets(exc)}
        return report

    report["migrations"] = read_migration_status(db)
    report["queue"] = build_queue_readiness(db, now=now)
    report["imports"] = build_import_error_readiness(db)
    if (
        report["migrations"].get("status") != "ok"
        or report["queue"]["stale_processing_count"]
        or report["queue"]["last_errors"]
        or report["imports"]["recent_errors"]
    ):
        report["status"] = "degraded"
    return report


def read_migration_status(db: Session):
    try:
        revision = db.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar_one_or_none()
    except SQLAlchemyError as exc:
        return {
            "status": "not_configured",
            "expected_baseline": EXPECTED_BASELINE_REVISION,
            "expected_head": EXPECTED_HEAD_REVISION,
            "current_revision": "",
            "error": redact_secrets(exc),
        }
    revision = str(revision or "")
    if not revision:
        status = "not_stamped"
    elif revision in OK_MIGRATION_REVISIONS:
        status = "ok"
    else:
        status = "revision_mismatch"
    return {
        "status": status,
        "expected_baseline": EXPECTED_BASELINE_REVISION,
        "expected_head": EXPECTED_HEAD_REVISION,
        "current_revision": revision,
    }


def build_queue_readiness(db: Session, now=None):
    now = now or datetime.now(timezone.utc)
    summary = build_event_queue_summary(db)
    stale_processing = list_stale_processing_events(db, now=now, limit=20)
    oldest_pending = db.execute(
        select(PendingEvent)
        .where(PendingEvent.status.in_(("pending", "failed")))
        .order_by(PendingEvent.created_at, PendingEvent.id)
        .limit(1)
    ).scalars().first()
    return {
        "summary": summary,
        "oldest_pending_age_seconds": event_age_seconds(oldest_pending, now, field="created_at"),
        "stale_processing_count": len(stale_processing),
        "stale_processing": [
            compact_event_error(event_to_queue_read(event, now=now))
            for event in stale_processing[:10]
        ],
        "last_errors": last_event_errors(db, now=now),
    }


def last_event_errors(db: Session, now=None, limit=10):
    now = now or datetime.now(timezone.utc)
    events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.status.in_(("failed", "error", "blocked")))
        .where(PendingEvent.last_error.is_not(None))
        .where(~PendingEvent.id.in_(
            select(Incident.pending_event_id)
            .where(Incident.pending_event_id.is_not(None))
            .where(Incident.status.in_(TERMINAL_INCIDENT_STATUSES))
        ))
        .order_by(PendingEvent.updated_at.desc(), PendingEvent.created_at.desc(), PendingEvent.id.desc())
        .limit(limit)
    ).scalars().all()
    return [compact_event_error(event_to_queue_read(event, now=now)) for event in events]


def compact_event_error(event):
    event = dict(event or {})
    event["last_error"] = redact_secrets(event.get("last_error") or "")
    return event


def event_age_seconds(event, now, field="updated_at"):
    if event is None:
        return 0
    value = ensure_aware_utc(getattr(event, field, None) or getattr(event, "updated_at", None))
    if value is None:
        return 0
    return int(max(0, (now - value).total_seconds()))


def build_import_error_readiness(db: Session, limit=10):
    imports = db.execute(
        select(ImportJob)
        .where(ImportJob.status.in_(("failed", "completed_with_errors")))
        .where(~ImportJob.id.in_(
            select(Incident.import_id)
            .where(Incident.import_id.is_not(None))
            .where(Incident.status.in_(TERMINAL_INCIDENT_STATUSES))
        ))
        .order_by(ImportJob.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return {
        "recent_errors": [
            {
                "id": str(item.id),
                "status": item.status,
                "source": item.source,
                "filename": redact_secrets((item.raw_payload or {}).get("filename") or ""),
                "rows": f"{item.rows_imported}/{item.rows_total}",
                "errors": [redact_secrets(error) for error in ((item.raw_payload or {}).get("errors") or [])[:3]],
            }
            for item in imports
        ]
    }
