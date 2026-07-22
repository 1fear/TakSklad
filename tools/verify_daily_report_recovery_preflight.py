#!/usr/bin/env python3
"""Fail-closed, redacted preflight for the two-date daily-report recovery."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable
from zoneinfo import ZoneInfo


RECOVERY_TIMEZONE = "Asia/Tashkent"
DAILY_EVENT_TYPE = "skladbot_daily_report_send"
REGISTRY_EVENT_TYPE = "skladbot_daily_reported_request"
ACTIVE_STATUSES = {"pending", "processing", "active"}
FAILURE_STATUSES = {"failed", "error", "blocked"}
SUCCESS_RESULT_STATUSES = {
    "CATCHUP_SENT_COMPLETE_ONCE",
    "completed_no_requests",
    "completed_sent",
}
COVERAGE_ERROR_PREFIX = "SKLADBOT_DAILY_REPORT_COVERAGE_NOT_COMPLETE"
PRE_TELEGRAM_STAGES = {
    "scheduled job started",
    "report generation finished",
    "xlsx created",
    "scheduled job failed",
}
DRY_RUN_FIELDS = {
    "status",
    "report_date",
    "requests_count",
    "order_kiz_count",
    "day_kiz_count",
    "xlsx_bytes",
}


class RecoveryPreflightError(RuntimeError):
    """The incident shape is not the one explicitly approved for recovery."""


def _normalize(value: Any) -> str:
    return str(value or "").strip()


def _parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(_normalize(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("report date must use YYYY-MM-DD") from exc
    if parsed.isoformat() != _normalize(value):
        raise argparse.ArgumentTypeError("report date must use YYYY-MM-DD")
    return parsed


def validate_recovery_dates(values: Iterable[date], *, today: date | None = None) -> tuple[date, date]:
    dates = tuple(values)
    if len(dates) != 2 or dates[0] >= dates[1]:
        raise RecoveryPreflightError("exactly two sorted distinct dates are required")
    business_today = today or datetime.now(ZoneInfo(RECOVERY_TIMEZONE)).date()
    if any(value >= business_today for value in dates):
        raise RecoveryPreflightError("recovery dates must be in the past")
    return dates[0], dates[1]


def _chat_from_event(event: Any) -> str:
    key = _normalize(getattr(event, "idempotency_key", ""))
    parts = key.split(":")
    if len(parts) >= 3 and parts[0] == "skladbot_daily_report":
        return parts[2].strip()
    payload = event.payload if isinstance(getattr(event, "payload", None), dict) else {}
    return _normalize(payload.get("chat_id"))


def _date_from_event(event: Any) -> str:
    payload = event.payload if isinstance(getattr(event, "payload", None), dict) else {}
    value = _normalize(payload.get("report_date"))
    if value:
        return value
    key = _normalize(getattr(event, "idempotency_key", ""))
    parts = key.split(":")
    return parts[1].strip() if len(parts) >= 2 else ""


def _event_is_success(event: Any) -> bool:
    if _normalize(getattr(event, "status", "")) != "completed":
        return False
    payload = event.payload if isinstance(getattr(event, "payload", None), dict) else {}
    success = payload.get("success")
    return (
        success is True
        or _normalize(success).casefold() == "true"
        or _normalize(payload.get("result_status")) in SUCCESS_RESULT_STATUSES
    )


def _event_is_proven_pre_telegram_failure(event: Any) -> bool:
    if _normalize(getattr(event, "status", "")) not in FAILURE_STATUSES:
        return False
    payload = event.payload if isinstance(getattr(event, "payload", None), dict) else {}
    stage = _normalize(payload.get("stage"))
    error = _normalize(getattr(event, "last_error", ""))
    result_status = _normalize(payload.get("result_status"))
    success = payload.get("success")
    return (
        stage in PRE_TELEGRAM_STAGES
        and stage == "scheduled job failed"
        and error.startswith(COVERAGE_ERROR_PREFIX)
        and result_status == "blocked_partial"
        and success is not True
        and _normalize(success).casefold() != "true"
    )


def inspect_database(report_dates: tuple[date, date]) -> dict[str, Any]:
    """Inspect production DB read-only and return only counts and approved dates."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.health_service import (
        TERMINAL_INCIDENT_STATUSES,
        count_unresolved_hot_path_failures,
        daily_report_failure_resolved_by_later_success,
    )
    from app.models import Incident, PendingEvent

    dates = tuple(value.isoformat() for value in report_dates)
    configured_chats = {
        value.strip()
        for value in os.environ.get("SKLADBOT_DAILY_REPORT_CHAT_IDS", "").replace(";", ",").split(",")
        if value.strip()
    }
    schedule_ok = (
        _normalize(os.environ.get("SKLADBOT_DAILY_REPORT_ENABLED")).casefold()
        in {"1", "true", "yes", "on", "да"}
        and _normalize(os.environ.get("TAKSKLAD_TIMEZONE") or RECOVERY_TIMEZONE)
        == RECOVERY_TIMEZONE
        and _normalize(os.environ.get("SKLADBOT_DAILY_REPORT_HOUR") or "22") == "22"
        and _normalize(os.environ.get("SKLADBOT_DAILY_REPORT_MINUTE") or "0") == "0"
    )
    counts_by_date = {value: 0 for value in dates}
    blocker_count = 0
    active_count = 0
    success_count = 0
    registry_count = 0
    ambiguous_count = 0

    with SessionLocal() as db:
        terminal_incident_ids = select(Incident.pending_event_id).where(
            Incident.pending_event_id.is_not(None),
            Incident.status.in_(TERMINAL_INCIDENT_STATUSES),
        )
        failure_events = db.execute(
            select(PendingEvent)
            .where(PendingEvent.status.in_(tuple(FAILURE_STATUSES)))
            .where(~PendingEvent.id.in_(terminal_incident_ids))
        ).scalars().all()
        unresolved_events = []
        for event in failure_events:
            if (
                event.event_type == DAILY_EVENT_TYPE
                and daily_report_failure_resolved_by_later_success(db, event)
            ):
                continue
            unresolved_events.append(event)

        blocker_count = int(count_unresolved_hot_path_failures(db) or 0)
        if blocker_count != len(unresolved_events):
            ambiguous_count += 1

        configured_chat = next(iter(configured_chats)) if len(configured_chats) == 1 else ""
        for event in unresolved_events:
            event_date = _date_from_event(event)
            if (
                event.event_type != DAILY_EVENT_TYPE
                or event_date not in counts_by_date
                or not configured_chat
                or _chat_from_event(event) != configured_chat
                or not _event_is_proven_pre_telegram_failure(event)
            ):
                ambiguous_count += 1
                continue
            counts_by_date[event_date] += 1

        daily_events = db.execute(
            select(PendingEvent).where(PendingEvent.event_type == DAILY_EVENT_TYPE)
        ).scalars().all()
        for event in daily_events:
            event_date = _date_from_event(event)
            event_chat = _chat_from_event(event)
            status = _normalize(event.status)
            matches_approved_pair = (
                event_date in counts_by_date
                and len(configured_chats) == 1
                and event_chat == configured_chat
            )
            if status in ACTIVE_STATUSES:
                active_count += 1
            if matches_approved_pair and _event_is_success(event):
                success_count += 1
            if matches_approved_pair and (
                status == "completed" and not _event_is_success(event)
            ):
                ambiguous_count += 1
            if matches_approved_pair and status in FAILURE_STATUSES and not _event_is_proven_pre_telegram_failure(event):
                ambiguous_count += 1

        registry_events = db.execute(
            select(PendingEvent).where(PendingEvent.event_type == REGISTRY_EVENT_TYPE)
        ).scalars().all()
        for event in registry_events:
            key = _normalize(event.idempotency_key)
            parts = key.split(":")
            if (
                len(parts) >= 3
                and parts[0] == REGISTRY_EVENT_TYPE
                and parts[1] in counts_by_date
                and len(configured_chats) == 1
                and parts[2] == configured_chat
            ):
                registry_count += 1

    safe = (
        len(configured_chats) == 1
        and schedule_ok
        and blocker_count > 0
        and all(counts_by_date[value] > 0 for value in dates)
        and sum(counts_by_date.values()) == blocker_count
        and active_count == 0
        and success_count == 0
        and registry_count == 0
        and ambiguous_count == 0
    )
    return {
        "status": "inspect_ok" if safe else "blocked",
        "report_dates": list(dates),
        "configured_chat_count": len(configured_chats),
        "schedule_2200_tashkent": schedule_ok,
        "blocker_count": blocker_count,
        "blockers_by_date": counts_by_date,
        "active_count": active_count,
        "success_count": success_count,
        "registry_count": registry_count,
        "ambiguous_count": ambiguous_count,
        "values_redacted": True,
    }


def _load_json(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RecoveryPreflightError("JSON object required")
    return payload


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RecoveryPreflightError("nonnegative integer required")
    return value


def verify_preflight(
    *,
    report_dates: tuple[date, date],
    ready: dict[str, Any],
    database: dict[str, Any],
    dry_runs: list[dict[str, Any]],
    today: date | None = None,
) -> dict[str, Any]:
    dates = validate_recovery_dates(report_dates, today=today)
    date_texts = [value.isoformat() for value in dates]
    if ready.get("http_status") != 503 or not isinstance(ready.get("payload"), dict):
        raise RecoveryPreflightError("current readiness shape is not recoverable")
    payload = ready["payload"]
    queue = payload.get("queue") or {}
    imports = payload.get("imports") or {}
    workers = payload.get("workers") or {}
    daily = payload.get("daily_report") or {}
    pairing = payload.get("desktop_pairing") or {}
    policy = payload.get("policy") or {}
    if not (
        payload.get("ready") is False
        and (payload.get("database") or {}).get("status") == "ok"
        and (payload.get("migrations") or {}).get("status") == "ok"
        and _nonnegative_int(queue.get("hot_path_stale_processing_count")) == 0
        and _nonnegative_int(imports.get("recent_error_count")) == 0
        and workers.get("status") == "ok"
        and _nonnegative_int(workers.get("required_count")) == 3
        and _nonnegative_int(workers.get("missing_count")) == 0
        and _nonnegative_int(workers.get("unhealthy_count")) == 0
        and daily.get("status") == "unhealthy"
        and daily.get("due_date") == date_texts[1]
        and _nonnegative_int(daily.get("missing_count")) == 1
        and pairing.get("status") == "ok"
        and _nonnegative_int(pairing.get("overdue_unacked_count")) == 0
        and _nonnegative_int(pairing.get("stale_cleanup_count")) == 0
        and pairing.get("sweeper_heartbeat_stale") is False
        and policy.get("mandatory_status") == "unhealthy"
    ):
        raise RecoveryPreflightError("unrelated readiness failure exists")

    if database.get("status") != "inspect_ok" or database.get("values_redacted") is not True:
        raise RecoveryPreflightError("database inspection is blocked")
    if database.get("report_dates") != date_texts:
        raise RecoveryPreflightError("database inspection dates differ")
    if database.get("configured_chat_count") != 1 or database.get("schedule_2200_tashkent") is not True:
        raise RecoveryPreflightError("daily configuration differs")
    blocker_count = _nonnegative_int(database.get("blocker_count"))
    blockers_by_date = database.get("blockers_by_date")
    if not isinstance(blockers_by_date, dict) or set(blockers_by_date) != set(date_texts):
        raise RecoveryPreflightError("daily blocker dates differ")
    if (
        blocker_count <= 0
        or sum(_nonnegative_int(blockers_by_date[value]) for value in date_texts) != blocker_count
        or any(_nonnegative_int(blockers_by_date[value]) <= 0 for value in date_texts)
        or _nonnegative_int(database.get("active_count")) != 0
        or _nonnegative_int(database.get("success_count")) != 0
        or _nonnegative_int(database.get("registry_count")) != 0
        or _nonnegative_int(database.get("ambiguous_count")) != 0
        or _nonnegative_int(queue.get("hot_path_blocking_count")) != blocker_count
        or _nonnegative_int(queue.get("hot_path_error_count")) != blocker_count
    ):
        raise RecoveryPreflightError("daily blocker proof differs")

    if len(dry_runs) != 2:
        raise RecoveryPreflightError("exactly two dry runs are required")
    for expected_date, dry_run in zip(date_texts, dry_runs):
        if set(dry_run) - DRY_RUN_FIELDS:
            raise RecoveryPreflightError("dry run emitted non-contract fields")
        if dry_run.get("status") != "ready" or dry_run.get("report_date") != expected_date:
            raise RecoveryPreflightError("dry run is not send-ready")
        for field in ("requests_count", "order_kiz_count", "day_kiz_count"):
            _nonnegative_int(dry_run.get(field))
        if _nonnegative_int(dry_run.get("xlsx_bytes")) <= 0:
            raise RecoveryPreflightError("dry run workbook is empty")

    return {
        "status": "ready",
        "dates_count": 2,
        "dry_run_count": 2,
        "blocker_count": blocker_count,
        "values_redacted": True,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--report-date", action="append", type=_parse_date, required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--report-date", action="append", type=_parse_date, required=True)
    verify_parser.add_argument("--ready-json", required=True)
    verify_parser.add_argument("--database-json", required=True)
    verify_parser.add_argument("--dry-run-json", action="append", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report_dates = validate_recovery_dates(tuple(args.report_date))
        if args.command == "inspect":
            result = inspect_database(report_dates)
            exit_code = 0 if result.get("status") == "inspect_ok" else 2
        else:
            result = verify_preflight(
                report_dates=report_dates,
                ready=_load_json(args.ready_json),
                database=_load_json(args.database_json),
                dry_runs=[_load_json(path) for path in args.dry_run_json],
            )
            exit_code = 0
    except Exception:
        result = {"status": "blocked", "values_redacted": True}
        exit_code = 2
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
