"""Durable, fail-closed one-off delivery for a missed SkladBot daily report."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date, datetime, timezone
from typing import Any, Callable, TextIO

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .models import AuditLog, PendingEvent
from .redaction import redact_secrets
from .telegram_common import normalize_text
from .telegram_daily_report_policy import (
    SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT,
    SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
    SKLADBOT_DAILY_SAFE_RETRY_STAGES,
    completed_daily_report_delivery_exists,
)


MANUAL_DAILY_CATCHUP_MODE = "manual_catchup"
MANUAL_DAILY_CATCHUP_VERSION = "v4-combined-kiz"
MANUAL_DAILY_CATCHUP_SUCCESS = "CATCHUP_SENT_COMPLETE_ONCE"
MANUAL_DAILY_CATCHUP_ACTIVE_STATUSES = {"pending", "processing", "active"}
SKLADBOT_DAILY_REPORTED_REQUEST_EVENT_TYPE = "skladbot_daily_reported_request"
MANUAL_DAILY_CATCHUP_PRE_TELEGRAM_STAGES = frozenset(SKLADBOT_DAILY_SAFE_RETRY_STAGES)
MANUAL_DAILY_CATCHUP_CLI_FIELDS = (
    "status",
    "report_date",
    "requests_count",
    "order_kiz_count",
    "day_kiz_count",
    "xlsx_bytes",
)


class ManualDailyCatchupConfigurationError(RuntimeError):
    """Configuration is unsafe for a single-target catch-up."""


def claim_manual_daily_catchup(sender: Any, chat_id: str, report_date: date) -> dict[str, str]:
    """Claim one date/chat before Telegram; never reclaim an ambiguous attempt."""
    report_date = _report_date(report_date)
    key = sender.skladbot_daily_report_idempotency_key(
        chat_id,
        report_date,
        mode=MANUAL_DAILY_CATCHUP_MODE,
        report_kind="daily_skladbot",
        report_version=MANUAL_DAILY_CATCHUP_VERSION,
    )
    prefix = f"skladbot_daily_report:{report_date.isoformat()}:{chat_id}:"
    now = datetime.now(timezone.utc)
    try:
        with sender._scheduled_session_factory()() as db:
            if completed_daily_report_delivery_exists(db, chat_id, report_date):
                return {"status": "already_completed", "event_id": ""}

            if _reported_request_registry_exists(db, chat_id, report_date):
                return {"status": "already_reported", "event_id": ""}

            related = db.execute(
                select(PendingEvent)
                .where(PendingEvent.event_type == SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE)
                .where(PendingEvent.idempotency_key.like(prefix + "%"))
                .order_by(PendingEvent.updated_at.desc(), PendingEvent.created_at.desc())
                .with_for_update()
            ).scalars().all()
            exact_event = None
            for event in related:
                if normalize_text(event.idempotency_key) == key:
                    exact_event = event
                status = normalize_text(event.status)
                payload = event.payload if isinstance(event.payload, dict) else {}
                stage = normalize_text(payload.get("stage"))
                if status in MANUAL_DAILY_CATCHUP_ACTIVE_STATUSES:
                    return {"status": "active_delivery_exists", "event_id": str(event.id)}
                if (
                    status in {"failed", "error", "blocked", "dead", "cancelled"}
                    and stage not in MANUAL_DAILY_CATCHUP_PRE_TELEGRAM_STAGES
                ):
                    return {"status": "ambiguous_delivery_exists", "event_id": str(event.id)}
                if status == "completed":
                    # A successful completion was handled above. Any other
                    # completion shape is not proof that Telegram was untouched.
                    return {"status": "ambiguous_delivery_exists", "event_id": str(event.id)}

            if exact_event is not None:
                return {"status": "already_claimed", "event_id": str(exact_event.id)}

            event = PendingEvent(
                event_type=SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
                action="manual_daily_catchup",
                aggregate_type="report_date",
                aggregate_id=report_date.isoformat(),
                idempotency_key=key,
                status="processing",
                attempts=1,
                payload={
                    "report_date": report_date.isoformat(),
                    "mode": MANUAL_DAILY_CATCHUP_MODE,
                    "kind": "daily_skladbot",
                    "report_version": MANUAL_DAILY_CATCHUP_VERSION,
                    "stage": "manual catchup claimed",
                    "claimed_at": now.isoformat(),
                },
            )
            db.add(event)
            db.flush()
            db.add(AuditLog(
                action="skladbot_daily_report_manual_catchup_claimed",
                entity_type="pending_event",
                entity_id=str(event.id),
                payload={
                    "report_date": report_date.isoformat(),
                    "report_version": MANUAL_DAILY_CATCHUP_VERSION,
                },
            ))
            db.commit()
            return {"status": "claimed", "event_id": str(event.id)}
    except IntegrityError:
        return {"status": "already_claimed", "event_id": ""}


def _reported_request_registry_exists(db: Any, chat_id: str, report_date: date) -> bool:
    prefix = (
        f"{SKLADBOT_DAILY_REPORTED_REQUEST_EVENT_TYPE}:"
        f"{report_date.isoformat()}:{chat_id}:"
    )
    event = db.execute(
        select(PendingEvent.id)
        .where(PendingEvent.event_type == SKLADBOT_DAILY_REPORTED_REQUEST_EVENT_TYPE)
        .where(PendingEvent.status == "completed")
        .where(PendingEvent.idempotency_key.like(prefix + "%"))
        .limit(1)
    ).scalar_one_or_none()
    return event is not None


def run_manual_daily_catchup(sender: Any, chat_id: str, report_date: date) -> dict[str, Any]:
    """Send one summary and one combined XLSX, with no reconciliation or retry."""
    report_date = _report_date(report_date)
    claim = claim_manual_daily_catchup(sender, chat_id, report_date)
    if claim["status"] != "claimed":
        return {
            **claim,
            "report_date": report_date.isoformat(),
            "sent": False,
        }

    event_id = claim["event_id"]
    progress_state = {
        "sendMessage_success_count": 0,
        "sendDocument_success_count": 0,
        "reported_count": 0,
        "combined_empty": False,
        "requests_count": 0,
        "order_kiz_count": 0,
        "day_kiz_count": 0,
        "xlsx_bytes": 0,
    }

    def progress(stage: str, **fields: Any) -> None:
        sender.update_scheduled_skladbot_daily_report_progress(event_id, stage, **fields)
        if stage == "telegram sendMessage success":
            progress_state["sendMessage_success_count"] += 1
        elif stage == "telegram sendDocument success":
            progress_state["sendDocument_success_count"] += 1
        elif stage == "reported mark success":
            progress_state["reported_count"] = max(
                0,
                _safe_nonnegative_int(fields.get("reported_count")),
            )
        elif stage == "scheduled job no requests":
            progress_state["combined_empty"] = fields.get("combined_empty") is True
        _capture_safe_progress_counts(progress_state, fields)

    try:
        result = sender.send_skladbot_daily_report(
            chat_id,
            report_date=report_date,
            scheduled=True,
            progress=progress,
            delivery_mode=MANUAL_DAILY_CATCHUP_MODE,
        )
    except Exception as exc:
        _block_manual_daily_catchup(sender, event_id, exc)
        return {
            "status": "manual_recovery_required",
            "event_id": event_id,
            "report_date": report_date.isoformat(),
            "sent": False,
            **_public_progress_counts(progress_state),
        }

    if result == SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT:
        if progress_state["combined_empty"] is not True:
            _block_manual_daily_catchup(
                sender,
                event_id,
                "combined_empty_not_confirmed",
            )
            return {
                "status": "manual_recovery_required",
                "event_id": event_id,
                "report_date": report_date.isoformat(),
                "sent": False,
                **_public_progress_counts(progress_state),
            }
        _finish_manual_daily_catchup(
            sender,
            event_id,
            result_status=SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT,
            sent=False,
            registry_marked=False,
        )
        return {
            "status": SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT,
            "event_id": event_id,
            "report_date": report_date.isoformat(),
            "sent": False,
            **_public_progress_counts(progress_state),
        }
    if result is not True:
        _block_manual_daily_catchup(sender, event_id, "telegram_send_failed")
        return {
            "status": "manual_recovery_required",
            "event_id": event_id,
            "report_date": report_date.isoformat(),
            "sent": False,
            **_public_progress_counts(progress_state),
        }
    if (
        progress_state["sendMessage_success_count"] != 1
        or progress_state["sendDocument_success_count"] != 1
    ):
        _block_manual_daily_catchup(sender, event_id, "unexpected_telegram_send_counts")
        return {
            "status": "manual_recovery_required",
            "event_id": event_id,
            "report_date": report_date.isoformat(),
            "sent": False,
            **_public_progress_counts(progress_state),
        }

    _finish_manual_daily_catchup(
        sender,
        event_id,
        result_status=MANUAL_DAILY_CATCHUP_SUCCESS,
        sent=True,
        registry_marked=progress_state["reported_count"] > 0,
    )
    return {
        "status": MANUAL_DAILY_CATCHUP_SUCCESS,
        "event_id": event_id,
        "report_date": report_date.isoformat(),
        "sent": True,
        **_public_progress_counts(progress_state),
    }


def _finish_manual_daily_catchup(
    sender: Any,
    event_id: str,
    *,
    result_status: str,
    sent: bool,
    registry_marked: bool,
) -> None:
    now = datetime.now(timezone.utc)
    with sender._scheduled_session_factory()() as db:
        event = db.get(PendingEvent, uuid.UUID(str(event_id)))
        if event is None or normalize_text(event.status) != "processing":
            return
        payload = dict(event.payload or {})
        payload.update({
            "stage": "manual catchup completed",
            "finished_at": now.isoformat(),
            "success": True,
            "result_status": result_status,
            "sendMessage_count": 1 if sent else 0,
            "sendDocument_count": 1 if sent else 0,
            "registry_marked_after_success": bool(registry_marked),
            "reconciliation_started": False,
        })
        event.status = "completed"
        event.completed_at = now
        event.last_error = ""
        event.payload = payload
        db.add(AuditLog(
            action="skladbot_daily_report_manual_catchup_completed",
            entity_type="pending_event",
            entity_id=str(event.id),
            payload={
                "report_date": normalize_text(payload.get("report_date")),
                "result_status": result_status,
            },
        ))
        db.commit()


def _block_manual_daily_catchup(sender: Any, event_id: str, error: Any) -> None:
    now = datetime.now(timezone.utc)
    safe_error = redact_secrets(normalize_text(error) or error.__class__.__name__)
    with sender._scheduled_session_factory()() as db:
        event = db.get(PendingEvent, uuid.UUID(str(event_id)))
        if event is None or normalize_text(event.status) != "processing":
            return
        payload = dict(event.payload or {})
        origin_stage = normalize_text(payload.get("stage"))
        payload.update({
            "stage": "manual_recovery_required",
            "finished_at": now.isoformat(),
            "success": False,
            "result_status": "manual_recovery_required",
            "manual_recovery_required": True,
            "manual_recovery_reason": "manual_catchup_failed",
            "origin_stage": origin_stage,
            "error": safe_error,
            "reconciliation_started": False,
        })
        event.status = "blocked"
        event.completed_at = now
        event.last_error = safe_error
        event.payload = payload
        db.add(AuditLog(
            action="skladbot_daily_report_manual_catchup_blocked",
            entity_type="pending_event",
            entity_id=str(event.id),
            payload={
                "report_date": normalize_text(payload.get("report_date")),
                "origin_stage": origin_stage,
            },
        ))
        db.commit()


def dry_run_manual_daily_catchup(sender: Any, report_date: date) -> dict[str, Any]:
    """Build the exact combined workbook without claims, writes, or Telegram I/O."""
    report_date = _report_date(report_date)
    prepared = sender.prepare_skladbot_daily_report(
        report_date=report_date,
        scheduled=True,
        build_for_dry_run=True,
    )
    if not isinstance(prepared, dict):
        raise RuntimeError("invalid_prepared_daily_report")
    report = prepared.get("report")
    if not isinstance(report, dict):
        raise RuntimeError("invalid_prepared_daily_report")
    content = prepared.get("content")
    if not isinstance(content, (bytes, bytearray)):
        raise RuntimeError("invalid_prepared_daily_workbook")

    prepared_date = prepared.get("report_date") or report.get("report_date") or report_date
    prepared_date = _report_date(prepared_date)
    requests_count = len(report.get("requests") or [])
    order_kiz_count = len(report.get("request_kiz_rows") or [])
    day_kiz_count = len(report.get("daily_kiz_rows") or [])
    blocker = normalize_text(prepared.get("blocker"))
    if blocker:
        status = "blocked"
    elif requests_count == 0 and order_kiz_count == 0 and day_kiz_count == 0:
        status = SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT
    else:
        status = "ready"
    return {
        "status": status,
        "report_date": prepared_date.isoformat(),
        "requests_count": requests_count,
        "order_kiz_count": order_kiz_count,
        "day_kiz_count": day_kiz_count,
        "xlsx_bytes": len(content),
    }


def configured_daily_chat_id(sender: Any) -> str:
    """Return the only configured target without ever rendering it in errors."""
    chat_ids = {
        normalize_text(value)
        for value in getattr(sender, "skladbot_daily_report_chat_ids", set()) or set()
        if normalize_text(value)
    }
    if len(chat_ids) != 1:
        raise ManualDailyCatchupConfigurationError(
            "exactly_one_daily_report_chat_required"
        )
    return next(iter(chat_ids))


def _capture_safe_progress_counts(state: dict[str, Any], fields: dict[str, Any]) -> None:
    for field in ("requests_count", "order_kiz_count", "day_kiz_count"):
        if field in fields:
            state[field] = _safe_nonnegative_int(fields.get(field))
    if "bytes" in fields:
        state["xlsx_bytes"] = _safe_nonnegative_int(fields.get("bytes"))


def _public_progress_counts(state: dict[str, Any]) -> dict[str, int]:
    return {
        "requests_count": _safe_nonnegative_int(state.get("requests_count")),
        "order_kiz_count": _safe_nonnegative_int(state.get("order_kiz_count")),
        "day_kiz_count": _safe_nonnegative_int(state.get("day_kiz_count")),
        "xlsx_bytes": _safe_nonnegative_int(state.get("xlsx_bytes")),
    }


def _safe_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _report_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError("report_date must be a date")


def _parse_iso_report_date(value: str) -> date:
    try:
        return date.fromisoformat(normalize_text(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("report date must use YYYY-MM-DD") from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail-closed one-date SkladBot daily catch-up",
    )
    parser.add_argument(
        "--report-date",
        required=True,
        type=_parse_iso_report_date,
        help="One report date in YYYY-MM-DD format",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect, hydrate, and build the combined workbook without writes or sends",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Claim and send exactly one daily message and one combined workbook",
    )
    return parser.parse_args(argv)


def _default_worker_factory() -> Any:
    from .telegram_worker import TelegramWorker

    return TelegramWorker()


def _cli_payload(result: dict[str, Any]) -> dict[str, Any]:
    return {
        field: result[field]
        for field in MANUAL_DAILY_CATCHUP_CLI_FIELDS
        if field in result
    }


def _write_cli_payload(output: TextIO, result: dict[str, Any]) -> None:
    output.write(json.dumps(_cli_payload(result), ensure_ascii=False, sort_keys=True))
    output.write("\n")


def main(
    argv: list[str] | None = None,
    *,
    worker_factory: Callable[[], Any] | None = None,
    output: TextIO | None = None,
) -> int:
    args = parse_args(argv)
    output = output or sys.stdout
    try:
        sender = (worker_factory or _default_worker_factory)()
        chat_id = configured_daily_chat_id(sender)
        if args.dry_run:
            result = dry_run_manual_daily_catchup(sender, args.report_date)
        else:
            result = run_manual_daily_catchup(sender, chat_id, args.report_date)
    except ManualDailyCatchupConfigurationError:
        _write_cli_payload(output, {"status": "configuration_error"})
        return 2
    except Exception:
        _write_cli_payload(output, {"status": "failed"})
        return 1

    _write_cli_payload(output, result)
    if normalize_text(result.get("status")) in {
        "ready",
        "already_completed",
        "already_reported",
        MANUAL_DAILY_CATCHUP_SUCCESS,
        SKLADBOT_DAILY_REPORT_NO_REQUESTS_RESULT,
    }:
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
