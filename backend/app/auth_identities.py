"""Revocable database identities for users and machine clients.

The module intentionally keeps bearer/session plaintext out of ORM objects.  A
token is returned once to the caller that provisions it; only its SHA-256
digest and public UUID are persisted.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from sqlalchemy import select

from .models import AuthSession, ServicePrincipal, ServicePrincipalToken, User


TOKEN_PREFIX = "tks"
TOKEN_SECRET_BYTES = 32
DEFAULT_LAST_USED_TOUCH_SECONDS = 60


# These are caller capabilities, not application roles.  In particular, none
# of the machine principals receives the broad ``admin:write`` permission.
SERVICE_PRINCIPAL_SCOPE_MATRIX = {
    "desktop": frozenset({
        "imports:create",
        "imports:preview",
        "kiz:read",
        "orders:complete",
        "orders:read",
        "returns:read",
        "returns:write",
        "scans:create",
        "scans:undo",
        "sync:run",
    }),
    "worker": frozenset({
        "diagnostics:read",
        "imports:create",
        "imports:read",
        "logistics:read",
        "orders:delete_active",
        "orders:read",
        "reports:read",
    }),
    "acceptance": frozenset({
        "imports:create",
        "logistics:read",
        "orders:complete",
        "orders:read",
        "reports:read",
        "scans:create",
    }),
}


class IdentityAuthError(Exception):
    """Fail-closed authentication error with no credential material."""


class IdentityScopeError(IdentityAuthError):
    """The verified principal does not own the requested scope."""


@dataclass(frozen=True)
class IssuedCredential:
    """One-time plaintext handoff paired with its persisted public identifier."""

    identifier: uuid.UUID
    token: str


@dataclass(frozen=True)
class VerifiedServiceIdentity:
    principal_id: uuid.UUID
    principal_identifier: str
    principal_kind: str
    scopes: frozenset[str]
    token_id: uuid.UUID


@dataclass(frozen=True)
class VerifiedUserSession:
    session_id: uuid.UUID
    user_id: uuid.UUID
    username: str
    role: str
    auth_version: int
    expires_at: datetime


def scopes_for_principal_kind(kind: str) -> frozenset[str]:
    normalized = str(kind or "").strip().casefold().replace("-", "_")
    try:
        return SERVICE_PRINCIPAL_SCOPE_MATRIX[normalized]
    except KeyError as exc:
        raise ValueError("unsupported service principal kind") from exc


def validate_principal_scopes(kind: str, scopes: Iterable[str]) -> tuple[str, ...]:
    allowed = scopes_for_principal_kind(kind)
    normalized = tuple(sorted({str(scope or "").strip() for scope in scopes if str(scope or "").strip()}))
    if not set(normalized).issubset(allowed):
        raise ValueError("service principal scope exceeds kind matrix")
    return normalized


def digest_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def user_auth_state_digest(user: User) -> str:
    """Fingerprint every user field that must invalidate an existing session."""

    material = "\x1f".join((
        str(user.id),
        str(user.username or ""),
        str(user.password_hash or ""),
        str(user.role or ""),
        "1" if bool(user.is_active) else "0",
        str(int(getattr(user, "auth_version", 0) or 0)),
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def create_user_session(
    db,
    user: User,
    *,
    expires_at: datetime,
    now: datetime | None = None,
    secret_factory: Callable[[int], str] | None = None,
) -> IssuedCredential:
    now = _utc(now)
    expires_at = _utc(expires_at)
    if not user.is_active or expires_at <= now:
        raise IdentityAuthError("user session cannot be issued")

    session_id = uuid.uuid4()
    token = _build_token(TOKEN_PREFIX, session_id, secret_factory)
    db.add(AuthSession(
        id=session_id,
        user_id=user.id,
        subject=user.username,
        role=user.role,
        auth_version=int(getattr(user, "auth_version", 0) or 0),
        auth_state_digest=user_auth_state_digest(user),
        session_digest=digest_token(token),
        expires_at=expires_at,
        last_used_at=now,
        created_at=now,
    ))
    db.flush()
    return IssuedCredential(identifier=session_id, token=token)


def validate_user_session(
    db,
    token: str,
    *,
    now: datetime | None = None,
    touch_interval_seconds: int = DEFAULT_LAST_USED_TOUCH_SECONDS,
) -> VerifiedUserSession:
    now = _utc(now)
    session_id = _parse_token(token, TOKEN_PREFIX)
    session = db.get(AuthSession, session_id)
    if session is None or not hmac.compare_digest(str(session.session_digest or ""), digest_token(token)):
        raise IdentityAuthError("invalid user session")
    if session.revoked_at is not None or _utc(session.expires_at) <= now:
        raise IdentityAuthError("invalid user session")

    user = db.get(User, session.user_id)
    expected_state = user_auth_state_digest(user) if user is not None else ""
    if (
        user is None
        or not user.is_active
        or not hmac.compare_digest(str(session.auth_state_digest or ""), expected_state)
    ):
        raise IdentityAuthError("invalid user session")

    _touch(session, now, touch_interval_seconds)
    db.flush()
    return VerifiedUserSession(
        session_id=session.id,
        user_id=user.id,
        username=user.username,
        role=user.role,
        auth_version=int(getattr(user, "auth_version", 0) or 0),
        expires_at=_utc(session.expires_at),
    )


def revoke_user_session(db, token: str, *, now: datetime | None = None) -> uuid.UUID:
    now = _utc(now)
    session_id = _parse_token(token, TOKEN_PREFIX)
    session = db.get(AuthSession, session_id)
    if session is None or not hmac.compare_digest(str(session.session_digest or ""), digest_token(token)):
        raise IdentityAuthError("invalid user session")
    if session.revoked_at is None:
        session.revoked_at = now
        db.flush()
    return session.id


def issue_service_token(
    db,
    principal: ServicePrincipal,
    *,
    expires_at: datetime,
    now: datetime | None = None,
    secret_factory: Callable[[int], str] | None = None,
) -> IssuedCredential:
    now = _utc(now)
    expires_at = _utc(expires_at)
    if (
        not principal.is_active
        or expires_at <= now
        or (principal.expires_at is not None and _utc(principal.expires_at) <= now)
    ):
        raise IdentityAuthError("service token cannot be issued")
    validate_principal_scopes(principal.kind, principal.scopes or ())

    token_id = uuid.uuid4()
    token = _build_token(TOKEN_PREFIX, token_id, secret_factory)
    db.add(ServicePrincipalToken(
        id=token_id,
        principal_id=principal.id,
        token_digest=digest_token(token),
        issued_at=now,
        expires_at=expires_at,
        last_used_at=None,
    ))
    db.flush()
    return IssuedCredential(identifier=token_id, token=token)


def authenticate_service_token(
    db,
    token: str,
    *,
    required_scope: str | None = None,
    now: datetime | None = None,
    touch_interval_seconds: int = DEFAULT_LAST_USED_TOUCH_SECONDS,
) -> VerifiedServiceIdentity:
    now = _utc(now)
    token_id = _parse_token(token, TOKEN_PREFIX)
    stored = db.get(ServicePrincipalToken, token_id)
    if stored is None or not hmac.compare_digest(str(stored.token_digest or ""), digest_token(token)):
        raise IdentityAuthError("invalid service token")
    if stored.revoked_at is not None or _utc(stored.expires_at) <= now:
        raise IdentityAuthError("invalid service token")
    principal = db.get(ServicePrincipal, stored.principal_id)
    if (
        principal is None
        or not principal.is_active
        or (principal.expires_at is not None and _utc(principal.expires_at) <= now)
    ):
        raise IdentityAuthError("invalid service token")
    try:
        scopes = frozenset(validate_principal_scopes(principal.kind, principal.scopes or ()))
    except ValueError as exc:
        raise IdentityAuthError("invalid service principal policy") from exc
    if required_scope and required_scope not in scopes:
        raise IdentityScopeError("service principal scope denied")

    _touch(stored, now, touch_interval_seconds)
    _touch(principal, now, touch_interval_seconds)
    db.flush()
    return VerifiedServiceIdentity(
        principal_id=principal.id,
        principal_identifier=principal.identifier,
        principal_kind=principal.kind,
        scopes=scopes,
        token_id=stored.id,
    )


def rotate_service_token(
    db,
    principal: ServicePrincipal,
    *,
    expires_at: datetime,
    overlap_seconds: int,
    max_overlap_seconds: int,
    now: datetime | None = None,
    secret_factory: Callable[[int], str] | None = None,
) -> IssuedCredential:
    now = _utc(now)
    overlap_seconds = int(overlap_seconds)
    max_overlap_seconds = int(max_overlap_seconds)
    if overlap_seconds < 0 or max_overlap_seconds < 0 or overlap_seconds > max_overlap_seconds:
        raise ValueError("rotation overlap exceeds configured maximum")

    locked_principal = db.execute(
        select(ServicePrincipal)
        .where(ServicePrincipal.id == principal.id)
        .with_for_update()
    ).scalar_one_or_none()
    if locked_principal is None:
        raise IdentityAuthError("service principal is unavailable")
    principal = locked_principal
    overlap_deadline = now + timedelta(seconds=overlap_seconds)
    active_tokens = list(db.execute(
        select(ServicePrincipalToken)
        .where(ServicePrincipalToken.principal_id == principal.id)
        .where(ServicePrincipalToken.revoked_at.is_(None))
        .where(ServicePrincipalToken.expires_at > now)
        .with_for_update()
    ).scalars())
    issued = issue_service_token(
        db,
        principal,
        expires_at=expires_at,
        now=now,
        secret_factory=secret_factory,
    )
    for old_token in active_tokens:
        old_token.expires_at = min(_utc(old_token.expires_at), overlap_deadline)
        old_token.replaced_by_token_id = issued.identifier
    db.flush()
    return issued


def revoke_service_token(db, token_id: uuid.UUID | str, *, now: datetime | None = None) -> uuid.UUID:
    try:
        identifier = uuid.UUID(str(token_id))
    except (TypeError, ValueError) as exc:
        raise IdentityAuthError("invalid service token identifier") from exc
    stored = db.get(ServicePrincipalToken, identifier)
    if stored is None:
        raise IdentityAuthError("invalid service token identifier")
    if stored.revoked_at is None:
        stored.revoked_at = _utc(now)
        db.flush()
    return stored.id


def _build_token(prefix: str, identifier: uuid.UUID, secret_factory: Callable[[int], str] | None) -> str:
    factory = secret_factory or (lambda byte_count: secrets.token_urlsafe(byte_count))
    secret = str(factory(TOKEN_SECRET_BYTES) or "")
    if len(secret) < 32 or "." in secret:
        raise ValueError("credential secret factory returned invalid material")
    return f"{prefix}.{identifier.hex}.{secret}"


def _parse_token(token: str, expected_prefix: str) -> uuid.UUID:
    parts = str(token or "").split(".")
    if len(parts) != 3 or parts[0] != expected_prefix or len(parts[1]) != 32 or not parts[2]:
        raise IdentityAuthError("invalid credential format")
    try:
        return uuid.UUID(hex=parts[1])
    except ValueError as exc:
        raise IdentityAuthError("invalid credential format") from exc


def _touch(record, now: datetime, interval_seconds: int) -> None:
    previous = getattr(record, "last_used_at", None)
    if previous is None or now - _utc(previous) >= timedelta(seconds=max(0, int(interval_seconds))):
        record.last_used_at = now


def _utc(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
