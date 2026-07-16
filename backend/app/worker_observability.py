"""Worker heartbeat persistence driven only by real main-loop cycles."""

from contextlib import contextmanager
from datetime import datetime, timezone
import logging
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import WorkerHeartbeat
from .observability_context import bind_correlation_id, current_correlation_id, log_trace, reset_correlation_id


DEFAULT_GRACE_SECONDS = 15
LOGGER = logging.getLogger(__name__)
KNOWN_WORKERS = (
    "skladbot",
    "smartup_auto_import",
    "telegram",
)


def record_cycle_start(
    worker_name: str,
    interval_seconds: int,
    *,
    grace_seconds: int = DEFAULT_GRACE_SECONDS,
    session_factory=SessionLocal,
    now: datetime | None = None,
) -> str:
    correlation_id = current_correlation_id()
    timestamp = now or datetime.now(timezone.utc)
    with session_factory() as db:
        row = db.get(WorkerHeartbeat, worker_name)
        if row is None:
            row = WorkerHeartbeat(worker_name=worker_name)
            db.add(row)
        row.interval_seconds = max(1, int(interval_seconds))
        row.grace_seconds = max(0, int(grace_seconds))
        row.status = "running"
        row.correlation_id = correlation_id
        row.last_cycle_started_at = timestamp
        row.last_progress_at = timestamp
        row.last_progress_phase = "cycle_started"
        row.last_error_class = None
        db.commit()
    log_trace(LOGGER, "worker_cycle_started", worker=worker_name)
    return correlation_id


def record_cycle_progress(
    worker_name: str,
    phase: str,
    *,
    correlation_id: str,
    session_factory=SessionLocal,
    now: datetime | None = None,
) -> None:
    timestamp = now or datetime.now(timezone.utc)
    phase_value = str(phase or "progress")[:120]
    with session_factory() as db:
        result = db.execute(
            update(WorkerHeartbeat)
            .where(WorkerHeartbeat.worker_name == worker_name)
            .where(WorkerHeartbeat.correlation_id == correlation_id)
            .where(WorkerHeartbeat.status == "running")
            .values(
                status="running",
                last_progress_at=timestamp,
                last_progress_phase=phase_value,
            )
        )
        db.commit()
    if result.rowcount:
        log_trace(LOGGER, "worker_cycle_progress", worker=worker_name, phase=phase_value)


def record_cycle_result(
    worker_name: str,
    *,
    correlation_id: str,
    error: BaseException | None = None,
    session_factory=SessionLocal,
    now: datetime | None = None,
) -> None:
    timestamp = now or datetime.now(timezone.utc)
    values = {
        "last_progress_at": timestamp,
        "last_progress_phase": "cycle_failed" if error is not None else "cycle_succeeded",
        "status": "failed" if error is not None else "success",
        "last_error_class": error.__class__.__name__[:80] if error is not None else None,
    }
    if error is None:
        values["last_success_at"] = timestamp
    else:
        values["last_failure_at"] = timestamp
    with session_factory() as db:
        result = db.execute(
            update(WorkerHeartbeat)
            .where(WorkerHeartbeat.worker_name == worker_name)
            .where(WorkerHeartbeat.correlation_id == correlation_id)
            .where(WorkerHeartbeat.status == "running")
            .values(**values)
        )
        db.commit()
    if result.rowcount:
        log_trace(
            LOGGER,
            "worker_cycle_finished",
            worker=worker_name,
            result="failed" if error is not None else "success",
        )


@contextmanager
def observed_worker_cycle(
    worker_name: str,
    interval_seconds: int,
    *,
    grace_seconds: int = DEFAULT_GRACE_SECONDS,
    session_factory=SessionLocal,
):
    token = bind_correlation_id()
    try:
        correlation_id = record_cycle_start(
            worker_name,
            interval_seconds,
            grace_seconds=grace_seconds,
            session_factory=session_factory,
        )
        try:
            yield correlation_id
        except BaseException as exc:
            record_cycle_result(
                worker_name,
                correlation_id=correlation_id,
                error=exc,
                session_factory=session_factory,
            )
            raise
        else:
            record_cycle_result(
                worker_name,
                correlation_id=correlation_id,
                session_factory=session_factory,
            )
    finally:
        reset_correlation_id(token)


def build_worker_readiness(
    db: Session,
    *,
    required_workers=(),
    now: datetime | None = None,
) -> dict:
    timestamp = now or datetime.now(timezone.utc)
    required = tuple(sorted(set(required_workers or ())))
    selected_names = tuple(sorted(set(KNOWN_WORKERS).union(required)))
    rows = db.execute(
        select(WorkerHeartbeat)
        .where(WorkerHeartbeat.worker_name.in_(selected_names))
        .order_by(WorkerHeartbeat.worker_name)
        .limit(len(selected_names))
    ).scalars().all()
    by_name = {row.worker_name: row for row in rows}
    missing = [name for name in required if name not in by_name]
    workers = []
    unhealthy = []
    for row in rows:
        progress_at = _aware_utc(row.last_progress_at or row.last_cycle_started_at)
        age_seconds = max(0, int((timestamp - progress_at).total_seconds()))
        unhealthy_after = 2 * int(row.interval_seconds) + int(row.grace_seconds)
        state = "stale" if age_seconds > unhealthy_after else row.status
        if row.worker_name in required and state in {"stale", "failed"}:
            unhealthy.append(row.worker_name)
        workers.append({
            "worker_name": row.worker_name,
            "status": state,
            "age_seconds": age_seconds,
            "unhealthy_after_seconds": unhealthy_after,
            "last_progress_phase": row.last_progress_phase or "",
            "last_success_at": row.last_success_at.isoformat() if row.last_success_at else "",
        })
    return {
        "status": "unhealthy" if missing or unhealthy else "ok",
        "required": list(required),
        "missing": missing,
        "unhealthy": unhealthy,
        "workers": workers,
    }


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
