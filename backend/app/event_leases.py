import os
from datetime import timedelta
from functools import lru_cache

from sqlalchemy import and_, bindparam, func, or_, select, true, update
from sqlalchemy.orm import Session

from .models import AuditLog, PendingEvent


EVENT_LEASES_ENABLED_ENV = "TAKSKLAD_EVENT_LEASES_ENABLED"
DEFAULT_EVENT_LEASE_DURATION = timedelta(minutes=30)
CLAIMABLE_EVENT_STATUSES = ("pending", "failed")
TERMINAL_EVENT_STATUSES = ("completed", "blocked", "dead", "cancelled")
DEPLOY_RECOVERABLE_EVENT_TYPES = (
    "google_sheets_export",
    "telegram_excel_import",
    "telegram_notification",
    "skladbot_request_create",
    "skladbot_return_request_create",
)


class LeaseOwnershipError(RuntimeError):
    pass


def event_leases_enabled(environ=None):
    environ = os.environ if environ is None else environ
    return str(environ.get(EVENT_LEASES_ENABLED_ENV) or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def claim_event_leases(
    db: Session,
    *,
    event_types,
    owner,
    limit,
    now=None,
    lease_duration=DEFAULT_EVENT_LEASE_DURATION,
):
    event_types = tuple(
        str(value or "").strip()
        for value in event_types or ()
        if str(value or "").strip()
    )
    owner = str(owner or "").strip()
    limit = max(1, min(int(limit or 1), 1000))
    if not event_types:
        return []
    if not owner:
        raise ValueError("lease owner is required")
    is_postgresql = db.bind is not None and db.bind.dialect.name == "postgresql"
    if now is None and is_postgresql:
        now_value = func.now()
        expires_at = func.now() + lease_duration
    else:
        now_value = now or db.execute(select(func.now())).scalar_one()
        expires_at = now_value + lease_duration
    try:
        if is_postgresql:
            statement = cached_postgres_claim_statement(
                event_type_count=len(event_types),
                limit=limit,
                lease_duration_seconds=int(lease_duration.total_seconds()),
            )
            events = db.execute(
                statement.execution_options(synchronize_session=False),
                {
                    **{f"lease_event_type_{index}": value for index, value in enumerate(event_types)},
                    "lease_owner": owner,
                },
            ).scalars().all()
        else:
            eligible = or_(
                and_(
                    PendingEvent.status.in_(CLAIMABLE_EVENT_STATUSES),
                    PendingEvent.available_at <= now_value,
                ),
                and_(
                    PendingEvent.status == "processing",
                    PendingEvent.lease_expires_at.is_not(None),
                    PendingEvent.lease_expires_at <= now_value,
                ),
            )
            events = db.execute(
                select(PendingEvent)
                .where(PendingEvent.event_type.in_(event_types))
                .where(eligible)
                .order_by(PendingEvent.available_at, PendingEvent.created_at, PendingEvent.id)
                .limit(limit)
            ).scalars().all()
            for event in events:
                event.status = "processing"
                event.attempts = int(event.attempts or 0) + 1
                event.lease_owner = owner
                event.lease_expires_at = expires_at
                event.completed_at = None
                event.updated_at = now_value
        db.commit()
    except Exception:
        db.rollback()
        raise
    # Candidate selection already applies the fairness order before locking.
    # Consumers do not require a second ordering pass over the claimed batch.
    return events


@lru_cache(maxsize=64)
def cached_postgres_claim_statement(*, event_type_count, limit, lease_duration_seconds):
    event_types = tuple(
        bindparam(f"lease_event_type_{index}")
        for index in range(max(1, int(event_type_count)))
    )
    duration = timedelta(seconds=max(1, int(lease_duration_seconds)))
    return build_postgres_claim_statement(
        event_types=event_types,
        owner=bindparam("lease_owner"),
        limit=max(1, min(int(limit), 1000)),
        now=func.now(),
        expires_at=func.now() + duration,
    )


def build_postgres_claim_statement(*, event_types, owner, limit, now, expires_at, eligible=None):
    eligible = eligible if eligible is not None else or_(
        and_(
            PendingEvent.status.in_(CLAIMABLE_EVENT_STATUSES),
            PendingEvent.available_at <= now,
        ),
        and_(
            PendingEvent.status == "processing",
            PendingEvent.lease_expires_at.is_not(None),
            PendingEvent.lease_expires_at <= now,
        ),
    )
    # A crash before WAL flush merely leaves the event claimable again. Apply
    # the transaction-local setting inside the claim statement so the
    # at-least-once lease does not pay either an fsync or another round trip.
    transaction_settings = select(
        func.set_config("synchronous_commit", "off", True).label("synchronous_commit")
    ).cte("lease_transaction_settings")
    candidates = (
        select(PendingEvent.id)
        .select_from(PendingEvent)
        .join(transaction_settings, true())
        .where(PendingEvent.event_type.in_(tuple(event_types)))
        .where(eligible)
        .order_by(PendingEvent.available_at, PendingEvent.created_at, PendingEvent.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .cte("lease_candidates")
    )
    return (
        update(PendingEvent)
        .where(PendingEvent.id == candidates.c.id)
        .values(
            status="processing",
            attempts=PendingEvent.attempts + 1,
            lease_owner=owner,
            lease_expires_at=expires_at,
            completed_at=None,
            updated_at=now,
        )
        .returning(PendingEvent)
    )


def finalize_event_leases(
    db: Session,
    *,
    event_ids,
    owner,
    status,
    last_error="",
    payload=None,
    available_at=None,
    completed_at=None,
    now=None,
):
    event_ids = tuple(event_ids or ())
    owner = str(owner or "").strip()
    status = str(status or "").strip()
    if not event_ids:
        return 0
    if not owner:
        raise ValueError("lease owner is required")
    if not status:
        raise ValueError("final status is required")
    now = now or db.execute(select(func.now())).scalar_one()
    terminal = status in TERMINAL_EVENT_STATUSES
    values = {
        "status": status,
        "last_error": str(last_error or ""),
        "lease_owner": None,
        "lease_expires_at": None,
        "completed_at": completed_at or (now if terminal else None),
        "updated_at": now,
    }
    if not terminal:
        values["available_at"] = available_at or now
    if payload is not None:
        values["payload"] = payload
    try:
        with db.no_autoflush:
            result = db.execute(
                update(PendingEvent)
                .where(PendingEvent.id.in_(event_ids))
                .where(PendingEvent.status == "processing")
                .where(PendingEvent.lease_owner == owner)
                .where(PendingEvent.lease_expires_at > now)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
        if result.rowcount != len(event_ids):
            db.rollback()
            raise LeaseOwnershipError(
                f"lease finalize rejected: expected={len(event_ids)} matched={result.rowcount}"
            )
        event_id_set = set(event_ids)
        for instance in tuple(db.identity_map.values()):
            if isinstance(instance, PendingEvent) and instance.id in event_id_set:
                db.expire(instance)
        db.commit()
    except Exception:
        if db.in_transaction():
            db.rollback()
        raise
    return int(result.rowcount or 0)


def recover_expired_event_leases(db: Session, *, event_types, now=None):
    event_types = tuple(event_types or ())
    if not event_types:
        return 0
    now = now or db.execute(select(func.now())).scalar_one()
    try:
        result = db.execute(
            update(PendingEvent)
            .where(PendingEvent.event_type.in_(event_types))
            .where(PendingEvent.status == "processing")
            .where(PendingEvent.lease_expires_at.is_not(None))
            .where(PendingEvent.lease_expires_at <= now)
            .values(
                status="pending",
                lease_owner=None,
                lease_expires_at=None,
                available_at=now,
                last_error="expired event lease recovered",
                updated_at=now,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return int(result.rowcount or 0)


def recover_inflight_event_leases_after_worker_stop(
    db: Session,
    *,
    event_types=DEPLOY_RECOVERABLE_EVENT_TYPES,
    now=None,
):
    """Requeue leases whose worker processes were intentionally stopped for deploy."""
    event_types = tuple(str(value or "").strip() for value in event_types or () if str(value or "").strip())
    if not event_types:
        return 0
    now = now or db.execute(select(func.now())).scalar_one()
    reason = "in-flight event lease recovered after deploy worker stop"
    try:
        grouped = db.execute(
            select(PendingEvent.event_type, func.count(PendingEvent.id))
            .where(PendingEvent.event_type.in_(event_types))
            .where(PendingEvent.status == "processing")
            .where(PendingEvent.lease_owner.is_not(None))
            .group_by(PendingEvent.event_type)
            .order_by(PendingEvent.event_type)
        ).all()
        result = db.execute(
            update(PendingEvent)
            .where(PendingEvent.event_type.in_(event_types))
            .where(PendingEvent.status == "processing")
            .where(PendingEvent.lease_owner.is_not(None))
            .values(
                status="pending",
                lease_owner=None,
                lease_expires_at=None,
                available_at=now,
                completed_at=None,
                last_error=reason,
                updated_at=now,
            )
        )
        recovered = int(result.rowcount or 0)
        if recovered:
            db.add(
                AuditLog(
                    action="deploy_worker_lease_recovery",
                    entity_type="pending_event",
                    entity_id="deploy",
                    payload={
                        "reason": reason,
                        "recovered": recovered,
                        "event_type_counts": {
                            str(event_type): int(count or 0)
                            for event_type, count in grouped
                        },
                    },
                )
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return recovered
