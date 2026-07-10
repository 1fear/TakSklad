#!/usr/bin/env python3
"""Disposable Phase 26 deploy and code-rollback rehearsals.

All state is synthetic and confined to uniquely named local Docker resources.
The candidate backend is built from the exact commit bound by the verified
local release manifest.  Database rollback is deliberately forbidden.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from urllib.request import urlopen

from sqlalchemy import create_engine, text

from tools.import_identity_backfill import apply_backfill, analyze
from tools.release_artifacts import verify_manifest


ROOT = Path(__file__).resolve().parents[1]
POSTGRES_IMAGE = "postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
EXPECTED_HEAD = "20260711_0015"
MIGRATION_START = "20260710_0013"
KNOWN_WORKERS = ("google_sheets_sync", "skladbot", "smartup_auto_import", "telegram")
DEFAULT_MIGRATION_BUDGET_SECONDS = 120.0


class RehearsalError(RuntimeError):
    pass


def run(command: list[str], *, timeout: int = 600, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        tail = "\n".join(completed.stdout.splitlines()[-30:])
        raise RehearsalError(f"command exit={completed.returncode}: {' '.join(command[:6])}\n{tail}")
    return completed


def docker_absent(name: str) -> bool:
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"name=^/{name}$"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return not result.stdout.strip()


def remove_container(name: str) -> None:
    subprocess.run(
        ["docker", "rm", "--force", name],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def exact_backend_image(source_sha: str, tag: str) -> str:
    resolved = run(["git", "rev-parse", f"{source_sha}^{{commit}}"], timeout=30).stdout.strip()
    if resolved != source_sha:
        raise RehearsalError("release source SHA does not resolve exactly")
    with tempfile.TemporaryDirectory(prefix="taksklad-rehearsal-src-", dir=ROOT / ".release-state") as directory:
        archive_path = Path(directory) / "source.tar"
        with archive_path.open("wb") as output:
            completed = subprocess.run(
                ["git", "archive", "--format=tar", source_sha, "backend"],
                cwd=ROOT,
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
            )
        if completed.returncode:
            raise RehearsalError("cannot archive exact backend source")
        extract_root = Path(directory) / "extract"
        extract_root.mkdir()
        with tarfile.open(archive_path, "r:") as stream:
            members = stream.getmembers()
            if any(member.name.startswith("/") or ".." in Path(member.name).parts for member in members):
                raise RehearsalError("unsafe path in git archive")
            stream.extractall(extract_root, filter="data")
        run(["docker", "build", "--pull=false", "--tag", tag, str(extract_root / "backend")], timeout=1200)
    image_id = run(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], timeout=30).stdout.strip()
    if not image_id.startswith("sha256:"):
        raise RehearsalError("local immutable image ID is invalid")
    return image_id


def start_postgres(name: str) -> tuple[int, str]:
    password = "synthetic-phase26-only"
    run([
        "docker", "run", "--detach", "--name", name,
        "--tmpfs", "/var/lib/postgresql/data:rw,nosuid,nodev,size=512m",
        "--env", f"POSTGRES_PASSWORD={password}", "--env", "POSTGRES_DB=postgres",
        "--publish", "127.0.0.1::5432", POSTGRES_IMAGE,
    ], timeout=120)
    for _ in range(120):
        probe = subprocess.run(
            ["docker", "exec", name, "pg_isready", "-U", "postgres", "-d", "postgres"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if probe.returncode == 0:
            break
        time.sleep(0.25)
    else:
        raise RehearsalError("disposable PostgreSQL did not become ready")
    mapping = run(["docker", "port", name, "5432/tcp"], timeout=30).stdout.strip().splitlines()[0]
    port = int(mapping.rsplit(":", 1)[1])
    return port, f"postgresql+psycopg://postgres:{password}@127.0.0.1:{port}/postgres"


def alembic(url: str, *arguments: str) -> None:
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": url,
        "TAKSKLAD_ENV": "test",
        "TAKSKLAD_API_TOKEN": "synthetic-phase26-token",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    run([sys.executable, "-m", "alembic", "-c", "backend/alembic.ini", *arguments], env=env, timeout=300)


def prepare_database(url: str, *, migration_budget: float) -> dict[str, object]:
    alembic(url, "upgrade", MIGRATION_START)
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO orders (
                    id, source, external_id, payment_type, client, address, status, raw_payload
                ) VALUES (
                    '00000000-0000-0000-0000-000000002601', 'synthetic_rehearsal',
                    'synthetic-release-order', 'synthetic', 'SYNTHETIC CLIENT',
                    'SYNTHETIC ADDRESS', 'not_completed',
                    '{"order_key":"synthetic-release-order"}'::jsonb
                )
            """))
            connection.execute(text("""
                INSERT INTO order_items (
                    id, order_id, product, quantity_pieces, quantity_blocks, scanned_blocks,
                    requires_kiz, status, raw_payload
                ) VALUES (
                    '00000000-0000-0000-0000-000000002602',
                    '00000000-0000-0000-0000-000000002601', 'SYNTHETIC PRODUCT',
                    10, 1, 0, true, 'not_completed',
                    '{"item_key":"synthetic-release-item","source_import_id":"synthetic-release-row"}'::jsonb
                )
            """))
    finally:
        engine.dispose()
    started = time.monotonic()
    alembic(url, "upgrade", "head")
    migration_seconds = time.monotonic() - started
    if migration_seconds > migration_budget:
        raise RehearsalError(
            f"migration exceeded budget: {migration_seconds:.3f}s > {migration_budget:.3f}s"
        )
    before = analyze(url, batch_size=50, max_batches=10)
    if before["orders_candidates"] != 1 or before["items_candidates"] != 1 or before["conflicts"]:
        raise RehearsalError("synthetic backfill precondition is invalid")
    started = time.monotonic()
    applied = apply_backfill(url, batch_size=50, max_batches=10)
    backfill_seconds = time.monotonic() - started
    after = analyze(url, batch_size=50, max_batches=10)
    if after["orders_candidates"] or after["items_candidates"] or after["conflicts"]:
        raise RehearsalError("synthetic backfill did not converge")
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            for index, worker in enumerate(KNOWN_WORKERS, start=1):
                connection.execute(text("""
                    INSERT INTO worker_heartbeats (
                        worker_name, interval_seconds, grace_seconds, status, correlation_id,
                        last_cycle_started_at, last_success_at
                    ) VALUES (
                        :worker, 30, 15, 'success', :correlation_id, now(), now()
                    )
                    ON CONFLICT (worker_name) DO UPDATE SET
                        status='success', last_cycle_started_at=now(), last_success_at=now()
                """), {
                    "worker": worker,
                    "correlation_id": f"00000000-0000-0000-0000-{index:012d}",
                })
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            rows = connection.execute(text(
                "SELECT count(*) FROM orders WHERE external_id='synthetic-release-order'"
            )).scalar_one()
    finally:
        engine.dispose()
    if revision != EXPECTED_HEAD or rows != 1:
        raise RehearsalError("migration or synthetic data invariant failed")
    return {
        "schema_revision": revision,
        "migration_seconds": round(migration_seconds, 3),
        "migration_budget_seconds": migration_budget,
        "backfill_seconds": round(backfill_seconds, 3),
        "backfill_mutations": int(applied["updated_orders"]) + int(applied["updated_items"]),
        "synthetic_rows": int(rows),
    }


def start_backend(
    name: str,
    image: str,
    *,
    database_port: int,
    source_sha: str,
    identity_digest: str,
    require_workers: bool,
) -> int:
    command = [
        "docker", "run", "--detach", "--name", name,
        "--add-host", "host.docker.internal:host-gateway",
        "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--publish", "127.0.0.1::8000",
        "--env", "TAKSKLAD_ENV=local",
        "--env", "TAKSKLAD_INSECURE_LOCAL_ANONYMOUS=true",
        "--env", f"DATABASE_URL=postgresql+psycopg://postgres:synthetic-phase26-only@host.docker.internal:{database_port}/postgres",
        "--env", f"TAKSKLAD_COMMIT_SHA={source_sha}",
        "--env", f"TAKSKLAD_IMAGE_DIGEST={identity_digest}",
    ]
    if require_workers:
        command.extend(["--env", f"TAKSKLAD_REQUIRED_WORKERS={','.join(KNOWN_WORKERS)}"])
    command.append(image)
    run(command, timeout=60)
    mapping = run(["docker", "port", name, "8000/tcp"], timeout=30).stdout.strip().splitlines()[0]
    return int(mapping.rsplit(":", 1)[1])


def wait_endpoint(port: int, path: str, *, timeout_seconds: float = 30.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
                payload = json.load(response)
                if response.status == 200:
                    return payload
        except Exception as exc:  # endpoint startup failures are summarized, never logged verbatim
            last_error = exc.__class__.__name__
        time.sleep(0.25)
    raise RehearsalError(f"backend endpoint {path} did not become healthy ({last_error})")


def database_snapshot(url: str) -> tuple[str, int]:
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            return (
                str(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()),
                int(connection.execute(text(
                    "SELECT count(*) FROM orders WHERE external_id='synthetic-release-order'"
                )).scalar_one()),
            )
    finally:
        engine.dispose()


def worker_heartbeats_green(url: str) -> bool:
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            healthy = int(connection.execute(text(
                "SELECT count(*) FROM worker_heartbeats "
                "WHERE worker_name = ANY(CAST(:workers AS text[])) "
                "AND status='success' AND last_success_at IS NOT NULL"
            ), {"workers": list(KNOWN_WORKERS)}).scalar_one())
        return healthy == len(KNOWN_WORKERS)
    finally:
        engine.dispose()


def write_evidence(path: Path, evidence: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def deploy(args: argparse.Namespace) -> dict[str, object]:
    manifest = verify_manifest(args.manifest.resolve(), local=True)
    run_id = f"phase26-deploy-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    postgres_name = run_id + "-postgres"
    backend_name = run_id + "-backend"
    tag = f"taksklad-phase26-candidate:{manifest['source_sha'][:20]}"
    resources = (backend_name, postgres_name)
    try:
        runtime_image_id = exact_backend_image(manifest["source_sha"], tag)
        port, url = start_postgres(postgres_name)
        database = prepare_database(url, migration_budget=args.migration_budget_seconds)
        backend_port = start_backend(
            backend_name,
            tag,
            database_port=port,
            source_sha=manifest["source_sha"],
            identity_digest=manifest["images"]["backend"]["digest"],
            require_workers=True,
        )
        ready = wait_endpoint(backend_port, "/ready")
        if ready.get("ready") is not True or not worker_heartbeats_green(url):
            raise RehearsalError("readiness or required worker heartbeat gate failed")
        if ready.get("commit_sha") != manifest["source_sha"]:
            raise RehearsalError("runtime source SHA differs from verified release manifest")
        if ready.get("image_digest") != manifest["images"]["backend"]["digest"]:
            raise RehearsalError("runtime image identity differs from verified release manifest")
        evidence = {
            "status": "pass",
            "environment": "isolated",
            "synthetic_only": True,
            "source_sha": manifest["source_sha"],
            "backend_digest": manifest["images"]["backend"]["digest"],
            "frontend_digest": manifest["images"]["frontend"]["digest"],
            "runtime_backend_image_id": runtime_image_id,
            **database,
            "readiness": "green",
            "worker_heartbeats": "green",
            "cleanup_zero": False,
            "production_mutations": 0,
            "external_sends": 0,
        }
    finally:
        for resource in resources:
            remove_container(resource)
    cleanup_zero = all(docker_absent(resource) for resource in resources)
    if not cleanup_zero:
        raise RehearsalError("disposable deploy resources remain after cleanup")
    evidence["cleanup_zero"] = True
    write_evidence(args.evidence, evidence)
    return evidence


def rollback(args: argparse.Namespace) -> dict[str, object]:
    manifest = verify_manifest(args.manifest.resolve(), local=True)
    previous_sha = run(["git", "rev-parse", f"{manifest['source_sha']}^"], timeout=30).stdout.strip()
    run_id = f"phase26-rollback-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    postgres_name = run_id + "-postgres"
    candidate_name = run_id + "-candidate"
    previous_name = run_id + "-previous"
    candidate_tag = f"taksklad-phase26-candidate:{manifest['source_sha'][:20]}"
    previous_tag = f"taksklad-phase26-previous:{previous_sha[:20]}"
    resources = (candidate_name, previous_name, postgres_name)
    try:
        candidate_image_id = exact_backend_image(manifest["source_sha"], candidate_tag)
        previous_image_id = exact_backend_image(previous_sha, previous_tag)
        port, url = start_postgres(postgres_name)
        prepare_database(url, migration_budget=DEFAULT_MIGRATION_BUDGET_SECONDS)
        candidate_port = start_backend(
            candidate_name,
            candidate_tag,
            database_port=port,
            source_sha=manifest["source_sha"],
            identity_digest=manifest["images"]["backend"]["digest"],
            require_workers=True,
        )
        candidate_ready = wait_endpoint(candidate_port, "/ready")
        if (
            candidate_ready.get("ready") is not True
            or candidate_ready.get("commit_sha") != manifest["source_sha"]
            or candidate_ready.get("image_digest") != manifest["images"]["backend"]["digest"]
            or not worker_heartbeats_green(url)
        ):
            raise RehearsalError("candidate identity/readiness gate failed before rollback")
        schema_before, rows_before = database_snapshot(url)
        started = time.monotonic()
        remove_container(candidate_name)
        previous_port = start_backend(
            previous_name,
            previous_tag,
            database_port=port,
            source_sha=previous_sha,
            identity_digest=manifest["previous_release"]["backend_digest"],
            require_workers=False,
        )
        previous_health = wait_endpoint(previous_port, "/health")
        rollback_seconds = time.monotonic() - started
        if rollback_seconds > args.max_seconds:
            raise RehearsalError(
                f"rollback exceeded budget: {rollback_seconds:.3f}s > {args.max_seconds:.3f}s"
            )
        if previous_health.get("commit_sha") != previous_sha:
            raise RehearsalError("previous runtime did not expose the expected immutable source SHA")
        if previous_health.get("image_digest") != manifest["previous_release"]["backend_digest"]:
            raise RehearsalError("previous runtime did not expose the declared rollback digest")
        schema_after, rows_after = database_snapshot(url)
        if schema_after != schema_before or rows_after != rows_before:
            raise RehearsalError("code rollback changed schema or lost synthetic data")
        evidence = {
            "status": "pass",
            "environment": "isolated",
            "synthetic_only": True,
            "source_sha": manifest["source_sha"],
            "candidate_backend_digest": manifest["images"]["backend"]["digest"],
            "candidate_runtime_image_id": candidate_image_id,
            "previous_backend_digest": manifest["previous_release"]["backend_digest"],
            "previous_runtime_image_id": previous_image_id,
            "previous_source_sha": previous_sha,
            "rollback_seconds": round(rollback_seconds, 3),
            "max_seconds": args.max_seconds,
            "schema_before": schema_before,
            "schema_after": schema_after,
            "synthetic_rows_before": rows_before,
            "synthetic_rows_after": rows_after,
            "database_downgrade": 0,
            "data_loss": 0,
            "cleanup_zero": False,
            "production_mutations": 0,
            "external_sends": 0,
        }
    finally:
        for resource in resources:
            remove_container(resource)
    cleanup_zero = all(docker_absent(resource) for resource in resources)
    if not cleanup_zero:
        raise RehearsalError("disposable rollback resources remain after cleanup")
    evidence["cleanup_zero"] = True
    write_evidence(args.evidence, evidence)
    return evidence


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    deploy_parser = subparsers.add_parser("deploy")
    deploy_parser.add_argument("--manifest", type=Path, default=ROOT / "test-artifacts/release.json")
    deploy_parser.add_argument("--evidence", type=Path, default=ROOT / ".release-state/rehearsals/deploy-latest.json")
    deploy_parser.add_argument("--migration-budget-seconds", type=float, default=DEFAULT_MIGRATION_BUDGET_SECONDS)
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--manifest", type=Path, default=ROOT / "test-artifacts/release.json")
    rollback_parser.add_argument("--evidence", type=Path, default=ROOT / ".release-state/rehearsals/rollback-latest.json")
    rollback_parser.add_argument("--max-seconds", type=float, default=300.0)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        evidence = deploy(args) if args.command == "deploy" else rollback(args)
    except (OSError, RehearsalError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"REHEARSAL_FAIL mode={args.command} error={exc}\n")
        return 1
    marker = "REHEARSE_DEPLOY_OK" if args.command == "deploy" else "REHEARSE_ROLLBACK_OK"
    ordered = (
        ("source_sha", "backend_digest", "frontend_digest", "schema_revision", "migration_seconds",
         "backfill_seconds", "readiness", "worker_heartbeats", "synthetic_rows", "cleanup_zero",
         "production_mutations", "external_sends")
        if args.command == "deploy"
        else ("source_sha", "candidate_backend_digest", "previous_backend_digest", "rollback_seconds",
              "schema_before", "schema_after", "synthetic_rows_before", "synthetic_rows_after",
              "database_downgrade", "data_loss", "cleanup_zero", "production_mutations", "external_sends")
    )
    fields = " ".join(f"{key}={str(evidence[key]).lower() if isinstance(evidence[key], bool) else evidence[key]}" for key in ordered)
    sys.stdout.write(f"{marker} {fields} evidence={args.evidence}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
