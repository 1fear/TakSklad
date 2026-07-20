#!/usr/bin/env python3
"""Fail-closed recovery for one Telegram Excel import rejected by backend auth."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import sys
from types import SimpleNamespace
import uuid

from sqlalchemy import func, select

try:
    from app.db import SessionLocal
    from app.event_queue_service import retry_event_queue_event
    from app.health_service import count_unresolved_hot_path_failures
    from app.models import AuditLog, ImportJob, Incident, PendingEvent
except ImportError:  # Local test/import path.
    from backend.app.db import SessionLocal
    from backend.app.event_queue_service import retry_event_queue_event
    from backend.app.health_service import count_unresolved_hot_path_failures
    from backend.app.models import AuditLog, ImportJob, Incident, PendingEvent


EVENT_TYPE = "telegram_excel_import"
RECOVERY_REASON = "backend_service_identity_401_recovered"
APPROVAL = "RETRY_ONE_TELEGRAM_IMPORT_AUTH_401"
RECENT_WINDOW = timedelta(hours=12)


class RecoveryBlocked(RuntimeError):
    pass


def _utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _event_uuid(value: str | None):
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise RecoveryBlocked("event_id_invalid") from exc


def _import_count(db, event_id) -> int:
    return int(db.execute(
        select(func.count(ImportJob.id)).where(
            ImportJob.raw_payload["telegram_event_id"].as_string() == str(event_id)
        )
    ).scalar_one() or 0)


def _matching_event(db, event_id=None, *, now=None):
    now = _utc(now) or datetime.now(timezone.utc)
    query = (
        select(PendingEvent)
        .where(PendingEvent.event_type == EVENT_TYPE)
        .where(PendingEvent.status == "failed")
        .where(PendingEvent.created_at >= now - RECENT_WINDOW)
        .order_by(PendingEvent.created_at.desc(), PendingEvent.id.desc())
    )
    parsed_id = _event_uuid(event_id)
    if parsed_id is not None:
        query = query.where(PendingEvent.id == parsed_id)
    candidates = list(db.execute(query).scalars())
    candidates = [
        event
        for event in candidates
        if "401 Unauthorized" in str(event.last_error or "")
        and "/api/v1/imports" in str(event.last_error or "")
    ]
    if len(candidates) != 1:
        raise RecoveryBlocked("exactly_one_auth_failure_required")
    event = candidates[0]
    payload = event.payload if isinstance(event.payload, dict) else {}
    document = payload.get("document") if isinstance(payload.get("document"), dict) else {}
    if not str(document.get("file_id") or "").strip():
        raise RecoveryBlocked("telegram_source_file_unavailable")
    if _import_count(db, event.id) != 0:
        raise RecoveryBlocked("existing_import_detected")
    if count_unresolved_hot_path_failures(db) != 1:
        raise RecoveryBlocked("unrelated_hot_path_blocker_present")
    return event


def inspect(db, event_id=None, *, now=None):
    event = _matching_event(db, event_id, now=now)
    return {
        "status": "recoverable",
        "event_id": str(event.id),
        "event_type": EVENT_TYPE,
        "event_status": event.status,
        "attempts": int(event.attempts or 0),
        "linked_imports": 0,
        "source_available": True,
        "unrelated_blockers": 0,
        "values_redacted": True,
    }


def retry(db, event_id, *, approval: str):
    if approval != APPROVAL:
        raise RecoveryBlocked("exact_retry_approval_required")
    event = _matching_event(db, event_id)
    result = retry_event_queue_event(
        db,
        event.id,
        SimpleNamespace(
            reason=RECOVERY_REASON,
            actor="production_recovery",
            source="verified_server_deploy",
            idempotency_key=f"telegram-import-auth-recovery:{event.id}",
        ),
    )
    return {
        "status": "retry_requested",
        "event_id": str(event.id),
        "event_status": result["status"],
        "linked_imports_before": 0,
        "values_redacted": True,
    }


def verify(db, event_id):
    parsed_id = _event_uuid(event_id)
    event = db.get(PendingEvent, parsed_id)
    if event is None or event.event_type != EVENT_TYPE:
        raise RecoveryBlocked("event_not_found")
    import_count = _import_count(db, event.id)
    if event.status != "completed" or import_count != 1:
        raise RecoveryBlocked("completion_not_verified")
    import_job = db.execute(
        select(ImportJob).where(
            ImportJob.raw_payload["telegram_event_id"].as_string() == str(event.id)
        )
    ).scalar_one()
    payload = import_job.raw_payload if isinstance(import_job.raw_payload, dict) else {}
    return {
        "status": "completed",
        "event_id": str(event.id),
        "event_status": event.status,
        "linked_imports": import_count,
        "import_status": import_job.status,
        "orders_created": int(payload.get("orders_created") or 0),
        "items_created": int(payload.get("items_created") or import_job.rows_imported or 0),
        "duplicate_rows": int(payload.get("duplicate_rows") or 0),
        "invalid_rows": int(payload.get("invalid_rows") or 0),
        "values_redacted": True,
    }


def finalize(db, event_id, *, approval: str):
    if approval != APPROVAL:
        raise RecoveryBlocked("exact_retry_approval_required")
    summary = verify(db, event_id)
    now = datetime.now(timezone.utc)
    incidents = list(db.execute(
        select(Incident).where(Incident.pending_event_id == _event_uuid(event_id)).with_for_update()
    ).scalars())
    resolved = 0
    for incident in incidents:
        if incident.status in {"resolved", "ignored", "cancelled"}:
            continue
        incident.status = "resolved"
        incident.resolved_at = now
        resolved += 1
    db.add(AuditLog(
        action="telegram_excel_import_auth_recovery_completed",
        entity_type="pending_event",
        entity_id=str(event_id),
        payload={
            "event_type": EVENT_TYPE,
            "linked_imports": summary["linked_imports"],
            "import_status": summary["import_status"],
            "incidents_resolved": resolved,
            "values_redacted": True,
        },
    ))
    db.commit()
    return {**summary, "incidents_resolved": resolved}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect", "retry", "verify", "finalize"))
    parser.add_argument("--event-id", default="")
    parser.add_argument("--approval", default="")
    return parser


def main(argv=None, *, session_factory=None):
    args = build_parser().parse_args(argv)
    session_factory = session_factory or SessionLocal
    try:
        with session_factory() as db:
            if args.command == "inspect":
                result = inspect(db, args.event_id or None)
            elif args.command == "retry":
                result = retry(db, args.event_id, approval=args.approval)
            elif args.command == "verify":
                result = verify(db, args.event_id)
            else:
                result = finalize(db, args.event_id, approval=args.approval)
    except RecoveryBlocked as exc:
        print(f"TELEGRAM_IMPORT_AUTH_RECOVERY_BLOCKED reason={exc}", file=sys.stderr)
        return 3
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
