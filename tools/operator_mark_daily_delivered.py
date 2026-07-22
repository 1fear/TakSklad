#!/usr/bin/env python3
"""Record an explicit operator acknowledgement for reviewed daily previews.

This command never calls Telegram.  It keeps the no-send fact in the durable
event payload and only closes the configured daily delivery gap after an
operator has explicitly approved the override.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
import os

from sqlalchemy import select, text

from app.db import SessionLocal
from app.models import AuditLog, PendingEvent
from app.telegram_daily_report_policy import (
    SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
    completed_daily_report_delivery_exists,
)


MODE = "operator_ack"
VERSION = "v1-personal-preview"
RESULT_STATUS = "operator_marked_delivered"
ACTIVE_STATUSES = {"pending", "processing", "active"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-date", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        args.report_dates = tuple(date.fromisoformat(value) for value in args.report_date)
    except ValueError as exc:
        raise SystemExit("OPERATOR_DAILY_MARK_DATE_INVALID") from exc
    if len(args.report_dates) != 2 or len(set(args.report_dates)) != 2:
        raise SystemExit("OPERATOR_DAILY_MARK_EXACTLY_TWO_DATES_REQUIRED")
    if tuple(sorted(args.report_dates)) != args.report_dates:
        raise SystemExit("OPERATOR_DAILY_MARK_DATES_MUST_BE_SORTED")
    return args


def configured_chat_id() -> str:
    values = {
        part.strip()
        for part in os.environ.get("SKLADBOT_DAILY_REPORT_CHAT_IDS", "").replace(";", ",").split(",")
        if part.strip()
    }
    if len(values) != 1:
        raise RuntimeError("configured_daily_chat_count_invalid")
    value = next(iter(values))
    if not value.startswith("-") or not value[1:].isdigit() or int(value) == 0:
        raise RuntimeError("configured_daily_chat_route_invalid")
    return value


def marker_key(report_date: date, chat_id: str) -> str:
    return (
        f"skladbot_daily_report:{report_date.isoformat()}:{chat_id}:"
        f"{MODE}:daily_skladbot:{VERSION}"
    )


def marker_payload(report_date: date, now: datetime) -> dict[str, object]:
    return {
        "report_date": report_date.isoformat(),
        "mode": MODE,
        "kind": "daily_skladbot",
        "report_version": VERSION,
        "stage": "operator marked delivered",
        "success": True,
        "result_status": RESULT_STATUS,
        "client_send_performed_by_marker": False,
        "source_delivery": "personal_admin_preview",
        "marked_at": now.isoformat(),
    }


def related_events(db, report_date: date, chat_id: str):
    prefix = f"skladbot_daily_report:{report_date.isoformat()}:{chat_id}:"
    return db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE)
        .where(PendingEvent.idempotency_key.like(prefix + "%"))
        .order_by(PendingEvent.updated_at.desc(), PendingEvent.created_at.desc())
        .with_for_update()
    ).scalars().all()


def inspect_dates(report_dates: tuple[date, ...], chat_id: str) -> list[dict[str, object]]:
    result = []
    with SessionLocal() as db:
        for report_date in report_dates:
            events = related_events(db, report_date, chat_id)
            result.append({
                "report_date": report_date.isoformat(),
                "delivery_closed": completed_daily_report_delivery_exists(db, chat_id, report_date),
                "active_count": sum(
                    1 for event in events if str(event.status or "").strip() in ACTIVE_STATUSES
                ),
                "event_count": len(events),
                "operator_marker_present": any(
                    str(event.idempotency_key or "") == marker_key(report_date, chat_id)
                    for event in events
                ),
            })
    return result


def apply_markers(report_dates: tuple[date, ...], chat_id: str) -> list[dict[str, str]]:
    now = datetime.now(timezone.utc)
    outcomes = []
    with SessionLocal() as db:
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        db.execute(text("SET LOCAL statement_timeout = '15s'"))
        db.execute(text("LOCK TABLE pending_events IN ACCESS EXCLUSIVE MODE"))
        for report_date in report_dates:
            events = related_events(db, report_date, chat_id)
            if any(str(event.status or "").strip() in ACTIVE_STATUSES for event in events):
                raise RuntimeError("active_daily_delivery_exists")
            if completed_daily_report_delivery_exists(db, chat_id, report_date):
                outcomes.append({"report_date": report_date.isoformat(), "status": "already_completed"})
                continue

            key = marker_key(report_date, chat_id)
            exact = next((event for event in events if str(event.idempotency_key or "") == key), None)
            payload = marker_payload(report_date, now)
            if exact is None:
                event = PendingEvent(
                    event_type=SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
                    action="operator_mark_daily_delivered",
                    aggregate_type="report_date",
                    aggregate_id=report_date.isoformat(),
                    idempotency_key=key,
                    status="completed",
                    attempts=1,
                    payload=payload,
                    last_error="",
                    completed_at=now,
                )
                db.add(event)
                outcome = "marked"
            else:
                old_payload = exact.payload if isinstance(exact.payload, dict) else {}
                if (
                    str(exact.status or "") != "cancelled"
                    or old_payload.get("result_status") != "operator_mark_cancelled"
                ):
                    raise RuntimeError("unexpected_existing_operator_marker")
                event = exact
                event.status = "completed"
                event.attempts = int(event.attempts or 0) + 1
                event.payload = payload
                event.last_error = ""
                event.completed_at = now
                outcome = "remarked"

            db.flush()
            if not completed_daily_report_delivery_exists(db, chat_id, report_date):
                raise RuntimeError("operator_marker_not_recognized")
            db.add(AuditLog(
                actor_subject="operator-approved",
                action="skladbot_daily_report_operator_marked_delivered",
                entity_type="pending_event",
                entity_id=str(event.id),
                payload={
                    "report_date": report_date.isoformat(),
                    "mode": MODE,
                    "result_status": RESULT_STATUS,
                    "client_send_performed_by_marker": False,
                },
            ))
            outcomes.append({"report_date": report_date.isoformat(), "status": outcome})
        db.commit()
    return outcomes


def main() -> int:
    args = parse_args()
    if str(os.environ.get("TAKSKLAD_ENV") or "").strip().casefold() != "production":
        raise SystemExit("OPERATOR_DAILY_MARK_PRODUCTION_REQUIRED")
    chat_id = configured_chat_id()
    try:
        if args.apply:
            outcomes = apply_markers(args.report_dates, chat_id)
            inspection = inspect_dates(args.report_dates, chat_id)
            if any(not row["delivery_closed"] or row["active_count"] for row in inspection):
                raise RuntimeError("operator_marker_postcheck_failed")
            marker_writes = sum(
                1 for row in outcomes if row["status"] in {"marked", "remarked"}
            )
            result = {
                "status": "completed",
                "dates": outcomes,
                "delivery_marker_writes": marker_writes,
                "audit_log_writes": marker_writes,
            }
        else:
            result = {"status": "inspected", "dates": inspect_dates(args.report_dates, chat_id)}
    except Exception:
        print(json.dumps({"status": "blocked", "values_redacted": True}, sort_keys=True))
        return 2
    result.update({
        "configured_chat_count": 1,
        "client_sends": 0,
        "values_redacted": True,
    })
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
