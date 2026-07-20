"""Ephemeral, least-privilege identity for database-backed worker processes."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from .auth_identities import (
    SERVICE_PRINCIPAL_SCOPE_MATRIX,
    issue_service_token,
    rotate_service_token,
)
from .models import AuditLog, ServicePrincipal


TELEGRAM_WORKER_IDENTIFIER = "worker.telegram"
TELEGRAM_WORKER_TOKEN_TTL_SECONDS = 10 * 365 * 24 * 60 * 60
TELEGRAM_WORKER_ROTATION_OVERLAP_SECONDS = 60
_LOCK_KEY = int.from_bytes(
    hashlib.sha256(TELEGRAM_WORKER_IDENTIFIER.encode("ascii")).digest()[:8],
    "big",
) & ((1 << 63) - 1)


class WorkerRuntimeIdentityError(RuntimeError):
    """Raised without credential material when worker identity bootstrap is unsafe."""


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def issue_telegram_worker_runtime_token(session_factory, *, now: datetime | None = None) -> str:
    """Issue a process-local token; only its digest is persisted.

    The Telegram worker already requires direct PostgreSQL access for its queue.
    It uses that existing trust boundary to mint a narrowly scoped bearer token
    on startup instead of depending on the time-bounded legacy shared token.
    """

    now = _utc(now)
    scopes = tuple(sorted(SERVICE_PRINCIPAL_SCOPE_MATRIX["worker"]))
    with session_factory() as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": _LOCK_KEY})
        principal = db.execute(
            select(ServicePrincipal)
            .where(ServicePrincipal.identifier == TELEGRAM_WORKER_IDENTIFIER)
            .with_for_update()
        ).scalar_one_or_none()
        if principal is None:
            principal = ServicePrincipal(
                identifier=TELEGRAM_WORKER_IDENTIFIER,
                kind="worker",
                scopes=list(scopes),
                is_active=True,
            )
            db.add(principal)
            db.flush()
            issued = issue_service_token(
                db,
                principal,
                expires_at=now + timedelta(seconds=TELEGRAM_WORKER_TOKEN_TTL_SECONDS),
                now=now,
            )
            action = "worker_runtime_identity_provisioned"
        else:
            if (
                not principal.is_active
                or principal.kind != "worker"
                or tuple(sorted(principal.scopes or ())) != scopes
                or (principal.expires_at is not None and _utc(principal.expires_at) <= now)
            ):
                raise WorkerRuntimeIdentityError("worker principal policy is unavailable")
            issued = rotate_service_token(
                db,
                principal,
                expires_at=now + timedelta(seconds=TELEGRAM_WORKER_TOKEN_TTL_SECONDS),
                overlap_seconds=TELEGRAM_WORKER_ROTATION_OVERLAP_SECONDS,
                max_overlap_seconds=TELEGRAM_WORKER_ROTATION_OVERLAP_SECONDS,
                now=now,
            )
            action = "worker_runtime_identity_rotated"
        db.add(AuditLog(
            action=action,
            entity_type="service_principal",
            entity_id=str(principal.id),
            payload={
                "identifier": TELEGRAM_WORKER_IDENTIFIER,
                "kind": "worker",
                "scopes": list(scopes),
                "token_id": str(issued.identifier),
                "ttl_seconds": TELEGRAM_WORKER_TOKEN_TTL_SECONDS,
                "overlap_seconds": (
                    TELEGRAM_WORKER_ROTATION_OVERLAP_SECONDS
                    if action == "worker_runtime_identity_rotated"
                    else 0
                ),
                "secret_output": False,
            },
        ))
        db.commit()
        return issued.token
