from datetime import timedelta, timezone

from sqlalchemy import select

from .models import PendingEvent
from .telegram_common import normalize_text


SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE = "skladbot_daily_report_send"
SKLADBOT_DAILY_REPORT_COVERAGE_FAILED_ERROR = "SKLADBOT_DAILY_REPORT_COVERAGE_NOT_COMPLETE"
SKLADBOT_DAILY_REPORT_LEGACY_EMPTY_ERROR_PREFIX = (
    f"{SKLADBOT_DAILY_REPORT_COVERAGE_FAILED_ERROR}: included=0 excluded="
)
SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT = "completed_no_requests"
SKLADBOT_DAILY_SUCCESS_RESULT_STATUSES = {
    "CATCHUP_SENT_COMPLETE_ONCE",
    SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT,
    "completed_sent",
}
SKLADBOT_DAILY_SAFE_RETRY_STAGES = {
    "scheduled job started",
    "report generation finished",
    "scheduled job failed",
    "xlsx created",
}


def ensure_aware_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def daily_report_event_success(event):
    if event is None or normalize_text(event.status) != "completed":
        return False
    payload = event.payload if isinstance(event.payload, dict) else {}
    success = payload.get("success")
    if success is True or normalize_text(success).casefold() == "true":
        return True
    return normalize_text(payload.get("result_status")) in SKLADBOT_DAILY_SUCCESS_RESULT_STATUSES


def completed_daily_report_delivery_exists(db, chat_id, report_date):
    prefix = f"skladbot_daily_report:{report_date.isoformat()}:{chat_id}:"
    candidates = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE)
        .where(PendingEvent.status == "completed")
        .where(PendingEvent.idempotency_key.like(prefix + "%"))
        .order_by(PendingEvent.updated_at.desc(), PendingEvent.created_at.desc())
        .limit(20)
    ).scalars().all()
    return any(daily_report_event_success(event) for event in candidates)


def failed_daily_report_retry_is_safe(event, now_utc, retry_minutes, max_attempts):
    payload = event.payload if isinstance(event.payload, dict) else {}
    stage = normalize_text(payload.get("stage"))
    if stage not in SKLADBOT_DAILY_SAFE_RETRY_STAGES:
        return False
    if int(event.attempts or 0) >= max(1, int(max_attempts)):
        return False
    updated_at = ensure_aware_utc(event.updated_at or event.created_at)
    return bool(
        updated_at
        and now_utc
        and now_utc - updated_at >= timedelta(minutes=max(1, int(retry_minutes)))
    )


def failed_daily_report_is_legacy_empty_false_positive(event):
    if event is None or normalize_text(event.status) != "failed":
        return False
    error = normalize_text(event.last_error)
    if not error.startswith(SKLADBOT_DAILY_REPORT_LEGACY_EMPTY_ERROR_PREFIX):
        return False
    excluded = error.removeprefix(SKLADBOT_DAILY_REPORT_LEGACY_EMPTY_ERROR_PREFIX)
    if not excluded.isdigit() or int(excluded) <= 0:
        return False
    payload = event.payload if isinstance(event.payload, dict) else {}
    return not normalize_text(payload.get("legacy_empty_retry_claimed_at"))
