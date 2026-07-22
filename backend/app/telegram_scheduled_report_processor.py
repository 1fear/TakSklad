import hashlib
import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from sqlalchemy import select
from . import skladbot_daily_report
from .db import SessionLocal
from .models import AuditLog, PendingEvent
from .outbox_service import queue_outbox_event
from .redaction import redact_secrets
from .reconciliation_service import run_daily_reconciliation
from .telegram_clients import TelegramProcessorDelegate
from .telegram_common import iso_date_from_display, normalize_text, parse_dates_from_text, parse_int
from .telegram_daily_report_policy import (
    SKLADBOT_DAILY_REPORT_COVERAGE_FAILED_ERROR,
    SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT,
    SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
    SKLADBOT_DAILY_SAFE_RETRY_STAGES,
    completed_daily_report_delivery_exists,
    ensure_aware_utc,
    failed_daily_report_is_legacy_empty_false_positive,
    failed_daily_report_retry_is_safe,
)
from .telegram_output_contract import daily_report_caption
SKLADBOT_DAILY_REPORTED_REQUEST_EVENT_TYPE = "skladbot_daily_reported_request"
TELEGRAM_NOTIFICATION_EVENT_TYPE = "telegram_notification"
SKLADBOT_DAILY_REPORT_STALE_TTL_MINUTES_ENV = "SKLADBOT_DAILY_REPORT_STALE_TTL_MINUTES"
SKLADBOT_DAILY_REPORT_STALE_FAILED_ERROR = "STUCK_PROCESSING_AFTER_TTL"
SCHEDULED_DAILY_PAYLOAD_SECRET_KEY_PARTS = (
    "chat", "token", "secret", "password", "authorization", "credential",
    "api_key", "apikey", "jwt", "raw", "payload",
)
SCHEDULED_DAILY_ALERT_REASON_MAX_LENGTH = 500
def command_date_or_today(text):
    dates = parse_dates_from_text(text)
    if dates:
        return datetime.strptime(dates[0], "%Y-%m-%d").date()
    return skladbot_daily_report.business_today()
def coerce_report_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    iso_date = iso_date_from_display(value)
    if iso_date:
        return datetime.strptime(iso_date, "%Y-%m-%d").date()
    return command_date_or_today(str(value))


def skladbot_reported_request_key(
    request_id,
    report_date="",
    chat_id="",
    mode="scheduled",
    report_kind="daily_skladbot",
    report_version="",
):
    return ":".join([
        "skladbot_daily_reported_request",
        normalize_text(report_date),
        normalize_text(chat_id),
        normalize_text(mode),
        normalize_text(report_kind),
        normalize_text(report_version),
        str(parse_int(request_id)),
    ])


def skladbot_report_version(report):
    rows = [
        {
            "id": parse_int(request.get("id")),
            "number": normalize_text(request.get("number")),
            "category": normalize_text(request.get("category")),
            "reason": normalize_text(request.get("inclusion_reason") or ",".join(request.get("include_reasons") or [])),
        }
        for request in report.get("requests") or []
    ]
    payload = {
        "report_date": normalize_text(report.get("report_date")),
        "coverage_status": normalize_text((report.get("coverage") or {}).get("coverage_status")),
        "included": rows,
        "excluded": len(report.get("excluded_requests") or []),
        "errors": len(report.get("errors") or []),
        "warnings": normalize_text((report.get("coverage") or {}).get("warnings")),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def mark_skladbot_daily_report_requests_reported(
    report, chat_id=None, mode="scheduled", report_kind="daily_skladbot", session_factory=None,
):
    report_date = coerce_report_date(report.get("report_date") or skladbot_daily_report.business_today())
    report_version = skladbot_report_version(report)
    rows = []
    for request in report.get("requests") or []:
        request_id = parse_int(request.get("id"))
        if request_id <= 0:
            continue
        rows.append({
            "request_id": request_id,
            "request_number": normalize_text(request.get("number")),
            "category": normalize_text(request.get("category")),
            "reported_date": report_date.isoformat(),
            "chat_id": normalize_text(chat_id),
            "mode": normalize_text(mode),
            "report_kind": normalize_text(report_kind),
            "report_version": report_version,
            "coverage_status": normalize_text((report.get("coverage") or {}).get("coverage_status")),
            "include_reasons": list(request.get("include_reasons") or []),
        })
    if not rows:
        return 0
    saved = 0
    with (session_factory or SessionLocal)() as db:
        for row in rows:
            key = skladbot_reported_request_key(
                row["request_id"],
                row["reported_date"],
                row["chat_id"],
                row["mode"],
                row["report_kind"],
                row["report_version"],
            )
            existing = db.execute(
                select(PendingEvent).where(PendingEvent.idempotency_key == key)
            ).scalar_one_or_none()
            if existing is not None:
                continue
            db.add(PendingEvent(
                event_type=SKLADBOT_DAILY_REPORTED_REQUEST_EVENT_TYPE,
                idempotency_key=key,
                status="completed",
                attempts=1,
                payload=row,
            ))
            saved += 1
        db.commit()
    return saved


def scheduled_skladbot_daily_report_blocker(report):
    coverage = report.get("coverage") if isinstance(report, dict) else {}
    coverage_status = normalize_text((coverage or {}).get("coverage_status")).lower()
    errors = report.get("errors") if isinstance(report, dict) else []
    if coverage_status and coverage_status != "complete":
        return f"{SKLADBOT_DAILY_REPORT_COVERAGE_FAILED_ERROR}: coverage_status={coverage_status}"
    if errors:
        return f"{SKLADBOT_DAILY_REPORT_COVERAGE_FAILED_ERROR}: errors={len(errors)}"
    included = parse_int((coverage or {}).get("included_operational_requests"))
    excluded = parse_int((coverage or {}).get("excluded_diagnostic_requests"))
    if included == 0 and excluded > 0 and not (report.get("daily_kiz_rows") or []):
        return f"{SKLADBOT_DAILY_REPORT_COVERAGE_FAILED_ERROR}: included=0 excluded={excluded}"
    return ""


def manual_skladbot_daily_partial_warning(report, blocker):
    coverage = report.get("coverage") if isinstance(report, dict) else {}
    coverage_status = normalize_text((coverage or {}).get("coverage_status")).upper() or "UNKNOWN"
    warnings = normalize_text((coverage or {}).get("warnings"))
    reasons = [normalize_text(blocker)]
    if warnings:
        reasons.append(f"warnings={warnings}")
    errors = report.get("errors") if isinstance(report, dict) else []
    if errors:
        reasons.append(f"errors={len(errors)}")
    reason_text = "; ".join(reason for reason in reasons if reason)
    return (
        f"SkladBot daily отчет не отправлен: coverage_status={coverage_status}, причины: {reason_text}. "
        "Подробности доступны в diagnostics/logs. "
        "Для ручной отправки неполного отчета нужен explicit override --allow-partial."
    )


def manual_skladbot_daily_partial_override_warning(report, blocker):
    coverage = report.get("coverage") if isinstance(report, dict) else {}
    coverage_status = normalize_text((coverage or {}).get("coverage_status")).upper() or "UNKNOWN"
    warnings = normalize_text((coverage or {}).get("warnings"))
    suffix = f" Причины: {normalize_text(blocker)}"
    if warnings:
        suffix += f"; warnings={warnings}"
    return f"НЕПОЛНЫЙ ОТЧЕТ. Ручная отправка выполнена по explicit override. coverage_status={coverage_status}.{suffix}"


def scheduled_skladbot_daily_report_payload_key_is_safe(key):
    key_text = normalize_text(key)
    if not key_text:
        return False
    key_folded = key_text.casefold()
    return not any(secret in key_folded for secret in SCHEDULED_DAILY_PAYLOAD_SECRET_KEY_PARTS)


def safe_scheduled_skladbot_daily_report_payload(payload):
    safe_payload = {}
    for key, value in dict(payload or {}).items():
        key_text = normalize_text(key)
        if scheduled_skladbot_daily_report_payload_key_is_safe(key_text):
            safe_payload[key_text] = value
    return safe_payload


def queue_scheduled_daily_failure_alert(
    db,
    event,
    automation_alert_chat_id,
    admin_chat_ids,
    error="",
):
    admins = {normalize_text(value) for value in admin_chat_ids or () if normalize_text(value)}
    target_chat_id = normalize_text(automation_alert_chat_id)
    if (
        event is None
        or not target_chat_id.isdigit()
        or int(target_chat_id) <= 0
        or target_chat_id not in admins
    ):
        return None
    payload = event.payload if isinstance(event.payload, dict) else {}
    report_date = normalize_text(payload.get("report_date")) or "unknown"
    attempt = max(1, int(event.attempts or 0))
    reason = normalize_text(redact_secrets(error or event.last_error or "scheduled daily failed"))
    if len(reason) > SCHEDULED_DAILY_ALERT_REASON_MAX_LENGTH:
        reason = reason[:SCHEDULED_DAILY_ALERT_REASON_MAX_LENGTH] + "..."
    event_id = str(event.id)
    idempotency_key = f"telegram:notification:v1:skladbot_daily_failure:{event_id}:{attempt}"
    text = "\n".join([
        "TakSklad: вечерний SkladBot daily не доставлен",
        "",
        f"Дата: {report_date}",
        f"Попытка: {attempt}",
        f"Причина: {reason}",
        "",
        "Что сделать: проверить /ready и событие daily перед ручным повтором.",
    ])
    return queue_outbox_event(
        db,
        event_type=TELEGRAM_NOTIFICATION_EVENT_TYPE,
        action="notify_skladbot_daily_failure",
        aggregate_type="pending_event",
        aggregate_id=event_id,
        idempotency_key=idempotency_key,
        payload={
            "kind": "skladbot_daily_failure_alert",
            "chat_id": target_chat_id,
            "source_event_id": event_id,
            "report_date": report_date,
            "retry_cycle": attempt,
            "text": text,
        },
    )


class TelegramScheduledReportProcessor(TelegramProcessorDelegate):
    def __init__(self, *, ports=None, owner=None, **port_dependencies):
        TelegramProcessorDelegate.__init__(self, ports=ports, owner=owner, **port_dependencies)

    def _scheduled_session_factory(self):
        return getattr(self, "session_factory", None) or SessionLocal

    def _skladbot_daily_report_module(self):
        return getattr(self, "skladbot_report_module", None) or skladbot_daily_report

    def _daily_reconciliation_callback(self):
        return getattr(self, "daily_reconciliation_callback", None) or run_daily_reconciliation

    @staticmethod
    def _emit_skladbot_daily_progress(progress, stage, **fields):
        logging.info(
            "Telegram worker: scheduled SkladBot daily progress stage=%s report_date=%s",
            stage,
            fields.get("report_date") or "",
        )
        if progress is not None:
            progress(stage, **fields)

    def prepare_skladbot_daily_report(
        self,
        report_date=None,
        progress=None,
        *,
        scheduled=False,
        allow_partial=False,
        build_for_dry_run=False,
    ):
        """Collect and fully hydrate one combined daily workbook before Telegram I/O."""
        report_module = self._skladbot_daily_report_module()
        report_date = coerce_report_date(report_date or report_module.business_today())
        self._emit_skladbot_daily_progress(
            progress,
            "scheduled job started",
            report_date=report_date.isoformat(),
            scheduled=bool(scheduled),
        )
        report = report_module.collect_skladbot_daily_report(
            report_date=report_date,
        )
        enrich_smartup_ids = getattr(report_module, "enrich_smartup_ids_from_orders", None)
        enrich_daily_kiz = getattr(report_module, "enrich_daily_kiz_from_orders", None)
        if callable(enrich_smartup_ids) or callable(enrich_daily_kiz):
            with self._scheduled_session_factory()() as db:
                if callable(enrich_smartup_ids):
                    enrich_smartup_ids(db, report)
                if callable(enrich_daily_kiz):
                    enrich_daily_kiz(db, report)
        report.setdefault("request_kiz_rows", [])
        report.setdefault("daily_kiz_rows", [])
        report_date = coerce_report_date(report.get("report_date") or report_date)
        coverage = report.get("coverage") or {}
        requests_count = len(report.get("requests") or [])
        order_kiz_count = len(report.get("request_kiz_rows") or [])
        day_kiz_count = len(report.get("daily_kiz_rows") or [])
        combined_empty = (
            requests_count == 0
            and order_kiz_count == 0
            and day_kiz_count == 0
        )
        self._emit_skladbot_daily_progress(
            progress,
            "report generation finished",
            report_date=report_date.isoformat(),
            coverage_status=normalize_text(coverage.get("coverage_status")),
            requests_count=requests_count,
            order_kiz_count=order_kiz_count,
            day_kiz_count=day_kiz_count,
            errors_count=len(report.get("errors") or []),
        )
        no_requests_result = bool(
            scheduled
            and normalize_text(coverage.get("coverage_status")).lower() == "complete"
            and not (report.get("errors") or [])
            and combined_empty
        )
        if no_requests_result:
            self._emit_skladbot_daily_progress(
                progress,
                "scheduled job no requests",
                report_date=report_date.isoformat(),
                result_status=SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT,
                combined_empty=True,
                requests_count=0,
                order_kiz_count=0,
                day_kiz_count=0,
            )
        if no_requests_result and not build_for_dry_run:
            return {
                "report": report,
                "report_date": report_date,
                "content": None,
                "filename": "",
                "message": "",
                "blocker": "",
                "result_status": SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT,
                "combined_empty": True,
                "requests_count": 0,
                "order_kiz_count": 0,
                "day_kiz_count": 0,
            }
        blocker = scheduled_skladbot_daily_report_blocker(report)
        content = None
        filename = ""
        message = ""
        if not blocker or allow_partial or build_for_dry_run:
            content, filename = report_module.build_skladbot_daily_report_xlsx(report)
            self._emit_skladbot_daily_progress(
                progress,
                "xlsx created",
                report_date=report_date.isoformat(),
                filename=filename,
                bytes=len(content),
            )
            message = report_module.build_skladbot_daily_report_message(report)
        return {
            "report": report,
            "report_date": report_date,
            "content": content,
            "filename": filename,
            "message": message,
            "blocker": blocker,
            "result_status": (
                SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT if no_requests_result else ""
            ),
            "combined_empty": combined_empty,
            "requests_count": requests_count,
            "order_kiz_count": order_kiz_count,
            "day_kiz_count": day_kiz_count,
        }

    def send_skladbot_daily_report(
        self,
        chat_id,
        report_date=None,
        scheduled=False,
        progress=None,
        allow_partial=False,
        delivery_mode="scheduled",
    ):
        prepared = self.prepare_skladbot_daily_report(
            report_date,
            progress,
            scheduled=scheduled,
            allow_partial=allow_partial,
        )
        if prepared["result_status"] == SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT:
            return SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT
        report = prepared["report"]
        report_date = prepared["report_date"]
        blocker = prepared["blocker"]
        if blocker:
            if scheduled:
                self._emit_skladbot_daily_progress(
                    progress,
                    "scheduled job failed",
                    report_date=report_date.isoformat(),
                    error=blocker,
                )
                raise RuntimeError(blocker)
            if not allow_partial:
                self.safe_send_message(chat_id, manual_skladbot_daily_partial_warning(report, blocker))
                return False

        content = prepared["content"]
        filename = prepared["filename"]
        message = prepared["message"]
        if not scheduled:
            report_date_text = report_date.strftime("%d.%m.%Y")
            self.safe_send_message(chat_id, f"Собираю SkladBot отчет за {report_date_text}.")
            if blocker:
                self.safe_send_message(chat_id, manual_skladbot_daily_partial_override_warning(report, blocker))
        if scheduled:
            self._emit_skladbot_daily_progress(
                progress,
                "telegram sendMessage started",
                report_date=report_date.isoformat(),
            )
            self.send_message(chat_id, message)
            self._emit_skladbot_daily_progress(
                progress,
                "telegram sendMessage success",
                report_date=report_date.isoformat(),
            )
            self._emit_skladbot_daily_progress(
                progress,
                "telegram sendDocument started",
                report_date=report_date.isoformat(),
            )
            document = self.send_document(
                chat_id,
                content,
                filename,
                caption=daily_report_caption(report_date),
            )
            self._emit_skladbot_daily_progress(
                progress,
                "telegram sendDocument success",
                report_date=report_date.isoformat(),
            )
        else:
            self.safe_send_message(chat_id, message)
            document = self.safe_send_document(
                chat_id,
                content,
                filename,
                caption=daily_report_caption(report_date),
            )
        if document is not None and scheduled:
            reported_count = mark_skladbot_daily_report_requests_reported(
                report,
                chat_id=chat_id,
                mode=normalize_text(delivery_mode) or "scheduled",
                session_factory=self._scheduled_session_factory(),
            )
            self._emit_skladbot_daily_progress(
                progress,
                "reported mark success",
                report_date=report_date.isoformat(),
                reported_count=reported_count,
            )
        return document is not None

    def scheduled_skladbot_daily_report_is_due(self, now=None):
        if not getattr(self, "skladbot_daily_report_enabled", False):
            return False
        if not getattr(self, "skladbot_daily_report_chat_ids", set()):
            return False
        report_module = self._skladbot_daily_report_module()
        timezone_info = report_module.business_timezone()
        now = now or datetime.now(timezone_info)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone_info)
        else:
            now = now.astimezone(timezone_info)
        scheduled_minutes = (
            getattr(self, "skladbot_daily_report_hour", 22) * 60
            + getattr(self, "skladbot_daily_report_minute", 0)
        )
        return now.hour * 60 + now.minute >= scheduled_minutes

    def latest_due_skladbot_daily_report_date(self, now=None):
        report_module = self._skladbot_daily_report_module()
        timezone_info = report_module.business_timezone()
        now = now or datetime.now(timezone_info)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone_info)
        else:
            now = now.astimezone(timezone_info)
        scheduled_at = now.replace(
            hour=getattr(self, "skladbot_daily_report_hour", 22),
            minute=getattr(self, "skladbot_daily_report_minute", 0),
            second=0,
            microsecond=0,
        )
        return now.date() if now >= scheduled_at else now.date() - timedelta(days=1)

    def oldest_missing_skladbot_daily_report_date(self, chat_id, now=None):
        latest_due_date = self.latest_due_skladbot_daily_report_date(now)
        lookback_days = max(
            1,
            int(getattr(self, "skladbot_daily_report_lookback_days", 1)),
        )
        first_due_date = latest_due_date - timedelta(days=lookback_days - 1)
        with self._scheduled_session_factory()() as db:
            for offset in range(lookback_days):
                candidate_date = first_due_date + timedelta(days=offset)
                if not completed_daily_report_delivery_exists(db, chat_id, candidate_date):
                    return candidate_date
        return None

    def skladbot_daily_report_idempotency_key(self, chat_id, report_date, mode="scheduled", report_kind="daily_skladbot", report_version="v2"):
        return f"skladbot_daily_report:{report_date.isoformat()}:{chat_id}:{mode}:{report_kind}:{report_version}"

    def claim_scheduled_skladbot_daily_report(self, chat_id, report_date, now=None):
        now = now or datetime.now(self._skladbot_daily_report_module().business_timezone())
        now_utc = ensure_aware_utc(now.astimezone(timezone.utc) if now.tzinfo else now)
        idempotency_key = self.skladbot_daily_report_idempotency_key(chat_id, report_date)
        with self._scheduled_session_factory()() as db:
            if completed_daily_report_delivery_exists(db, chat_id, report_date):
                return ""
            event = db.execute(
                select(PendingEvent).where(PendingEvent.idempotency_key == idempotency_key)
            ).scalars().first()
            if event is not None and event.status == "completed":
                self.mark_scheduled_skladbot_daily_report_manual_recovery_required(
                    db,
                    event,
                    now_utc,
                    "skipped_same_day_existing_completed_event",
                )
                db.commit()
                return ""
            if event is not None and event.status == "processing":
                updated_at = ensure_aware_utc(event.updated_at)
                stale_minutes = max(1, parse_int(os.environ.get(SKLADBOT_DAILY_REPORT_STALE_TTL_MINUTES_ENV) or "30"))
                if updated_at and now_utc and now_utc - updated_at >= timedelta(minutes=stale_minutes):
                    self.fail_stale_scheduled_skladbot_daily_report(db, event, now_utc)
                    db.commit()
                    return ""
                # A fresh processing lease is owned by an active worker. Polling it
                # must not refresh updated_at or overwrite the last delivery stage,
                # otherwise the stale detector can never expire the original lease.
                return ""
            legacy_empty_retry = False
            if event is not None and event.status == "failed":
                legacy_empty_retry = failed_daily_report_is_legacy_empty_false_positive(event)
                retry_minutes = max(
                    1,
                    int(getattr(self, "skladbot_daily_report_retry_minutes", 15)),
                )
                max_attempts = max(
                    1,
                    int(getattr(self, "skladbot_daily_report_max_attempts", 3)),
                )
                if not legacy_empty_retry and not failed_daily_report_retry_is_safe(
                    event,
                    now_utc,
                    retry_minutes,
                    max_attempts,
                ):
                    payload = event.payload if isinstance(event.payload, dict) else {}
                    stage = normalize_text(payload.get("stage"))
                    updated_at = ensure_aware_utc(event.updated_at or event.created_at)
                    waiting_for_retry = bool(
                        stage in SKLADBOT_DAILY_SAFE_RETRY_STAGES
                        and int(event.attempts or 0) < max_attempts
                        and updated_at
                        and now_utc - updated_at < timedelta(minutes=retry_minutes)
                    )
                    if not waiting_for_retry:
                        self.mark_scheduled_skladbot_daily_report_manual_recovery_required(
                            db,
                            event,
                            now_utc,
                            "automatic_retry_not_safe_or_exhausted",
                        )
                        db.commit()
                    return ""
            payload = {
                "report_date": report_date.isoformat(),
                "mode": "scheduled",
                "kind": "daily_skladbot",
                "report_version": "v2",
                "stage": "scheduled job started",
                "scheduled_at": f"{getattr(self, 'skladbot_daily_report_hour', 22):02d}:{getattr(self, 'skladbot_daily_report_minute', 0):02d}",
                "claimed_at": now_utc.isoformat() if now_utc else "",
            }
            if event is None:
                event = PendingEvent(
                    event_type=SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
                    idempotency_key=idempotency_key,
                    status="processing",
                    attempts=1,
                    payload=payload,
                    last_error=None,
                )
                db.add(event)
            else:
                existing_payload = {**(event.payload or {})}
                if legacy_empty_retry:
                    for key in (
                        "error",
                        "finished_at",
                        "manual_recovery_required",
                        "manual_recovery_reason",
                        "manual_recovery_marked_at",
                        "result_status",
                        "same_day_existing_event_status",
                        "stale_failed_at",
                        "stale_origin_stage",
                        "success",
                    ):
                        existing_payload.pop(key, None)
                    existing_payload.update({
                        "legacy_empty_retry_claimed_at": now_utc.isoformat() if now_utc else "",
                        "legacy_empty_retry_reason": "legacy_false_positive_empty_coverage_guard",
                    })
                    db.add(AuditLog(
                        action="skladbot_daily_report_legacy_empty_retry_claimed",
                        entity_type="pending_event",
                        entity_id=str(event.id),
                        payload={
                            "report_date": report_date.isoformat(),
                            "reason": "legacy_false_positive_empty_coverage_guard",
                            "previous_attempts": int(event.attempts or 0),
                        },
                    ))
                event.status = "processing"
                event.attempts = (event.attempts or 0) + 1
                event.payload = {**existing_payload, **payload}
                event.last_error = None
            db.commit()
            return str(event.id)

    def mark_scheduled_skladbot_daily_report_manual_recovery_required(self, db, event, now_utc, reason):
        payload = safe_scheduled_skladbot_daily_report_payload(event.payload or {})
        if payload.get("manual_recovery_required") is True:
            return False
        payload.update({
            "stage": "manual_recovery_required",
            "result_status": "manual_recovery_required",
            "manual_recovery_required": True,
            "same_day_existing_event_status": normalize_text(event.status),
            "manual_recovery_reason": normalize_text(reason),
            "manual_recovery_marked_at": now_utc.isoformat() if now_utc else datetime.now(timezone.utc).isoformat(),
        })
        event.payload = payload
        db.add(AuditLog(
            action="skladbot_daily_report_manual_recovery_required",
            entity_type="pending_event",
            entity_id=str(event.id),
            payload={
                "event_type": event.event_type,
                "status": normalize_text(event.status),
                "reason": normalize_text(reason),
            },
        ))
        return True

    def fail_stale_scheduled_skladbot_daily_report(self, db, event, now_utc):
        event.status = "failed"
        event.last_error = SKLADBOT_DAILY_REPORT_STALE_FAILED_ERROR
        payload = safe_scheduled_skladbot_daily_report_payload(event.payload or {})
        stale_origin_stage = normalize_text(payload.get("stage"))
        payload.update({
            "finished_at": now_utc.isoformat() if now_utc else datetime.now(timezone.utc).isoformat(),
            "success": False,
            "error": SKLADBOT_DAILY_REPORT_STALE_FAILED_ERROR,
            "stage": stale_origin_stage or "stale failed",
            "stale_origin_stage": stale_origin_stage,
            "result_status": "failed",
            "stale_failed_at": now_utc.isoformat() if now_utc else datetime.now(timezone.utc).isoformat(),
        })
        event.payload = payload
        db.add(AuditLog(
            action="skladbot_daily_report_stale_failed",
            entity_type="pending_event",
            entity_id=str(event.id),
            payload={
                "event_type": event.event_type,
                "attempts": int(event.attempts or 0),
                "reason": SKLADBOT_DAILY_REPORT_STALE_FAILED_ERROR,
            },
        ))
        queue_scheduled_daily_failure_alert(
            db,
            event,
            getattr(self, "automation_alert_chat_id", ""),
            getattr(self, "admin_chat_ids", set()),
            SKLADBOT_DAILY_REPORT_STALE_FAILED_ERROR,
        )

    def update_scheduled_skladbot_daily_report_progress(self, event_id, stage, **fields):
        if not event_id:
            return
        try:
            event_uuid = event_id if isinstance(event_id, uuid.UUID) else uuid.UUID(str(event_id))
        except (TypeError, ValueError):
            return
        safe_fields = {}
        for key, value in (fields or {}).items():
            key_text = normalize_text(key)
            if not scheduled_skladbot_daily_report_payload_key_is_safe(key_text):
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe_fields[key_text] = value
        with self._scheduled_session_factory()() as db:
            event = db.get(PendingEvent, event_uuid)
            if event is None or event.status != "processing":
                return
            payload = safe_scheduled_skladbot_daily_report_payload(event.payload or {})
            payload.update(safe_fields)
            payload["stage"] = normalize_text(stage)
            payload["progress_updated_at"] = datetime.now(timezone.utc).isoformat()
            event.payload = payload
            db.commit()

    def finish_scheduled_skladbot_daily_report(self, event_id, success, error="", result_status=""):
        if not event_id:
            return
        with self._scheduled_session_factory()() as db:
            event = db.get(PendingEvent, event_id if isinstance(event_id, uuid.UUID) else uuid.UUID(str(event_id)))
            if event is None:
                return
            event.status = "completed" if success else "failed"
            event.last_error = "" if success else redact_secrets(normalize_text(error))
            payload = safe_scheduled_skladbot_daily_report_payload(event.payload or {})
            payload["finished_at"] = datetime.now(timezone.utc).isoformat()
            payload["success"] = bool(success)
            if success:
                payload["result_status"] = normalize_text(result_status) or "completed_sent"
            elif SKLADBOT_DAILY_REPORT_COVERAGE_FAILED_ERROR in normalize_text(error):
                payload["result_status"] = "blocked_partial"
            else:
                payload["result_status"] = "failed"
            if error:
                payload["error"] = redact_secrets(normalize_text(error))
            event.payload = payload
            if not success:
                queue_scheduled_daily_failure_alert(
                    db,
                    event,
                    getattr(self, "automation_alert_chat_id", ""),
                    getattr(self, "admin_chat_ids", set()),
                    error,
                )
            db.commit()

    def run_scheduled_daily_reconciliation(self, chat_id, report_date):
        if not getattr(self, "daily_reconciliation_enabled", False):
            return None
        admin_chat_ids = set(getattr(self, "admin_chat_ids", set()))
        configured_chat_ids = set(getattr(self, "daily_reconciliation_chat_ids", set()))
        alert_chat_ids = sorted((configured_chat_ids & admin_chat_ids) or admin_chat_ids)
        try:
            return self._daily_reconciliation_callback()(report_date=report_date, alert_chat_ids=alert_chat_ids)
        except Exception as exc:
            logging.exception("Telegram worker: scheduled daily reconciliation failed")
            return {
                "status": "failed",
                "error": normalize_text(exc) or exc.__class__.__name__,
            }

    def send_due_skladbot_daily_reports(self, now=None):
        now = now or datetime.now(self._skladbot_daily_report_module().business_timezone())
        scheduled_now = self.scheduled_skladbot_daily_report_is_due(now)
        enabled = getattr(self, "skladbot_daily_report_enabled", scheduled_now)
        if (
            not enabled
            or not getattr(self, "skladbot_daily_report_chat_ids", set())
        ):
            return 0
        sent = 0
        for chat_id in sorted(getattr(self, "skladbot_daily_report_chat_ids", set())):
            report_date = self.oldest_missing_skladbot_daily_report_date(chat_id, now=now)
            if report_date is None:
                continue
            event_id = self.claim_scheduled_skladbot_daily_report(chat_id, report_date, now=now)
            if not event_id:
                continue
            progress = lambda stage, **fields: self.update_scheduled_skladbot_daily_report_progress(event_id, stage, **fields)
            try:
                result = self.send_skladbot_daily_report(
                    chat_id,
                    report_date=report_date,
                    scheduled=True,
                    progress=progress,
                )
            except Exception as exc:
                error = redact_secrets(normalize_text(exc) or exc.__class__.__name__)
                logging.exception("Telegram worker: scheduled SkladBot daily report failed")
                self.finish_scheduled_skladbot_daily_report(event_id, False, error)
                continue
            if result == SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT:
                self.finish_scheduled_skladbot_daily_report(
                    event_id,
                    True,
                    result_status=SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT,
                )
                continue
            success = bool(result)
            self.finish_scheduled_skladbot_daily_report(event_id, success, "" if success else "telegram_send_failed")
            if success:
                self.run_scheduled_daily_reconciliation(chat_id, report_date)
                sent += 1
        return sent
