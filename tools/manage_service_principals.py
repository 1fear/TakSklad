#!/usr/bin/env python3
"""Plan and locally provision revocable service-principal credentials.

The planning command is intentionally database-free and does not generate a
credential.  Mutating commands only accept an explicit local SQLAlchemy URL
and hand the plaintext token to a newly-created mode-0600 file exactly once.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from backend.app.auth_identities import (
    SERVICE_PRINCIPAL_SCOPE_MATRIX,
    issue_service_token,
    rotate_service_token,
    validate_principal_scopes,
)
from backend.app.models import AuditLog, ServicePrincipal


DEFAULT_ROTATION_MAX_OVERLAP_SECONDS = 900
DEFAULT_TOKEN_TTL_SECONDS = 31_536_000
HARD_ROTATION_MAX_OVERLAP_SECONDS = 3_600


class ServicePrincipalToolError(RuntimeError):
    """Safe CLI failure whose message contains no caller-supplied material."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="print the deterministic principal matrix")
    plan.add_argument("--dummy-only", action="store_true")

    provision = subparsers.add_parser("provision", help="create a principal and its first token")
    _add_mutation_arguments(provision)
    provision.add_argument("--identifier", required=True)
    provision.add_argument("--kind", choices=tuple(sorted(SERVICE_PRINCIPAL_SCOPE_MATRIX)), required=True)
    provision.add_argument(
        "--scope",
        action="append",
        default=[],
        help="repeat to provision a validated least-privilege subset of the kind matrix",
    )

    rotate = subparsers.add_parser("rotate", help="rotate an existing principal token")
    _add_mutation_arguments(rotate)
    rotate.add_argument("--identifier", required=True)
    rotate.add_argument("--overlap-seconds", type=int, default=300)
    rotate.add_argument(
        "--rotation-max-overlap-seconds",
        type=int,
        default=DEFAULT_ROTATION_MAX_OVERLAP_SECONDS,
    )
    return parser


def _add_mutation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--secret-file", required=True)
    parser.add_argument("--token-ttl-seconds", type=int, default=DEFAULT_TOKEN_TTL_SECONDS)


def print_plan() -> None:
    for kind in sorted(SERVICE_PRINCIPAL_SCOPE_MATRIX):
        scopes = ",".join(sorted(SERVICE_PRINCIPAL_SCOPE_MATRIX[kind]))
        sys.stdout.write(
            f"service_principal_plan identifier={kind} kind={kind} scopes={scopes}\n"
        )
    sys.stdout.write(
        "service_principal_plan_summary "
        f"principals={len(SERVICE_PRINCIPAL_SCOPE_MATRIX)} "
        f"rotation_max_overlap_seconds={DEFAULT_ROTATION_MAX_OVERLAP_SECONDS} "
        "secret_values=0\n"
    )


def validate_local_database_url(database_url: str) -> str:
    try:
        url = make_url(str(database_url or ""))
    except Exception as exc:
        raise ServicePrincipalToolError("database_url_invalid") from exc

    driver = url.get_backend_name()
    if driver == "sqlite":
        database = str(url.database or "")
        if not database or database == ":memory:" or not Path(database).is_absolute():
            raise ServicePrincipalToolError("database_url_not_local")
        return url.render_as_string(hide_password=False)

    if driver != "postgresql":
        raise ServicePrincipalToolError("database_url_not_local")

    host = str(url.host or "").strip()
    query_host = str(url.query.get("host") or "").strip()
    if query_host:
        if host or set(url.query) != {"host"}:
            raise ServicePrincipalToolError("database_url_not_local")
        if "\x00" in query_host or "," in query_host or not Path(query_host).is_absolute():
            raise ServicePrincipalToolError("database_url_not_local")
    elif host:
        if url.query:
            raise ServicePrincipalToolError("database_url_not_local")
        if host.casefold() != "localhost":
            try:
                if not ipaddress.ip_address(host).is_loopback:
                    raise ServicePrincipalToolError("database_url_not_local")
            except ValueError as exc:
                raise ServicePrincipalToolError("database_url_not_local") from exc
    else:
        # A PostgreSQL URL without a host uses the local Unix-domain socket.
        if url.query:
            raise ServicePrincipalToolError("database_url_not_local")
    return url.render_as_string(hide_password=False)


def validate_mutation_arguments(arguments) -> None:
    if not arguments.apply:
        raise ServicePrincipalToolError("apply_required")
    if int(arguments.token_ttl_seconds) <= 0:
        raise ServicePrincipalToolError("token_ttl_invalid")
    secret_path = Path(arguments.secret_file)
    if not secret_path.is_absolute():
        raise ServicePrincipalToolError("secret_file_not_absolute")
    if secret_path.exists():
        raise ServicePrincipalToolError("secret_file_exists")


def write_secret_once(path: str, token: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        payload = (str(token) + "\n").encode("utf-8")
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("credential handoff write failed")
            written += count
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
    except Exception:
        os.close(descriptor)
        Path(path).unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)


def provision(arguments, *, now: datetime | None = None) -> tuple[str, str]:
    validate_mutation_arguments(arguments)
    database_url = validate_local_database_url(arguments.database_url)
    now = _utc(now)
    expires_at = now + timedelta(seconds=int(arguments.token_ttl_seconds))
    requested_scopes = arguments.scope or SERVICE_PRINCIPAL_SCOPE_MATRIX[arguments.kind]
    try:
        scopes = validate_principal_scopes(arguments.kind, requested_scopes)
    except ValueError as exc:
        raise ServicePrincipalToolError("principal_scope_invalid") from exc
    engine = create_engine(database_url, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    secret_written = False
    try:
        with Session() as db:
            try:
                existing = db.execute(
                    select(ServicePrincipal).where(ServicePrincipal.identifier == arguments.identifier)
                ).scalar_one_or_none()
                if existing is not None:
                    raise ServicePrincipalToolError("principal_identifier_exists")
                principal = ServicePrincipal(
                    identifier=str(arguments.identifier).strip(),
                    kind=arguments.kind,
                    scopes=list(scopes),
                    is_active=True,
                    created_at=now,
                    updated_at=now,
                )
                if not principal.identifier:
                    raise ServicePrincipalToolError("principal_identifier_invalid")
                db.add(principal)
                db.flush()
                issued = issue_service_token(db, principal, expires_at=expires_at, now=now)
                db.add(AuditLog(
                    action="service_principal_provisioned",
                    entity_type="service_principal",
                    entity_id=str(principal.id),
                    payload={
                        "identifier": principal.identifier,
                        "kind": principal.kind,
                        "scopes": list(scopes),
                        "token_id": str(issued.identifier),
                    },
                ))
                write_secret_once(arguments.secret_file, issued.token)
                secret_written = True
                db.commit()
                return str(principal.id), str(issued.identifier)
            except Exception:
                db.rollback()
                if secret_written:
                    Path(arguments.secret_file).unlink(missing_ok=True)
                raise
    finally:
        engine.dispose()


def rotate(arguments, *, now: datetime | None = None) -> tuple[str, str]:
    validate_mutation_arguments(arguments)
    database_url = validate_local_database_url(arguments.database_url)
    max_overlap = int(arguments.rotation_max_overlap_seconds)
    overlap = int(arguments.overlap_seconds)
    if max_overlap < 0 or max_overlap > HARD_ROTATION_MAX_OVERLAP_SECONDS:
        raise ServicePrincipalToolError("rotation_max_overlap_invalid")
    if overlap < 0 or overlap > max_overlap:
        raise ServicePrincipalToolError("rotation_overlap_invalid")

    now = _utc(now)
    expires_at = now + timedelta(seconds=int(arguments.token_ttl_seconds))
    engine = create_engine(database_url, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    secret_written = False
    try:
        with Session() as db:
            try:
                principal = db.execute(
                    select(ServicePrincipal).where(ServicePrincipal.identifier == arguments.identifier)
                ).scalar_one_or_none()
                if principal is None:
                    raise ServicePrincipalToolError("principal_not_found")
                issued = rotate_service_token(
                    db,
                    principal,
                    expires_at=expires_at,
                    overlap_seconds=overlap,
                    max_overlap_seconds=max_overlap,
                    now=now,
                )
                db.add(AuditLog(
                    action="service_principal_token_rotated",
                    entity_type="service_principal",
                    entity_id=str(principal.id),
                    payload={
                        "identifier": principal.identifier,
                        "token_id": str(issued.identifier),
                        "overlap_seconds": overlap,
                    },
                ))
                write_secret_once(arguments.secret_file, issued.token)
                secret_written = True
                db.commit()
                return str(principal.id), str(issued.identifier)
            except Exception:
                db.rollback()
                if secret_written:
                    Path(arguments.secret_file).unlink(missing_ok=True)
                raise
    finally:
        engine.dispose()


def _utc(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def main(argv=None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "plan":
            if not arguments.dummy_only:
                raise ServicePrincipalToolError("dummy_only_required")
            print_plan()
            return 0
        if arguments.command == "provision":
            principal_id, token_id = provision(arguments)
            sys.stdout.write(
                f"service_principal_apply principal_id={principal_id} token_id={token_id} state=provisioned\n"
            )
            return 0
        if arguments.command == "rotate":
            principal_id, token_id = rotate(arguments)
            sys.stdout.write(
                f"service_principal_apply principal_id={principal_id} token_id={token_id} state=rotated\n"
            )
            return 0
        raise ServicePrincipalToolError("command_invalid")
    except ServicePrincipalToolError as exc:
        sys.stderr.write(f"service_principal_error class={exc}\n")
        return 2
    except Exception:
        sys.stderr.write("service_principal_error class=operation_failed\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
