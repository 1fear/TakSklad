"""One-shot, container-local service-principal provisioning with file handoff."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hmac
import os
import re
import stat
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from .auth_identities import (
    ACCEPTANCE_CANARY_IDENTIFIER,
    ACCEPTANCE_CANARY_SCOPES,
    SERVICE_PRINCIPAL_SCOPE_MATRIX,
    digest_token,
    issue_service_token,
    rotate_service_token,
)
from .models import AuditLog, ServicePrincipal, ServicePrincipalToken


IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,119}$")
HANDOFF_ROOT = Path("/run/taksklad-private")
DEFAULT_TTL_SECONDS = 31_536_000
SCOPED_TOKEN_RE = re.compile(r"^tks\.[0-9a-f]{32}\.[A-Za-z0-9_-]{32,}$")


class HandoffError(RuntimeError):
    pass


@dataclass(frozen=True)
class StatusResult:
    db_active: bool
    policy_exact: bool
    token_active: bool
    handoff_valid: bool

    @property
    def ready(self) -> bool:
        return self.db_active and self.policy_exact and self.token_active and self.handoff_valid

    def render(self) -> str:
        bit = lambda value: "1" if value else "0"
        return (
            f"db_active={bit(self.db_active)} policy_exact={bit(self.policy_exact)} "
            f"token_active={bit(self.token_active)} handoff_valid={bit(self.handoff_valid)} "
            f"ready={bit(self.ready)}"
        )


@dataclass(frozen=True)
class RevokeResult:
    cleanup: str

    def render(self) -> str:
        return f"state=db_revoked cleanup={self.cleanup}"


def required_scopes(kind: str) -> tuple[str, ...]:
    if kind == "acceptance":
        return tuple(sorted(ACCEPTANCE_CANARY_SCOPES))
    if kind == "desktop":
        return tuple(sorted(SERVICE_PRINCIPAL_SCOPE_MATRIX["desktop"]))
    raise HandoffError("kind_not_allowed")


def expected_handoff_path(kind: str) -> Path:
    return HANDOFF_ROOT / ("acceptance-canary.token" if kind == "acceptance" else "desktop-token")


def handoff_temp_residue(parent: Path) -> tuple[Path, ...]:
    residue = []
    for item in parent.iterdir():
        if not item.name.startswith(".token."):
            continue
        value = item.lstat()
        if (
            not stat.S_ISREG(value.st_mode)
            or stat.S_ISLNK(value.st_mode)
            or value.st_uid != os.geteuid()
            or stat.S_IMODE(value.st_mode) != 0o600
        ):
            raise HandoffError("handoff_temp_residue_unsafe")
        residue.append(item)
    return tuple(residue)


def expected_command_approval(command: str, kind: str) -> str:
    if command == "destroy-handoff":
        return f"DESTROY_{kind.upper()}_HANDOFF"
    return f"{command.upper()}_{kind.upper()}_PRINCIPAL"


def audit_action_for_command(command: str) -> str:
    return {
        "provision": "service_principal_handoff_provisioned",
        "rotate": "service_principal_handoff_rotated",
        "revoke": "service_principal_handoff_revoked",
        "destroy-handoff": "service_principal_handoff_destroyed",
        "reactivate": "service_principal_handoff_reactivated",
    }[command]


def default_session_factory():
    raw_url = os.environ.get("DATABASE_URL", "")
    if os.environ.get("TAKSKLAD_PRINCIPAL_HANDOFF_ROOT") != str(HANDOFF_ROOT):
        raise HandoffError("database_environment_invalid")
    try:
        url = make_url(raw_url)
    except Exception as exc:
        raise HandoffError("database_environment_invalid") from exc
    database = (url.database or "").lstrip("/")
    if (
        url.drivername != "postgresql+psycopg"
        or url.host != "postgres"
        or url.port not in (None, 5432)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", database)
        or not url.username
        or url.password is None
        or bool(url.query)
    ):
        raise HandoffError("database_environment_invalid")
    return sessionmaker(
        bind=create_engine(
            url,
            pool_pre_ping=True,
            connect_args={
                "connect_timeout": 5,
                "options": (
                    "-c statement_timeout=15000 "
                    "-c lock_timeout=5000 "
                    "-c idle_in_transaction_session_timeout=15000"
                ),
            },
        ),
        expire_on_commit=False,
    )


def validate_handoff_path(value: str, kind: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path != expected_handoff_path(kind):
        raise HandoffError("handoff_path_invalid")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise HandoffError("handoff_parent_invalid")
    stat_result = parent.stat(follow_symlinks=False)
    if stat_result.st_uid != os.geteuid() or (stat_result.st_mode & 0o022):
        raise HandoffError("handoff_parent_unsafe")
    if path.exists() or path.is_symlink():
        raise HandoffError("handoff_file_exists")
    return path


def validate_handoff_parent_path(value: str, kind: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path != expected_handoff_path(kind):
        raise HandoffError("handoff_path_invalid")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise HandoffError("handoff_parent_invalid")
    parent_stat = parent.stat(follow_symlinks=False)
    if parent_stat.st_uid != os.geteuid() or (parent_stat.st_mode & 0o022):
        raise HandoffError("handoff_parent_unsafe")
    return path


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def read_valid_handoff(path: Path, kind: str) -> str:
    if path != expected_handoff_path(kind):
        raise HandoffError("handoff_path_invalid")
    parent_stat = path.parent.lstat()
    file_stat = path.lstat()
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or stat.S_ISLNK(parent_stat.st_mode)
        or parent_stat.st_uid != os.geteuid()
        or stat.S_IMODE(parent_stat.st_mode) & 0o022
    ):
        raise HandoffError("handoff_parent_unsafe")
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or stat.S_ISLNK(file_stat.st_mode)
        or file_stat.st_uid != os.geteuid()
        or stat.S_IMODE(file_stat.st_mode) not in {0o400, 0o600}
        or file_stat.st_size < 2
        or file_stat.st_size > 4097
    ):
        raise HandoffError("handoff_file_unsafe")
    raw = path.read_bytes()
    payload = raw[:-2] if raw.endswith(b"\r\n") else raw[:-1] if raw.endswith(b"\n") else raw
    try:
        token = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise HandoffError("handoff_format_invalid") from exc
    if not SCOPED_TOKEN_RE.fullmatch(token):
        raise HandoffError("handoff_format_invalid")
    return token


def validate_published_handoff(path: Path, identity, token: str) -> None:
    current = path.lstat()
    if (current.st_dev, current.st_ino) != identity:
        raise HandoffError("handoff_identity_changed")
    stored = read_valid_handoff(path, "acceptance" if path.name == "acceptance-canary.token" else "desktop")
    if not hmac.compare_digest(stored, token):
        raise HandoffError("handoff_content_mismatch")


def remove_owned_handoff(path: Path, identity, *, restore_on_failure_payload: bytes | None = None) -> bool:
    try:
        current = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    if (current.st_dev, current.st_ino) != identity:
        return False
    path.unlink()
    try:
        fsync_parent(path)
    except Exception:
        if restore_on_failure_payload is not None:
            try:
                restore_handoff(path, None, restore_on_failure_payload)
            except Exception as exc:
                raise HandoffError("handoff_remove_recovery_unverified") from exc
        raise
    return True


def fsync_parent(path: Path) -> None:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path.parent, directory_flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_temporary_payload(parent: Path, payload: bytes) -> Path:
    descriptor, temporary = tempfile.mkstemp(prefix=".token.", dir=str(parent))
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(payload):
            try:
                count = os.write(descriptor, payload[written:])
            except InterruptedError:
                continue
            if count <= 0:
                raise OSError("handoff_write_failed")
            written += count
        os.fsync(descriptor)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    return temporary_path


def write_handoff(path: Path, token: str):
    temporary_path = _write_temporary_payload(path.parent, (token + "\n").encode("utf-8"))
    published_identity = None
    try:
        os.link(temporary_path, path)
        published = path.stat(follow_symlinks=False)
        published_identity = (published.st_dev, published.st_ino)
        temporary_path.unlink()
        fsync_parent(path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        if published_identity is not None:
            remove_owned_handoff(path, published_identity)
        raise
    return published_identity


def replace_handoff(path: Path, expected_identity, token: str):
    old_payload = path.read_bytes()
    current = path.lstat()
    if (current.st_dev, current.st_ino) != expected_identity:
        raise HandoffError("handoff_identity_changed")
    temporary_path = _write_temporary_payload(path.parent, (token + "\n").encode("utf-8"))
    temporary_stat = temporary_path.lstat()
    new_identity = (temporary_stat.st_dev, temporary_stat.st_ino)
    replaced = False
    try:
        current = path.lstat()
        if (current.st_dev, current.st_ino) != expected_identity:
            raise HandoffError("handoff_identity_changed")
        os.replace(temporary_path, path)
        replaced = True
        new = path.lstat()
        if (new.st_dev, new.st_ino) != new_identity:
            raise HandoffError("handoff_publish_identity_mismatch")
        fsync_parent(path)
        return new_identity, old_payload
    except Exception:
        if replaced:
            try:
                restore_handoff(path, new_identity, old_payload)
            except Exception as exc:
                raise HandoffError("handoff_replace_recovery_unverified") from exc
        raise
    finally:
        temporary_path.unlink(missing_ok=True)


def restore_handoff(path: Path, expected_current_identity, payload: bytes) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        current = None
    if current is not None and (current.st_dev, current.st_ino) != expected_current_identity:
        raise HandoffError("handoff_concurrent_change")
    temporary_path = _write_temporary_payload(path.parent, payload)
    try:
        if current is None:
            os.link(temporary_path, path)
            temporary_path.unlink()
        else:
            os.replace(temporary_path, path)
        fsync_parent(path)
        if not hmac.compare_digest(path.read_bytes(), payload):
            raise HandoffError("handoff_restore_unverified")
    finally:
        temporary_path.unlink(missing_ok=True)


def bound_handoff(db, principal, path: Path, kind: str, now: datetime):
    before = path.lstat()
    token = read_valid_handoff(path, kind)
    raw = path.read_bytes()
    after = path.lstat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise HandoffError("handoff_identity_changed")
    expected_raw = (token + "\n").encode("ascii")
    if not hmac.compare_digest(raw, expected_raw):
        raise HandoffError("handoff_content_changed")
    token_id = uuid.UUID(hex=token.split(".", 2)[1])
    stored = db.get(ServicePrincipalToken, token_id)
    if (
        stored is None
        or stored.principal_id != principal.id
        or stored.revoked_at is not None
        or _utc(stored.expires_at) <= now
        or not hmac.compare_digest(str(stored.token_digest or ""), digest_token(token))
    ):
        raise HandoffError("handoff_not_bound_to_active_token")
    return token, stored, (after.st_dev, after.st_ino), raw


def revoked_handoff_cleanup_candidate(db, principal, path: Path, kind: str):
    try:
        before = path.lstat()
    except FileNotFoundError:
        return "absent", None
    try:
        token = read_valid_handoff(path, kind)
        after = path.lstat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise HandoffError("handoff_identity_changed")
        token_id = uuid.UUID(hex=token.split(".", 2)[1])
        stored = db.get(ServicePrincipalToken, token_id)
        if (
            stored is None
            or stored.principal_id != principal.id
            or not hmac.compare_digest(str(stored.token_digest or ""), digest_token(token))
        ):
            raise HandoffError("handoff_not_bound_to_principal")
        return "bound", (after.st_dev, after.st_ino)
    except (HandoffError, OSError, ValueError):
        return "unverified", None


def status_result(db, principal, *, kind: str, handoff: Path, scopes: tuple[str, ...], now: datetime) -> StatusResult:
    policy_exact = bool(
        principal is not None
        and principal.kind == kind
        and tuple(sorted(principal.scopes or ())) == scopes
    )
    db_active = bool(
        principal is not None
        and principal.is_active
        and (principal.expires_at is None or _utc(principal.expires_at) > now)
    )
    token_active = False
    handoff_valid = False
    if not (db_active and policy_exact):
        return StatusResult(db_active, policy_exact, token_active, handoff_valid)
    try:
        token = read_valid_handoff(handoff, kind)
        token_id = uuid.UUID(hex=token.split(".", 2)[1])
        stored = db.get(ServicePrincipalToken, token_id)
        token_active = bool(
            stored is not None
            and stored.principal_id == principal.id
            and stored.revoked_at is None
            and _utc(stored.expires_at) > now
        )
        handoff_valid = bool(
            token_active
            and hmac.compare_digest(str(stored.token_digest or ""), digest_token(token))
        )
    except (HandoffError, OSError, ValueError):
        pass
    return StatusResult(db_active, policy_exact, token_active, handoff_valid)


def apply_command(args, *, session_factory=None, now=None) -> str:
    session_factory = session_factory or default_session_factory()
    now = now or datetime.now(timezone.utc)
    identifier = str(args.identifier or "").strip()
    if not IDENTIFIER_RE.fullmatch(identifier):
        raise HandoffError("identifier_invalid")
    if args.kind == "acceptance" and identifier != ACCEPTANCE_CANARY_IDENTIFIER:
        raise HandoffError("acceptance_identifier_invalid")
    if args.command == "destroy-handoff" and args.kind != "desktop":
        raise HandoffError("destroy_handoff_desktop_only")
    scopes = required_scopes(args.kind)
    operation_id = ""
    if args.command != "status":
        try:
            operation_id = str(uuid.UUID(str(getattr(args, "operation_id", "") or "")))
        except ValueError as exc:
            raise HandoffError("operation_id_invalid") from exc
        expected_approval = expected_command_approval(args.command, args.kind)
        if not getattr(args, "apply", False) or os.environ.get("TAKSKLAD_PRINCIPAL_COMMAND_APPROVAL") != expected_approval:
            raise HandoffError("exact_command_approval_required")
    handoff = expected_handoff_path(args.kind)
    if args.handoff_file:
        handoff = validate_handoff_parent_path(args.handoff_file, args.kind)
    if args.command != "revoke" and handoff_temp_residue(handoff.parent):
        raise HandoffError("handoff_temp_residue_present")
    with session_factory() as db:
        principal = db.execute(
            select(ServicePrincipal).where(ServicePrincipal.identifier == identifier).with_for_update()
        ).scalar_one_or_none()
        if args.command == "status":
            return status_result(db, principal, kind=args.kind, handoff=handoff, scopes=scopes, now=now)
        operation_rows = list(db.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "service_principal",
                AuditLog.payload["operation_id"].as_string() == operation_id,
            )
        ).scalars())
        if len(operation_rows) > 1:
            raise HandoffError("operation_id_not_unique")
        if operation_rows:
            previous = operation_rows[0]
            payload = previous.payload or {}
            if (
                previous.action != audit_action_for_command(args.command)
                or payload.get("identifier") != identifier
                or payload.get("kind") != args.kind
                or tuple(sorted(payload.get("scopes") or ())) != scopes
            ):
                raise HandoffError("operation_id_reuse_mismatch")
            if args.command == "revoke" and principal is not None and not principal.is_active:
                cleanup_state, cleanup_identity = revoked_handoff_cleanup_candidate(
                    db, principal, handoff, args.kind
                )
                if cleanup_state == "bound" and remove_owned_handoff(handoff, cleanup_identity):
                    cleanup_state = "removed"
                elif cleanup_state == "bound":
                    cleanup_state = "unverified"
                if handoff_temp_residue(handoff.parent):
                    cleanup_state = "residue_unverified"
                return RevokeResult(cleanup_state)
            if args.command == "destroy-handoff" and principal is not None and principal.is_active and not handoff.exists():
                return "already_applied"
            if principal is not None and status_result(
                db, principal, kind=args.kind, handoff=handoff, scopes=scopes, now=now
            ).ready:
                return "already_applied"
            raise HandoffError("operation_postcondition_unverified")
        if args.command == "provision":
            validate_handoff_path(str(handoff), args.kind)
        if args.command == "provision":
            if principal is not None:
                raise HandoffError("principal_exists")
            principal = ServicePrincipal(identifier=identifier, kind=args.kind, scopes=list(scopes), is_active=True)
            db.add(principal)
            db.flush()
            issued = issue_service_token(db, principal, expires_at=now + timedelta(seconds=args.ttl_seconds), now=now)
            action = "service_principal_handoff_provisioned"
        elif args.command == "rotate":
            if principal is None or not principal.is_active:
                raise HandoffError("principal_unavailable")
            if principal.kind != args.kind or tuple(sorted(principal.scopes or ())) != scopes:
                raise HandoffError("principal_policy_mismatch")
            if args.kind == "desktop" and (handoff.exists() or handoff.is_symlink()):
                raise HandoffError("desktop_rotation_handoff_must_be_absent")
            try:
                if args.kind == "desktop":
                    raise FileNotFoundError
                _, _, old_handoff_identity, old_handoff_payload = bound_handoff(
                    db, principal, handoff, args.kind, now
                )
                rotation_handoff_mode = "replace_bound"
            except FileNotFoundError:
                old_handoff_identity = None
                old_handoff_payload = None
                rotation_handoff_mode = "recover_absent"
            issued = rotate_service_token(
                db,
                principal,
                expires_at=now + timedelta(seconds=args.ttl_seconds),
                overlap_seconds=args.overlap_seconds,
                max_overlap_seconds=900,
                now=now,
            )
            action = "service_principal_handoff_rotated"
        elif args.command == "revoke":
            if principal is None:
                raise HandoffError("target_not_found")
            if principal.kind != args.kind or tuple(sorted(principal.scopes or ())) != scopes:
                raise HandoffError("principal_policy_mismatch")
            cleanup_state, revoke_cleanup_identity = revoked_handoff_cleanup_candidate(
                db, principal, handoff, args.kind
            )
            active_tokens = list(db.execute(
                select(ServicePrincipalToken).where(ServicePrincipalToken.principal_id == principal.id)
            ).scalars())
            revoke_already_applied = not principal.is_active and all(
                token.revoked_at is not None for token in active_tokens
            )
            if revoke_already_applied:
                if cleanup_state == "bound" and remove_owned_handoff(handoff, revoke_cleanup_identity):
                    cleanup_state = "removed"
                elif cleanup_state == "bound":
                    cleanup_state = "unverified"
                if handoff_temp_residue(handoff.parent):
                    cleanup_state = "residue_unverified"
                return RevokeResult(cleanup_state)
            principal.is_active = False
            for token in active_tokens:
                if token.revoked_at is None:
                    token.revoked_at = now
            issued = None
            action = "service_principal_handoff_revoked"
        elif args.command == "destroy-handoff":
            if principal is None or not principal.is_active:
                raise HandoffError("principal_unavailable")
            if principal.kind != args.kind or tuple(sorted(principal.scopes or ())) != scopes:
                raise HandoffError("principal_policy_mismatch")
            _, _, removed_handoff_identity, removed_handoff_payload = bound_handoff(
                db, principal, handoff, args.kind, now
            )
            issued = None
            action = "service_principal_handoff_destroyed"
        elif args.command == "reactivate":
            if principal is None or principal.is_active:
                raise HandoffError("principal_not_inactive")
            if principal.kind != args.kind or tuple(sorted(principal.scopes or ())) != scopes:
                raise HandoffError("principal_policy_mismatch")
            tokens = list(db.execute(
                select(ServicePrincipalToken).where(ServicePrincipalToken.principal_id == principal.id)
            ).scalars())
            if any(token.revoked_at is None for token in tokens) or handoff.exists() or handoff.is_symlink():
                raise HandoffError("reactivation_precondition_failed")
            principal.is_active = True
            issued = issue_service_token(
                db, principal, expires_at=now + timedelta(seconds=args.ttl_seconds), now=now
            )
            action = "service_principal_handoff_reactivated"
        else:
            raise HandoffError("command_invalid")
        audit_payload = {
            "identifier": principal.identifier,
            "kind": principal.kind,
            "scopes": list(sorted(principal.scopes or ())),
            "operation_id": operation_id,
        }
        if issued is not None:
            audit_payload.update({
                "token_id": str(issued.identifier),
                "ttl_seconds": int(args.ttl_seconds),
                "overlap_seconds": int(args.overlap_seconds) if args.command == "rotate" else 0,
            })
            if args.command == "rotate":
                audit_payload["handoff_mode"] = rotation_handoff_mode
        db.add(AuditLog(
            action=action,
            entity_type="service_principal",
            entity_id=str(principal.id),
            payload=audit_payload,
        ))
        handoff_identity = None
        replacement_identity = None
        replacement_old_payload = None
        removed_before_commit = False
        try:
            if issued is not None:
                if args.command == "rotate":
                    if rotation_handoff_mode == "replace_bound":
                        replacement_identity, replacement_old_payload = replace_handoff(
                            handoff, old_handoff_identity, issued.token
                        )
                        validate_published_handoff(handoff, replacement_identity, issued.token)
                    else:
                        handoff_identity = write_handoff(handoff, issued.token)
                        validate_published_handoff(handoff, handoff_identity, issued.token)
                else:
                    handoff_identity = write_handoff(handoff, issued.token)
                    validate_published_handoff(handoff, handoff_identity, issued.token)
            elif args.command == "destroy-handoff":
                if not remove_owned_handoff(
                    handoff,
                    removed_handoff_identity,
                    restore_on_failure_payload=removed_handoff_payload,
                ):
                    raise HandoffError("handoff_cleanup_unverified")
                removed_before_commit = True
            db.commit()
        except Exception:
            if handoff is not None and handoff_identity is not None:
                remove_owned_handoff(handoff, handoff_identity)
            if replacement_identity is not None and replacement_old_payload is not None:
                restore_handoff(handoff, replacement_identity, replacement_old_payload)
            if removed_before_commit:
                restore_handoff(handoff, None, removed_handoff_payload)
            raise
        if args.command == "revoke":
            if cleanup_state == "bound" and remove_owned_handoff(handoff, revoke_cleanup_identity):
                cleanup_state = "removed"
            elif cleanup_state == "bound":
                cleanup_state = "unverified"
            if handoff_temp_residue(handoff.parent):
                cleanup_state = "residue_unverified"
            return RevokeResult(cleanup_state)
    if args.command == "provision":
        return "provisioned"
    if args.command == "destroy-handoff":
        return "handoff_destroyed"
    return args.command + "d"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("provision", "rotate", "revoke", "destroy-handoff", "reactivate", "status")
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--identifier", required=True)
    parser.add_argument("--kind", choices=("acceptance", "desktop"), required=True)
    parser.add_argument("--handoff-file", default="")
    parser.add_argument("--ttl-seconds", type=int, default=DEFAULT_TTL_SECONDS)
    parser.add_argument("--overlap-seconds", type=int, default=0)
    parser.add_argument("--operation-id", default="")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.ttl_seconds <= 0 or not 0 <= args.overlap_seconds <= 900:
            raise HandoffError("lifetime_invalid")
        state = apply_command(args)
    except HandoffError as exc:
        print(f"PRINCIPAL_HANDOFF_BLOCKED reason={exc}", file=sys.stderr)
        return 2
    except Exception:
        print("PRINCIPAL_HANDOFF_FATAL reason=operation_failed", file=sys.stderr)
        return 1
    if isinstance(state, StatusResult) and not state.ready:
        print(f"PRINCIPAL_HANDOFF_BLOCKED {state.render()} kind={args.kind} secret_output=0", file=sys.stderr)
        return 3
    if isinstance(state, RevokeResult) and state.cleanup not in {"removed", "absent"}:
        print(f"PRINCIPAL_HANDOFF_BLOCKED {state.render()} kind={args.kind} secret_output=0", file=sys.stderr)
        return 4
    rendered = state.render() if isinstance(state, (StatusResult, RevokeResult)) else f"state={state}"
    print(f"PRINCIPAL_HANDOFF_OK {rendered} kind={args.kind} secret_output=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
