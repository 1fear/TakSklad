"""One-time, server-owned provisioning for the Windows desktop client.

Plain setup codes and service credentials are returned once and are never
persisted.  The database stores only domain-separated HMAC digests.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError

from .audit_identity import AuditActor, set_audit_actor
from .auth_identities import SERVICE_PRINCIPAL_SCOPE_MATRIX, issue_service_token
from .models import (
    AuditLog,
    DesktopPairing,
    DesktopPairingMaintenance,
    DesktopPairingRateLimit,
    ServicePrincipal,
    ServicePrincipalToken,
)


SETUP_CODE_BYTES = 32
SETUP_CODE_TTL_SECONDS = 300
UNACKED_TOKEN_TTL_SECONDS = 300
ACKED_TOKEN_TTL_SECONDS = 31_536_000
SWEEPER_INTERVAL_SECONDS = 30
STALE_CLEANUP_GRACE_SECONDS = 60
GLOBAL_PENDING_CAP = 100
CREATOR_PENDING_CAP = 5
CREATE_ADMIN_RATE_LIMIT = 5
CREATE_ADMIN_RATE_WINDOW_SECONDS = 900
CREATE_IP_RATE_LIMIT = 20
CREATE_IP_RATE_WINDOW_SECONDS = 3600
PUBLIC_BOOTSTRAP_RATE_LIMIT = 20
PUBLIC_BOOTSTRAP_RATE_WINDOW_SECONDS = 3600
REDEEM_RATE_LIMIT = 10
REDEEM_RATE_WINDOW_SECONDS = 60
RATE_LOCK_SECONDS = 900
_SETUP_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{43,64}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z._+-]{0,40}$")
_BOOTSTRAP_VERSION_RE = re.compile(r"^[0-9A-Za-z._+-]{1,40}$")
_CREATE_CAP_ADVISORY_LOCK = 7_431_905_021


class DevicePairingError(Exception):
    """Safe operational error; ``detail`` contains no credential material."""

    def __init__(self, detail: str, *, status_code: int = 400, retry_after: int = 0):
        self.detail = detail
        self.status_code = int(status_code)
        self.retry_after = max(0, int(retry_after))
        super().__init__(detail)


@dataclass(frozen=True)
class CreatedPairing:
    pairing_id: uuid.UUID
    setup_code: str
    expires_at: datetime


@dataclass(frozen=True)
class RedeemedPairing:
    pairing_id: uuid.UUID
    credential: str
    principal_identifier: str
    ack_deadline: datetime


def create_desktop_pairing(
    db,
    *,
    pepper: str,
    created_by_user_id: str | uuid.UUID,
    device_label: str = "",
    rate_key: str,
    now: datetime | None = None,
    code_factory=None,
) -> CreatedPairing:
    now = _utc(now)
    creator_id = _required_uuid(created_by_user_id, "Authenticated administrator is required")
    _require_pepper(pepper)
    label = _normalize_label(device_label)
    _consume_budget(
        db,
        pepper=pepper,
        bucket=f"create-admin:{creator_id}",
        limit=CREATE_ADMIN_RATE_LIMIT,
        window_seconds=CREATE_ADMIN_RATE_WINDOW_SECONDS,
        now=now,
    )
    _consume_budget(
        db,
        pepper=pepper,
        bucket=f"create-ip:{rate_key}",
        limit=CREATE_IP_RATE_LIMIT,
        window_seconds=CREATE_IP_RATE_WINDOW_SECONDS,
        now=now,
    )
    _serialize_pending_cap_check(db)
    global_pending = db.execute(
        select(func.count(DesktopPairing.id))
        .where(DesktopPairing.status == "pending")
        .where(DesktopPairing.expires_at > now)
    ).scalar_one()
    creator_pending = db.execute(
        select(func.count(DesktopPairing.id))
        .where(DesktopPairing.status == "pending")
        .where(DesktopPairing.expires_at > now)
        .where(DesktopPairing.created_by_user_id == creator_id)
    ).scalar_one()
    if int(global_pending or 0) >= GLOBAL_PENDING_CAP:
        raise DevicePairingError("Desktop pairing capacity is temporarily unavailable", status_code=503)
    if int(creator_pending or 0) >= CREATOR_PENDING_CAP:
        raise DevicePairingError("Too many active desktop pairing codes", status_code=429, retry_after=60)

    factory = code_factory or (lambda: secrets.token_urlsafe(SETUP_CODE_BYTES))
    setup_code = str(factory() or "")
    if not _SETUP_CODE_RE.fullmatch(setup_code):
        raise DevicePairingError("Desktop pairing code generation failed", status_code=503)
    row = DesktopPairing(
        id=uuid.uuid4(),
        setup_code_digest=_digest(pepper, "setup-code", setup_code),
        status="pending",
        device_label=label or None,
        created_by_user_id=creator_id,
        expires_at=now + timedelta(seconds=SETUP_CODE_TTL_SECONDS),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.add(AuditLog(
        action="desktop_pairing_created",
        entity_type="desktop_pairing",
        entity_id=str(row.id),
        payload={
            "expires_at": row.expires_at.isoformat(),
            "device_label_present": bool(label),
        },
    ))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise DevicePairingError("Desktop pairing code generation failed", status_code=503) from exc
    return CreatedPairing(row.id, setup_code, _utc(row.expires_at))


def bootstrap_desktop(
    db,
    *,
    pepper: str,
    desktop_version: str,
    rate_key: str,
    now: datetime | None = None,
) -> RedeemedPairing:
    """Issue an anonymous desktop credential through the existing ACK lifecycle."""

    now = _utc(now)
    _require_pepper(pepper)
    version = str(desktop_version or "").strip()
    if not _BOOTSTRAP_VERSION_RE.fullmatch(version):
        raise DevicePairingError("Invalid desktop version", status_code=422)
    _consume_budget(
        db,
        pepper=pepper,
        bucket=f"public-bootstrap-ip:{rate_key}",
        limit=PUBLIC_BOOTSTRAP_RATE_LIMIT,
        window_seconds=PUBLIC_BOOTSTRAP_RATE_WINDOW_SECONDS,
        now=now,
    )
    _serialize_pending_cap_check(db)
    active_bootstraps = db.execute(
        select(func.count(DesktopPairing.id))
        .where(DesktopPairing.status == "redeemed_unacked")
        .where(DesktopPairing.ack_deadline > now)
    ).scalar_one()
    if int(active_bootstraps or 0) >= GLOBAL_PENDING_CAP:
        db.rollback()
        raise DevicePairingError(
            "Desktop bootstrap capacity is temporarily unavailable",
            status_code=503,
        )

    pairing_id = uuid.uuid4()
    ack_deadline = now + timedelta(seconds=UNACKED_TOKEN_TTL_SECONDS)
    principal = ServicePrincipal(
        id=uuid.uuid4(),
        identifier=f"desktop.bootstrap.{pairing_id.hex[:24]}",
        kind="desktop",
        scopes=sorted(SERVICE_PRINCIPAL_SCOPE_MATRIX["desktop"]),
        is_active=True,
        expires_at=None,
        created_at=now,
        updated_at=now,
    )
    db.add(principal)
    db.flush()
    issued = issue_service_token(db, principal, expires_at=ack_deadline, now=now)
    pairing = DesktopPairing(
        id=pairing_id,
        setup_code_digest=_digest(
            pepper,
            "setup-code",
            secrets.token_urlsafe(SETUP_CODE_BYTES),
        ),
        status="redeemed_unacked",
        desktop_version=version,
        created_by_user_id=None,
        principal_id=principal.id,
        token_id=issued.identifier,
        expires_at=ack_deadline,
        ack_deadline=ack_deadline,
        redeemed_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(pairing)
    set_audit_actor(db, AuditActor(subject="public:desktop-bootstrap"))
    db.add(AuditLog(
        action="desktop_public_bootstrap_issued",
        entity_type="desktop_pairing",
        entity_id=str(pairing.id),
        payload={
            "principal_id": str(principal.id),
            "ack_deadline": ack_deadline.isoformat(),
            "desktop_version_present": True,
            "source_digest": _digest(pepper, "public-bootstrap-source", rate_key),
        },
    ))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise DevicePairingError(
            "Desktop bootstrap is temporarily unavailable",
            status_code=503,
        ) from exc
    return RedeemedPairing(pairing.id, issued.token, principal.identifier, ack_deadline)


def redeem_desktop_pairing(
    db,
    *,
    pepper: str,
    setup_code: str,
    desktop_version: str = "",
    rate_key: str,
    now: datetime | None = None,
) -> RedeemedPairing:
    now = _utc(now)
    _require_pepper(pepper)
    code = str(setup_code or "").strip()
    version = str(desktop_version or "").strip()
    if not _SETUP_CODE_RE.fullmatch(code) or not _VERSION_RE.fullmatch(version):
        # Invalid syntax must consume the same persistent abuse budget as an
        # unknown digest, while returning one generic response.
        _consume_redeem_budget(db, pepper, rate_key, now)
        raise DevicePairingError("Invalid or expired desktop setup code", status_code=401)
    _consume_redeem_budget(db, pepper, rate_key, now)
    digest = _digest(pepper, "setup-code", code)
    pairing = db.execute(
        select(DesktopPairing)
        .where(DesktopPairing.setup_code_digest == digest)
        .with_for_update()
    ).scalar_one_or_none()
    if pairing is None or pairing.status != "pending" or _utc(pairing.expires_at) <= now:
        if pairing is not None and pairing.status == "pending":
            pairing.status = "expired"
            pairing.updated_at = now
            db.commit()
        else:
            db.rollback()
        raise DevicePairingError("Invalid or expired desktop setup code", status_code=401)

    principal = ServicePrincipal(
        id=uuid.uuid4(),
        identifier=f"desktop.paired.{pairing.id.hex[:24]}",
        kind="desktop",
        scopes=sorted(SERVICE_PRINCIPAL_SCOPE_MATRIX["desktop"]),
        is_active=True,
        expires_at=None,
        created_at=now,
        updated_at=now,
    )
    db.add(principal)
    db.flush()
    ack_deadline = now + timedelta(seconds=UNACKED_TOKEN_TTL_SECONDS)
    issued = issue_service_token(db, principal, expires_at=ack_deadline, now=now)
    pairing.status = "redeemed_unacked"
    pairing.desktop_version = version or None
    pairing.principal_id = principal.id
    pairing.token_id = issued.identifier
    pairing.ack_deadline = ack_deadline
    pairing.redeemed_at = now
    pairing.updated_at = now
    set_audit_actor(db, AuditActor(subject="device-pairing:redeem"))
    db.add(AuditLog(
        action="desktop_pairing_redeemed",
        entity_type="desktop_pairing",
        entity_id=str(pairing.id),
        payload={
            "principal_id": str(principal.id),
            "token_id": str(issued.identifier),
            "ack_deadline": ack_deadline.isoformat(),
            "desktop_version_present": bool(version),
        },
    ))
    db.commit()
    return RedeemedPairing(pairing.id, issued.token, principal.identifier, ack_deadline)


def acknowledge_desktop_pairing(
    db,
    pairing_id: str | uuid.UUID,
    *,
    auth_principal_id: str | uuid.UUID,
    auth_token_id: str | uuid.UUID,
    now: datetime | None = None,
) -> dict:
    now = _utc(now)
    identifier = _required_uuid(pairing_id, "Desktop pairing not found")
    principal_id = _required_uuid(auth_principal_id, "Desktop pairing credential denied")
    token_id = _required_uuid(auth_token_id, "Desktop pairing credential denied")
    pairing = db.execute(
        select(DesktopPairing).where(DesktopPairing.id == identifier).with_for_update()
    ).scalar_one_or_none()
    if pairing is None:
        raise DevicePairingError("Desktop pairing not found", status_code=404)
    if pairing.principal_id != principal_id or pairing.token_id != token_id:
        raise DevicePairingError("Desktop pairing credential denied", status_code=403)
    token = db.execute(
        select(ServicePrincipalToken)
        .where(ServicePrincipalToken.id == token_id)
        .with_for_update()
    ).scalar_one_or_none()
    principal = db.execute(
        select(ServicePrincipal)
        .where(ServicePrincipal.id == principal_id)
        .with_for_update()
    ).scalar_one_or_none()
    if token is None or principal is None or not principal.is_active or token.revoked_at is not None:
        raise DevicePairingError("Desktop pairing credential denied", status_code=403)
    if pairing.status == "acked":
        return {
            "pairing_id": str(pairing.id),
            "status": "acked",
            "credential_expires_at": _utc(token.expires_at),
        }
    if (
        pairing.status != "redeemed_unacked"
        or pairing.ack_deadline is None
        or _utc(pairing.ack_deadline) <= now
        or _utc(token.expires_at) <= now
    ):
        raise DevicePairingError("Desktop pairing acknowledgement expired", status_code=409)
    token.expires_at = now + timedelta(seconds=ACKED_TOKEN_TTL_SECONDS)
    pairing.status = "acked"
    pairing.acked_at = now
    pairing.updated_at = now
    db.add(AuditLog(
        action="desktop_pairing_acked",
        entity_type="desktop_pairing",
        entity_id=str(pairing.id),
        payload={
            "principal_id": str(principal.id),
            "token_id": str(token.id),
            "credential_expires_at": token.expires_at.isoformat(),
        },
    ))
    db.commit()
    return {
        "pairing_id": str(pairing.id),
        "status": "acked",
        "credential_expires_at": _utc(token.expires_at),
    }


def cleanup_expired_pairings(db, *, now: datetime | None = None, commit: bool = True) -> dict:
    now = _utc(now)
    rows = list(db.execute(
        select(DesktopPairing)
        .where(or_(
            (DesktopPairing.status == "pending") & (DesktopPairing.expires_at <= now),
            (DesktopPairing.status == "redeemed_unacked") & (DesktopPairing.ack_deadline <= now),
        ))
        .order_by(DesktopPairing.created_at, DesktopPairing.id)
        .limit(GLOBAL_PENDING_CAP)
        .with_for_update(skip_locked=True)
    ).scalars())
    expired = 0
    revoked = 0
    if rows:
        set_audit_actor(db, AuditActor(subject="system:desktop-pairing-sweeper"))
    for pairing in rows:
        if pairing.status == "pending":
            pairing.status = "expired"
            expired += 1
            action = "desktop_pairing_expired"
        else:
            token = db.get(ServicePrincipalToken, pairing.token_id) if pairing.token_id else None
            principal = db.get(ServicePrincipal, pairing.principal_id) if pairing.principal_id else None
            if token is not None and token.revoked_at is None:
                token.revoked_at = now
            if principal is not None:
                principal.is_active = False
                principal.updated_at = now
            pairing.status = "revoked"
            pairing.revoked_at = now
            revoked += 1
            action = "desktop_pairing_orphan_revoked"
        pairing.updated_at = now
        db.add(AuditLog(
            action=action,
            entity_type="desktop_pairing",
            entity_id=str(pairing.id),
            payload={
                "principal_id": str(pairing.principal_id or ""),
                "token_id": str(pairing.token_id or ""),
            },
        ))
    if commit:
        db.commit()
    elif rows:
        db.flush()
    return {"expired": expired, "revoked": revoked}


def build_device_pairing_readiness(
    db,
    *,
    now: datetime | None = None,
    require_sweeper: bool = False,
) -> dict:
    now = _utc(now)
    stale_cutoff = now - timedelta(seconds=STALE_CLEANUP_GRACE_SECONDS)
    overdue_unacked = db.execute(
        select(func.count(DesktopPairing.id))
        .where(DesktopPairing.status == "redeemed_unacked")
        .where(DesktopPairing.ack_deadline <= now)
    ).scalar_one()
    stale_cleanup = db.execute(
        select(func.count(DesktopPairing.id)).where(or_(
            (DesktopPairing.status == "pending") & (DesktopPairing.expires_at <= stale_cutoff),
            (DesktopPairing.status == "redeemed_unacked") & (DesktopPairing.ack_deadline <= stale_cutoff),
        ))
    ).scalar_one()
    maintenance = db.get(DesktopPairingMaintenance, "sweeper")
    last_success = (
        _utc(maintenance.last_succeeded_at)
        if maintenance is not None and maintenance.last_succeeded_at is not None
        else None
    )
    last_error = (
        _utc(maintenance.last_error_at)
        if maintenance is not None and maintenance.last_error_at is not None
        else None
    )
    heartbeat_stale = bool(
        require_sweeper
        and (
            last_success is None
            or now - last_success > timedelta(seconds=60)
            or (last_error is not None and last_error > last_success)
        )
    )
    unhealthy = bool(int(overdue_unacked or 0) or int(stale_cleanup or 0) or heartbeat_stale)
    return {
        "status": "unhealthy" if unhealthy else "ok",
        "overdue_unacked_count": int(overdue_unacked or 0),
        "stale_cleanup_count": int(stale_cleanup or 0),
        "sweeper_heartbeat_stale": heartbeat_stale,
        "sweeper_last_succeeded_at": last_success.isoformat() if last_success is not None else "",
    }


def run_device_pairing_sweeper_loop(session_factory, *, stop_event: threading.Event, interval_seconds: int = SWEEPER_INTERVAL_SECONDS):
    interval = max(1, min(60, int(interval_seconds)))
    while not stop_event.is_set():
        now = datetime.now(timezone.utc)
        db = session_factory()
        try:
            maintenance = _maintenance_row(db, now)
            maintenance.last_started_at = now
            maintenance.updated_at = now
            db.flush()
            cleanup_expired_pairings(db, now=now, commit=False)
            maintenance.last_succeeded_at = now
            maintenance.updated_at = now
            db.commit()
        except Exception:
            db.rollback()
            try:
                maintenance = _maintenance_row(db, now)
                maintenance.last_error_at = now
                maintenance.updated_at = now
                db.commit()
            except Exception:
                db.rollback()
        finally:
            db.close()
        if stop_event.wait(interval):
            break


def _maintenance_row(db, now: datetime) -> DesktopPairingMaintenance:
    row = db.execute(
        select(DesktopPairingMaintenance)
        .where(DesktopPairingMaintenance.name == "sweeper")
        .with_for_update()
    ).scalar_one_or_none()
    if row is not None:
        return row
    try:
        with db.begin_nested():
            row = DesktopPairingMaintenance(name="sweeper", updated_at=now)
            db.add(row)
            db.flush()
    except IntegrityError:
        row = db.execute(
            select(DesktopPairingMaintenance)
            .where(DesktopPairingMaintenance.name == "sweeper")
            .with_for_update()
        ).scalar_one()
    return row


def _consume_redeem_budget(db, pepper: str, rate_key: str, now: datetime) -> None:
    _consume_budget(
        db,
        pepper=pepper,
        bucket=f"redeem:{rate_key}",
        limit=REDEEM_RATE_LIMIT,
        window_seconds=REDEEM_RATE_WINDOW_SECONDS,
        now=now,
    )


def _consume_budget(db, *, pepper: str, bucket: str, limit: int, window_seconds: int, now: datetime) -> None:
    try:
        _enforce_rate_limit(
            db,
            pepper=pepper,
            bucket=bucket,
            limit=limit,
            window_seconds=window_seconds,
            now=now,
        )
    except DevicePairingError:
        # The rejection itself is security state. Commit it before propagating
        # so a new process/session cannot bypass the lock.
        db.commit()
        raise
    db.commit()


def _enforce_rate_limit(
    db,
    *,
    pepper: str,
    bucket: str,
    limit: int,
    window_seconds: int,
    now: datetime,
) -> None:
    digest = _digest(pepper, "rate-limit", bucket)
    row = db.execute(
        select(DesktopPairingRateLimit)
        .where(DesktopPairingRateLimit.bucket_digest == digest)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        try:
            with db.begin_nested():
                row = DesktopPairingRateLimit(
                    id=uuid.uuid4(),
                    bucket_digest=digest,
                    attempts=0,
                    window_started_at=now,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                db.flush()
        except IntegrityError:
            row = db.execute(
                select(DesktopPairingRateLimit)
                .where(DesktopPairingRateLimit.bucket_digest == digest)
                .with_for_update()
            ).scalar_one()
    locked_until = _utc(row.locked_until) if row.locked_until is not None else None
    if locked_until is not None and locked_until > now:
        raise DevicePairingError(
            "Too many desktop pairing attempts",
            status_code=429,
            retry_after=max(1, int((locked_until - now).total_seconds())),
        )
    window_started = _utc(row.window_started_at)
    if now - window_started >= timedelta(seconds=max(1, int(window_seconds))):
        row.window_started_at = now
        row.attempts = 0
        row.locked_until = None
    row.attempts = int(row.attempts or 0) + 1
    row.updated_at = now
    if row.attempts > int(limit):
        row.locked_until = now + timedelta(seconds=RATE_LOCK_SECONDS)
        db.flush()
        raise DevicePairingError(
            "Too many desktop pairing attempts",
            status_code=429,
            retry_after=RATE_LOCK_SECONDS,
        )
    db.flush()


def _serialize_pending_cap_check(db) -> None:
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": _CREATE_CAP_ADVISORY_LOCK})


def _digest(pepper: str, purpose: str, value: str) -> str:
    material = f"taksklad.desktop-pairing.v1\x1f{purpose}\x1f{value}".encode("utf-8")
    return hmac.new(pepper.encode("utf-8"), material, hashlib.sha256).hexdigest()


def _require_pepper(pepper: str) -> None:
    if len(str(pepper or "").encode("utf-8")) < 32:
        raise DevicePairingError("Desktop pairing is not configured", status_code=503)


def _normalize_label(value: str) -> str:
    label = " ".join(str(value or "").strip().split())
    if len(label) > 80 or any(ord(char) < 32 for char in label):
        raise DevicePairingError("Invalid desktop device label", status_code=422)
    return label


def _required_uuid(value, detail: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value or ""))
    except (TypeError, ValueError) as exc:
        raise DevicePairingError(detail, status_code=403) from exc


def _utc(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
