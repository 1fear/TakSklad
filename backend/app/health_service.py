from datetime import datetime, timedelta, timezone
import os
import re
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, literal, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from .daily_report_config import (
    DailyReportConfigurationError,
    validate_daily_report_schedule_config,
)

from .event_queue_service import (
    build_event_queue_summary,
    ensure_aware_utc,
    event_to_queue_read,
    list_stale_processing_events,
)
from .models import AuditLog, ImportJob, Incident, PendingEvent
from .redaction import redact_secrets
from .settings import APP_VERSION
from .worker_observability import build_worker_readiness


EXPECTED_BASELINE_REVISION = "20260616_0001"
EXPECTED_HEAD_REVISION = "20260716_0019"
LEGACY_SQLITE_HEAD_REVISION = "20260710_0011"
TERMINAL_INCIDENT_STATUSES = ("resolved", "ignored", "cancelled")
SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE = "skladbot_daily_report_send"
SKLADBOT_DAILY_SUCCESS_RESULT_STATUSES = {
    "CATCHUP_SENT_COMPLETE_ONCE",
    "completed_sent",
}
SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def runtime_build_identity():
    commit_sha = str(os.environ.get("TAKSKLAD_COMMIT_SHA") or "").strip().lower()
    image_digest = str(os.environ.get("TAKSKLAD_IMAGE_DIGEST") or "").strip().lower()
    return {
        "commit_sha": commit_sha if COMMIT_SHA_RE.fullmatch(commit_sha) else "unknown",
        "image_digest": image_digest if SHA256_DIGEST_RE.fullmatch(image_digest) else "unknown",
    }


def build_readiness_report(db: Session, app_settings, now=None):
    now = now or datetime.now(timezone.utc)
    report = {
        "generated_at": now.isoformat(),
        "ready": False,
        "status": "ok",
        "service": app_settings.service_name,
        "version": APP_VERSION,
        **runtime_build_identity(),
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
        "workers": {"status": "unknown", "required": [], "missing": [], "unhealthy": [], "workers": []},
        "daily_report": {"status": "unknown", "due_date": "", "missing_count": 0},
        "policy": {
            "mandatory": [
                "database",
                "migrations",
                "hot_path_queue",
                "imports",
                "worker_main_loops",
                "daily_report_delivery",
            ],
            "optional": [],
            "mandatory_status": "unknown",
            "optional_status": "unknown",
        },
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
        report["policy"]["mandatory_status"] = "unhealthy"
        return report

    report["migrations"] = read_migration_status(db)
    try:
        report["queue"] = build_queue_readiness(db, now=now)
        report["imports"] = build_import_error_readiness(db)
        configured_required_workers = getattr(app_settings, "worker_heartbeat_required_names", ())
        report["workers"] = build_worker_readiness(
            db,
            required_workers=configured_required_workers,
            now=now,
        )
        report["daily_report"] = build_daily_report_delivery_readiness(
            db,
            app_settings,
            now=now,
        )
    except SQLAlchemyError as exc:
        report["status"] = "unhealthy"
        report["database"] = {
            "status": "error",
            "error": redact_secrets(exc),
        }
        report["policy"]["mandatory_status"] = "unhealthy"
        report["policy"]["optional_status"] = "unknown"
        return report
    mandatory_failed = (
        report["migrations"].get("status") != "ok"
        or report["queue"]["hot_path_stale_processing_count"]
        or report["queue"]["hot_path_blocking_count"]
        or report["queue"]["hot_path_last_errors"]
        or report["imports"]["recent_errors"]
        or report["workers"].get("status") != "ok"
        or report["daily_report"].get("status") == "unhealthy"
    )
    report["ready"] = not bool(mandatory_failed)
    report["policy"]["mandatory_status"] = "unhealthy" if mandatory_failed else "ok"
    report["policy"]["optional_status"] = "ok"
    if mandatory_failed:
        report["status"] = "unhealthy"
    return report


def readiness_http_status(report):
    return 200 if report.get("ready") is True else 503


def public_readiness_report(report):
    queue = report.get("queue") or {}
    imports = report.get("imports") or {}
    workers = report.get("workers") or {}
    return {
        "generated_at": report.get("generated_at"),
        "ready": report.get("ready") is True,
        "status": report.get("status") or "unhealthy",
        "service": report.get("service") or "",
        "version": report.get("version") or "",
        "commit_sha": report.get("commit_sha") or "unknown",
        "image_digest": report.get("image_digest") or "unknown",
        "environment": report.get("environment") or "",
        "database": {"status": (report.get("database") or {}).get("status") or "unknown"},
        "migrations": {
            key: (report.get("migrations") or {}).get(key)
            for key in ("status", "expected_baseline", "expected_head", "current_revision")
        },
        "queue": {
            "hot_path_stale_processing_count": int(queue.get("hot_path_stale_processing_count") or 0),
            "hot_path_blocking_count": int(queue.get("hot_path_blocking_count") or 0),
            "hot_path_error_count": len(queue.get("hot_path_last_errors") or []),
        },
        "imports": {"recent_error_count": len(imports.get("recent_errors") or [])},
        "workers": {
            "status": workers.get("status") or "unknown",
            "required_count": len(workers.get("required") or []),
            "missing_count": len(workers.get("missing") or []),
            "unhealthy_count": len(workers.get("unhealthy") or []),
        },
        "daily_report": {
            "status": (report.get("daily_report") or {}).get("status") or "unknown",
            "due_date": (report.get("daily_report") or {}).get("due_date") or "",
            "missing_count": int((report.get("daily_report") or {}).get("missing_count") or 0),
        },
        "policy": dict(report.get("policy") or {}),
    }


def read_migration_status(db: Session):
    try:
        revisions = [str(value or "") for value in db.execute(
            text("SELECT version_num FROM alembic_version ORDER BY version_num")
        ).scalars().all()]
    except SQLAlchemyError as exc:
        return {
            "status": "not_configured",
            "expected_baseline": EXPECTED_BASELINE_REVISION,
            "expected_head": EXPECTED_HEAD_REVISION,
            "current_revision": "",
            "error": redact_secrets(exc),
        }
    revision = revisions[0] if len(revisions) == 1 else ",".join(revisions)
    expected_head = EXPECTED_HEAD_REVISION
    if (
        len(revisions) == 1
        and revision == LEGACY_SQLITE_HEAD_REVISION
        and db.bind is not None
        and db.bind.dialect.name == "sqlite"
    ):
        # SQLite is the local/offline compatibility store and never receives the
        # PostgreSQL-only hot-query indexes from 0014. PostgreSQL remains strict.
        expected_head = LEGACY_SQLITE_HEAD_REVISION
    if not revisions:
        status = "not_stamped"
    elif len(revisions) != 1:
        status = "multiple_revisions"
    elif revision == expected_head:
        status = "ok"
    else:
        status = "revision_mismatch"
    return {
        "status": status,
        "expected_baseline": EXPECTED_BASELINE_REVISION,
        "expected_head": expected_head,
        "current_revision": revision,
    }


def build_queue_readiness(db: Session, now=None):
    now = now or datetime.now(timezone.utc)
    summary = sanitize_queue_summary(build_event_queue_summary(db))
    stale_processing = list_stale_processing_events(db, now=now, limit=20)
    hot_path_stale_processing = list(stale_processing)
    oldest_pending = db.execute(
        select(PendingEvent)
        .where(PendingEvent.status.in_(("pending", "failed")))
        .order_by(PendingEvent.created_at, PendingEvent.id)
        .limit(1)
    ).scalars().first()
    errors = last_event_errors(db, now=now)
    resolved_errors = resolved_daily_report_errors(db, now=now)
    hot_path_errors = list(errors)
    hot_path_blocking_count = count_unresolved_hot_path_failures(db)
    return {
        "summary": summary,
        "oldest_pending_age_seconds": event_age_seconds(oldest_pending, now, field="created_at"),
        "stale_processing_count": len(stale_processing),
        "hot_path_stale_processing_count": len(hot_path_stale_processing),
        "hot_path_blocking_count": hot_path_blocking_count,
        "stale_processing": [
            compact_event_error(event_to_queue_read(event, now=now))
            for event in stale_processing[:10]
        ],
        "last_errors": errors,
        "hot_path_last_errors": hot_path_errors,
        "resolved_historical_errors": resolved_errors,
    }


def build_daily_report_delivery_readiness(db: Session, app_settings, now=None):
    enabled = bool(getattr(app_settings, "skladbot_daily_report_enabled", False))
    environment = str(getattr(app_settings, "environment", "") or "").strip().casefold()
    if not enabled:
        return {
            "status": "unhealthy" if environment == "production" else "disabled",
            "due_date": "",
            "missing_count": 0,
        }

    chat_ids = tuple(
        str(value).strip()
        for value in getattr(app_settings, "skladbot_daily_report_chat_ids", ()) or ()
        if str(value).strip()
    )
    if not chat_ids:
        return {"status": "unhealthy", "due_date": "", "missing_count": 0}

    try:
        schedule_config = validate_daily_report_schedule_config({
            "TAKSKLAD_TIMEZONE": getattr(app_settings, "timezone", "Asia/Tashkent"),
            "SKLADBOT_DAILY_REPORT_HOUR": getattr(app_settings, "skladbot_daily_report_hour", 22),
            "SKLADBOT_DAILY_REPORT_MINUTE": getattr(app_settings, "skladbot_daily_report_minute", 0),
            "SKLADBOT_DAILY_REPORT_RETRY_MINUTES": getattr(
                app_settings,
                "skladbot_daily_report_retry_minutes",
                15,
            ),
            "SKLADBOT_DAILY_REPORT_MAX_ATTEMPTS": getattr(
                app_settings,
                "skladbot_daily_report_max_attempts",
                3,
            ),
            "SKLADBOT_DAILY_REPORT_GRACE_MINUTES": getattr(
                app_settings,
                "skladbot_daily_report_grace_minutes",
                30,
            ),
            "SKLADBOT_DAILY_REPORT_LOOKBACK_DAYS": getattr(
                app_settings,
                "skladbot_daily_report_lookback_days",
                1,
            ),
        })
    except DailyReportConfigurationError:
        return {"status": "unhealthy", "due_date": "", "missing_count": len(chat_ids)}

    business_timezone = ZoneInfo(schedule_config.timezone_name)
    now_utc = ensure_aware_utc(now or datetime.now(timezone.utc))
    local_now = now_utc.astimezone(business_timezone)
    scheduled_at = local_now.replace(
        hour=schedule_config.hour,
        minute=schedule_config.minute,
        second=0,
        microsecond=0,
    )
    latest_due_date = (
        local_now.date()
        if local_now >= scheduled_at + timedelta(minutes=schedule_config.grace_minutes)
        else local_now.date() - timedelta(days=1)
    )
    first_due_date = latest_due_date - timedelta(days=schedule_config.lookback_days - 1)
    due_dates = tuple(
        first_due_date + timedelta(days=offset)
        for offset in range(schedule_config.lookback_days)
    )
    due_date_texts = tuple(value.isoformat() for value in due_dates)

    candidates = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE)
        .where(PendingEvent.status == "completed")
        .where(or_(
            *(
                PendingEvent.idempotency_key.like(f"skladbot_daily_report:{date_text}:%")
                for date_text in due_date_texts
            ),
            PendingEvent.payload["report_date"].as_string().in_(due_date_texts),
        ))
        .order_by(PendingEvent.updated_at.desc(), PendingEvent.created_at.desc())
    ).scalars().all()
    delivered_pairs = {
        (daily_report_event_report_date(event), daily_report_event_chat_key(event))
        for event in candidates
        if daily_report_event_report_date(event) in due_date_texts
        and daily_report_event_success(event)
    }
    missing_pairs = tuple(
        (due_date, chat_id)
        for due_date in due_dates
        for chat_id in chat_ids
        if (due_date.isoformat(), chat_id) not in delivered_pairs
    )
    oldest_missing_date = min(
        (due_date for due_date, _chat_id in missing_pairs),
        default=None,
    )
    return {
        "status": "unhealthy" if missing_pairs else "ok",
        "due_date": (oldest_missing_date or latest_due_date).isoformat(),
        "missing_count": len(missing_pairs),
    }


def count_unresolved_hot_path_failures(db: Session) -> int:
    failed_event = aliased(PendingEvent)
    successful_event = aliased(PendingEvent)
    failed_report_date = _payload_text(failed_event, "report_date")
    successful_report_date = _payload_text(successful_event, "report_date")
    failed_kind = func.coalesce(
        func.nullif(_payload_text(failed_event, "kind"), ""),
        _payload_text(failed_event, "report_kind"),
        "",
    )
    successful_kind = func.coalesce(
        func.nullif(_payload_text(successful_event, "kind"), ""),
        _payload_text(successful_event, "report_kind"),
        "",
    )
    failed_chat = _daily_report_chat_key_expression(db, failed_event, failed_report_date)
    successful_chat = _daily_report_chat_key_expression(db, successful_event, successful_report_date)
    failed_time = func.coalesce(failed_event.updated_at, failed_event.created_at)
    successful_time = func.coalesce(successful_event.updated_at, successful_event.created_at)
    successful_result_status = _payload_text(successful_event, "result_status")
    successful_later_report = (
        select(literal(1))
        .select_from(successful_event)
        .where(successful_event.event_type == SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE)
        .where(successful_event.status == "completed")
        .where(failed_report_date != "")
        .where(successful_report_date == failed_report_date)
        .where(or_(failed_kind == "", successful_kind == "", successful_kind == failed_kind))
        .where(or_(failed_chat == "", successful_chat == "", successful_chat == failed_chat))
        .where(successful_time > failed_time)
        .where(or_(
            successful_event.payload["success"].as_boolean().is_(True),
            func.lower(_payload_text(successful_event, "success")) == "true",
            successful_result_status.in_(SKLADBOT_DAILY_SUCCESS_RESULT_STATUSES),
        ))
        .correlate(failed_event)
        .exists()
    )
    stmt = (
        select(func.count(failed_event.id))
        .select_from(failed_event)
        .where(failed_event.status.in_(("failed", "error", "blocked")))
        .where(~failed_event.id.in_(
            select(Incident.pending_event_id)
            .where(Incident.pending_event_id.is_not(None))
            .where(Incident.status.in_(TERMINAL_INCIDENT_STATUSES))
        ))
        .where(or_(
            failed_event.event_type != SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
            ~successful_later_report,
        ))
    )
    return int(db.execute(stmt).scalar_one() or 0)


def _payload_text(event, key):
    return func.coalesce(event.payload[key].as_string(), "")


def _daily_report_chat_key_expression(db: Session, event, report_date):
    payload_chat = _payload_text(event, "chat_id")
    idempotency_key = func.coalesce(event.idempotency_key, "")
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        parsed_chat = func.split_part(idempotency_key, ":", 3)
    else:
        chat_start = func.length(literal("skladbot_daily_report:")) + func.length(report_date) + 2
        remainder = func.substr(idempotency_key, chat_start)
        parsed_chat = func.substr(remainder, 1, func.instr(remainder, ":") - 1)
    valid_key = idempotency_key.like(
        literal("skladbot_daily_report:") + report_date + literal(":%")
    )
    return case(
        (and_(valid_key, parsed_chat != ""), parsed_chat),
        else_=payload_chat,
    )


def last_event_errors(db: Session, now=None, limit=10, event_type=None):
    now = now or datetime.now(timezone.utc)
    stmt = (
        select(PendingEvent)
        .where(PendingEvent.status.in_(("failed", "error", "blocked")))
        .where(PendingEvent.last_error.is_not(None))
        .where(~PendingEvent.id.in_(
            select(Incident.pending_event_id)
            .where(Incident.pending_event_id.is_not(None))
            .where(Incident.status.in_(TERMINAL_INCIDENT_STATUSES))
        ))
    )
    if event_type:
        stmt = stmt.where(PendingEvent.event_type == event_type)
    stmt = stmt.order_by(PendingEvent.updated_at.desc(), PendingEvent.created_at.desc(), PendingEvent.id.desc()).limit(limit)
    events = db.execute(stmt).scalars().all()
    return [
        compact_event_error(event_to_queue_read(event, now=now))
        for event in events
        if not daily_report_failure_resolved_by_later_success(db, event)
    ]


def resolved_daily_report_errors(db: Session, now=None, limit=10):
    now = now or datetime.now(timezone.utc)
    stmt = (
        select(PendingEvent)
        .where(PendingEvent.event_type == SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE)
        .where(PendingEvent.status.in_(("failed", "error", "blocked")))
        .where(PendingEvent.last_error.is_not(None))
        .order_by(PendingEvent.updated_at.desc(), PendingEvent.created_at.desc(), PendingEvent.id.desc())
        .limit(limit)
    )
    result = []
    for event in db.execute(stmt).scalars().all():
        if not daily_report_failure_resolved_by_later_success(db, event):
            continue
        row = compact_event_error(event_to_queue_read(event, now=now))
        row["resolved_by"] = "later_successful_daily_report"
        result.append(row)
    return result


def daily_report_failure_resolved_by_later_success(db: Session, event: PendingEvent) -> bool:
    if event.event_type != SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE:
        return False
    report_date = daily_report_event_report_date(event)
    if not report_date:
        return False
    event_time = daily_report_event_time(event)
    event_kind = daily_report_event_kind(event)
    event_chat = daily_report_event_chat_key(event)
    candidates = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE)
        .where(PendingEvent.status == "completed")
        .order_by(PendingEvent.updated_at.desc(), PendingEvent.created_at.desc(), PendingEvent.id.desc())
        .limit(100)
    ).scalars().all()
    for candidate in candidates:
        if candidate.id == event.id:
            continue
        if daily_report_event_report_date(candidate) != report_date:
            continue
        candidate_kind = daily_report_event_kind(candidate)
        if event_kind and candidate_kind and event_kind != candidate_kind:
            continue
        candidate_chat = daily_report_event_chat_key(candidate)
        if event_chat and candidate_chat and event_chat != candidate_chat:
            continue
        candidate_time = daily_report_event_time(candidate)
        if event_time and candidate_time and candidate_time <= event_time:
            continue
        if daily_report_event_success(candidate):
            return True
    return False


def daily_report_event_payload(event: PendingEvent) -> dict:
    return event.payload if isinstance(event.payload, dict) else {}


def daily_report_event_report_date(event: PendingEvent) -> str:
    return str(daily_report_event_payload(event).get("report_date") or "").strip()


def daily_report_event_kind(event: PendingEvent) -> str:
    payload = daily_report_event_payload(event)
    return str(payload.get("kind") or payload.get("report_kind") or "").strip()


def daily_report_event_chat_key(event: PendingEvent) -> str:
    key = str(event.idempotency_key or "")
    parts = key.split(":")
    if len(parts) >= 6 and parts[0] == "skladbot_daily_report":
        return parts[2].strip()
    payload = daily_report_event_payload(event)
    return str(payload.get("chat_id") or "").strip()


def daily_report_event_time(event: PendingEvent):
    return ensure_aware_utc(event.updated_at or event.created_at)


def daily_report_event_success(event: PendingEvent) -> bool:
    payload = daily_report_event_payload(event)
    result_status = str(payload.get("result_status") or "").strip()
    success = payload.get("success")
    if success is True:
        return True
    if isinstance(success, str) and success.strip().lower() == "true":
        return True
    return result_status in SKLADBOT_DAILY_SUCCESS_RESULT_STATUSES


def compact_event_error(event):
    event = dict(event or {})
    return {
        "id": event.get("id") or "",
        "event_type": sanitize_readiness_event_type(event.get("event_type")),
        "status": event.get("status") or "",
        "attempts": int(event.get("attempts") or 0),
        "last_error": redact_secrets(event.get("last_error") or ""),
        "payload_status": event.get("payload_status") or "",
        "retryable": bool(event.get("retryable")),
        "age_seconds": int(event.get("age_seconds") or 0),
        "created_at": event.get("created_at"),
        "updated_at": event.get("updated_at"),
    }


def sanitize_queue_summary(summary):
    summary = dict(summary or {})
    by_type = summary.get("by_type") or {}
    sanitized_by_type = {}
    for event_type, statuses in by_type.items():
        safe_type = sanitize_readiness_event_type(event_type)
        target = sanitized_by_type.setdefault(safe_type, {})
        for status, count in dict(statuses or {}).items():
            target[status] = int(target.get(status) or 0) + int(count or 0)
    summary["by_type"] = dict(sorted(sanitized_by_type.items()))
    return summary


def sanitize_readiness_event_type(event_type):
    text_value = redact_secrets(str(event_type or ""))
    if ":" not in text_value:
        return text_value
    prefix = text_value.split(":", 1)[0].strip() or "event"
    return f"{prefix}:*"


def min_next_attempt_at(events, now=None):
    now = now or datetime.now(timezone.utc)
    attempts = []
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        next_attempt_raw = str(payload.get("next_attempt_at") or "").strip()
        if not next_attempt_raw:
            continue
        try:
            next_attempt = datetime.fromisoformat(next_attempt_raw)
        except ValueError:
            continue
        next_attempt = ensure_aware_utc(next_attempt)
        if next_attempt and next_attempt > now:
            attempts.append(next_attempt)
    if not attempts:
        return None
    return min(attempts)


def event_age_seconds(event, now, field="updated_at"):
    if event is None:
        return 0
    value = ensure_aware_utc(getattr(event, field, None) or getattr(event, "updated_at", None))
    if value is None:
        return 0
    return int(max(0, (now - value).total_seconds()))


def datetime_age_seconds(value, now):
    value = ensure_aware_utc(value)
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
