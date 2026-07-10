#!/usr/bin/env python3
"""Sanitized synthetic disaster-recovery drills for TakSklad.

This module never connects to production.  It creates a small deterministic
synthetic data set in a temporary directory and records only IDs, counts and
timings in the evidence file.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_VERSIONS = PROJECT_ROOT / "backend" / "migrations" / "versions"
DEFAULT_EVIDENCE_DIR = Path(
    os.environ.get(
        "TAKSKLAD_DR_EVIDENCE_DIR",
        str(PROJECT_ROOT / "test-artifacts" / "disaster-recovery"),
    )
)
POSTGRES_IMAGE = "postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _literal_assignment(tree: ast.Module, name: str) -> str | tuple[str, ...] | None:
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        value = ast.literal_eval(node.value)
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, (tuple, list)) and all(isinstance(item, str) for item in value):
            return tuple(value)
    return None


def migration_head() -> str:
    revisions: dict[str, tuple[str, ...]] = {}
    for path in sorted(ALEMBIC_VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        revision = _literal_assignment(tree, "revision")
        down_revision = _literal_assignment(tree, "down_revision")
        if not isinstance(revision, str):
            continue
        if down_revision is None:
            parents: tuple[str, ...] = ()
        elif isinstance(down_revision, str):
            parents = (down_revision,)
        else:
            parents = tuple(down_revision)
        revisions[revision] = parents

    referenced = {parent for parents in revisions.values() for parent in parents}
    heads = sorted(set(revisions) - referenced)
    if len(heads) != 1:
        raise RuntimeError(f"expected exactly one migration head, found {heads}")
    return heads[0]


def synthetic_snapshot(created_at: datetime) -> dict[str, Any]:
    return {
        "format": "taksklad-synthetic-backup-v1",
        "backup_id": f"synthetic-{created_at.strftime('%Y%m%dT%H%M%SZ')}",
        "created_at": iso_z(created_at),
        "migration_head": migration_head(),
        "tables": {
            "orders": ["order-001", "order-002", "order-003"],
            "order_items": ["item-001", "item-002", "item-003", "item-004", "item-005"],
            "scan_codes": ["scan-001", "scan-002", "scan-003", "scan-004"],
            "imports": ["import-001", "import-002"],
        },
        "relationships": {
            "item_order": [
                ["item-001", "order-001"],
                ["item-002", "order-001"],
                ["item-003", "order-002"],
                ["item-004", "order-002"],
                ["item-005", "order-003"],
            ],
            "scan_item": [
                ["scan-001", "item-001"],
                ["scan-002", "item-002"],
                ["scan-003", "item-003"],
                ["scan-004", "item-004"],
            ],
        },
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    partial.replace(path)


def validate_snapshot(snapshot: dict[str, Any]) -> tuple[dict[str, int], dict[str, bool]]:
    if snapshot.get("format") != "taksklad-synthetic-backup-v1":
        raise RuntimeError("unexpected synthetic backup format")
    if snapshot.get("migration_head") != migration_head():
        raise RuntimeError("restored migration revision is not current head")
    tables = snapshot.get("tables")
    relationships = snapshot.get("relationships")
    if not isinstance(tables, dict) or not isinstance(relationships, dict):
        raise RuntimeError("synthetic backup structure is incomplete")

    required = ("orders", "order_items", "scan_codes", "imports")
    counts = {table: len(tables.get(table, [])) for table in required}
    unique_ids = all(len(tables[table]) == len(set(tables[table])) for table in required)
    order_ids = set(tables["orders"])
    item_ids = set(tables["order_items"])
    item_order = relationships.get("item_order", [])
    scan_item = relationships.get("scan_item", [])
    no_orphan_items = all(item in item_ids and order in order_ids for item, order in item_order)
    no_orphan_scans = all(scan in set(tables["scan_codes"]) and item in item_ids for scan, item in scan_item)
    nonempty_required = all(counts[table] > 0 for table in required)
    invariants = {
        "unique_ids": unique_ids,
        "no_orphan_items": no_orphan_items,
        "no_orphan_scans": no_orphan_scans,
        "required_tables_nonempty": nonempty_required,
    }
    if not all(invariants.values()):
        raise RuntimeError(f"synthetic restore invariant failed: {invariants}")
    return counts, invariants


def _latest_synthetic_manifest() -> Path:
    backup_dir = Path(
        os.environ.get("TAKSKLAD_BACKUP_TEST_DIR")
        or PROJECT_ROOT / "test-artifacts" / "phase24" / "backups"
    )
    manifests = sorted(
        (backup_dir / "completed").glob(
            "taksklad-postgres-*-synthetic-*/*.manifest.json"
        )
    )
    if not manifests:
        environment = os.environ.copy()
        environment["TAKSKLAD_BACKUP_TEST_DIR"] = str(backup_dir)
        subprocess.run(
            [str(PROJECT_ROOT / "deploy" / "vds" / "backup_postgres.sh"), "--test-mode", "--synthetic-db"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        manifests = sorted(
            (backup_dir / "completed").glob(
                "taksklad-postgres-*-synthetic-*/*.manifest.json"
            )
        )
    if not manifests:
        raise RuntimeError("synthetic backup manifest was not created")
    return manifests[-1]


def _validate_archive(
    manifest_path: Path, *, require_synthetic: bool
) -> tuple[dict[str, Any], Path, str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive_info = manifest.get("archive") or {}
    list_info = archive_info.get("list") or {}
    if manifest.get("schema_version") != 2 or manifest.get("sanitized_manifest") is not True:
        raise RuntimeError("invalid synthetic backup manifest")
    if manifest.get("atomic_bundle") is not True:
        raise RuntimeError("restore drill requires an atomically published backup bundle")
    if require_synthetic and (
        manifest.get("source") != "synthetic-postgresql"
        or manifest.get("actual_postgresql") is not True
        or manifest.get("contains_customer_content") is not False
    ):
        raise RuntimeError("mandatory restore drill accepts only content-free synthetic backups")
    archive_format = str(archive_info.get("format") or "")
    if archive_format not in {
        "postgresql-custom",
        "postgresql-plain-sql-gzip-legacy-transition",
    }:
        raise RuntimeError("unexpected synthetic backup archive format")
    archive = manifest_path.parent / str(archive_info.get("filename"))
    inventory = manifest_path.parent / str(list_info.get("filename"))
    sidecar = manifest_path.parent / str(archive_info.get("checksum_sidecar"))
    if not archive.is_file() or not inventory.is_file() or not sidecar.is_file():
        raise RuntimeError("synthetic backup artifact group is incomplete")
    archive_sha = sha256_file(archive)
    if archive_sha != archive_info.get("sha256"):
        raise RuntimeError("synthetic archive checksum mismatch")
    if sha256_file(inventory) != list_info.get("sha256"):
        raise RuntimeError("synthetic archive list checksum mismatch")
    if sidecar.read_text(encoding="utf-8").strip() != f"{archive_sha}  {archive.name}":
        raise RuntimeError("synthetic checksum sidecar mismatch")

    if archive_format == "postgresql-custom":
        with archive.open("rb") as stream:
            header = stream.read(5)
        if header != b"PGDMP":
            raise RuntimeError("PostgreSQL custom archive header is invalid")
    else:
        with gzip.open(archive, "rt", encoding="utf-8") as stream:
            first = stream.readline().rstrip("\r\n")
            last_nonempty = ""
            for line in stream:
                if line.strip():
                    last_nonempty = line.strip()
        if first != "-- PostgreSQL database dump" or last_nonempty != "-- PostgreSQL database dump complete":
            raise RuntimeError("legacy PostgreSQL dump markers are invalid")
    return manifest, archive, archive_format


def _docker_text(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        error = completed.stderr.decode("utf-8", errors="replace").splitlines()[-5:]
        raise RuntimeError(f"disposable PostgreSQL command failed: {' | '.join(error)}")
    return completed.stdout.decode("utf-8", errors="strict").strip()


def _docker_stdin(command: list[str], source: Any) -> None:
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        stdin=source,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        error = completed.stderr.decode("utf-8", errors="replace").splitlines()[-5:]
        raise RuntimeError(f"disposable PostgreSQL streaming command failed: {' | '.join(error)}")


def run_restore_drill(
    evidence_path: Path,
    *,
    manifest_path: Path | None = None,
    require_synthetic: bool = True,
) -> dict[str, Any]:
    started = time.monotonic()
    created_at = utc_now()
    manifest_path = manifest_path or _latest_synthetic_manifest()
    manifest, archive, archive_format = _validate_archive(
        manifest_path.resolve(), require_synthetic=require_synthetic
    )
    archive_info = manifest["archive"]
    if manifest.get("postgres_image") != POSTGRES_IMAGE:
        raise RuntimeError("backup manifest PostgreSQL image is not allowlisted")
    image = POSTGRES_IMAGE
    container = f"taksklad-restore-synthetic-{os.getpid()}-{time.time_ns()}"
    password = f"synthetic-restore-{os.getpid()}"
    cleanup_count = -1
    counts: dict[str, int] = {}
    try:
        _docker_text(["docker", "image", "inspect", image])
        _docker_text(
            [
                "docker", "run", "-d", "--rm", "--name", container,
                "-e", f"POSTGRES_PASSWORD={password}",
                "-e", "POSTGRES_DB=taksklad_restore",
                "--tmpfs", "/var/lib/postgresql/data:rw,nosuid,nodev,noexec,size=384m",
                "--network", "none",
                image,
            ]
        )
        for _ in range(120):
            probe = subprocess.run(
                [
                    "docker", "exec", container, "psql", "-U", "postgres",
                    "-d", "taksklad_restore", "-At", "-c", "select 1;",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("disposable restore PostgreSQL did not become ready")
        if archive_format == "postgresql-custom":
            with archive.open("rb") as stream:
                _docker_stdin(
                    [
                        "docker", "exec", "-i", container, "pg_restore",
                        "-U", "postgres", "-d", "taksklad_restore",
                        "--exit-on-error", "--no-owner", "--no-acl",
                    ],
                    stream,
                )
        else:
            with gzip.open(archive, "rb") as stream:
                _docker_stdin(
                    [
                        "docker", "exec", "-i", container, "psql",
                        "-U", "postgres", "-d", "taksklad_restore",
                        "-v", "ON_ERROR_STOP=1",
                    ],
                    stream,
                )
        current_head = _docker_text(
            ["docker", "exec", container, "psql", "-U", "postgres", "-d", "taksklad_restore", "-At", "-v", "ON_ERROR_STOP=1", "-c", "select version_num from alembic_version;"]
        )
        if current_head != migration_head():
            raise RuntimeError(f"restored migration head mismatch: {current_head}")
        count_json = _docker_text(
            ["docker", "exec", container, "psql", "-U", "postgres", "-d", "taksklad_restore", "-At", "-v", "ON_ERROR_STOP=1", "-c", "select json_build_object('orders',(select count(*) from orders),'order_items',(select count(*) from order_items),'scan_codes',(select count(*) from scan_codes),'imports',(select count(*) from imports));"]
        )
        counts = {key: int(value) for key, value in json.loads(count_json).items()}
        probe_exists = _docker_text(
            ["docker", "exec", container, "psql", "-U", "postgres", "-d", "taksklad_restore", "-At", "-v", "ON_ERROR_STOP=1", "-c", "select to_regclass('public.synthetic_restore_probe') is not null;"]
        )
        counts["synthetic_restore_probe"] = (
            int(
                _docker_text(
                    ["docker", "exec", container, "psql", "-U", "postgres", "-d", "taksklad_restore", "-At", "-v", "ON_ERROR_STOP=1", "-c", "select count(*) from synthetic_restore_probe;"]
                )
            )
            if probe_exists == "t"
            else 0
        )
        orphan_count = int(
            _docker_text(
                ["docker", "exec", container, "psql", "-U", "postgres", "-d", "taksklad_restore", "-At", "-v", "ON_ERROR_STOP=1", "-c", "select (select count(*) from order_items i left join orders o on o.id=i.order_id where o.id is null) + (select count(*) from scan_codes s left join order_items i on i.id=s.order_item_id where i.id is null);"]
            )
        )
        database_probe = _docker_text(
            ["docker", "exec", container, "psql", "-U", "postgres", "-d", "taksklad_restore", "-At", "-v", "ON_ERROR_STOP=1", "-c", "select 1;"]
        )
        expected_probe = 1 if require_synthetic else counts.get("synthetic_restore_probe", 0)
        if counts.get("synthetic_restore_probe", 0) != expected_probe or orphan_count != 0 or database_probe != "1":
            raise RuntimeError("disposable restore count/invariant/readiness failed")
    finally:
        subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        remaining = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"name=^/{container}$"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        ).stdout.strip()
        cleanup_count = 0 if not remaining else 1
    if cleanup_count != 0:
        raise RuntimeError("disposable restore PostgreSQL cleanup failed")
    invariants = {
        "archive_checksum": True,
        "archive_list_checksum": True,
        "migration_head_current": True,
        "no_orphans": True,
        "synthetic_primary_key_count": counts["synthetic_restore_probe"] == 1,
    }

    duration_seconds = round(time.monotonic() - started, 3)
    evidence = {
        "schema": "taksklad-restore-drill-evidence-v2",
        "drill_mode": "disposable-postgresql-custom-archive",
        "drill_id": f"restore-{created_at.strftime('%Y%m%dT%H%M%SZ')}",
        "backup_id": manifest["backup_id"],
        "archive_sha256": archive_info["sha256"],
        "migration_head": migration_head(),
        "migration_head_assertion": "restored-database",
        "counts": counts,
        "invariants": invariants,
        "readiness": {
            "database": "ok",
            "migrations": "ok",
        },
        "duration_seconds": duration_seconds,
        "rto_target_minutes": 30,
        "rto_met": duration_seconds <= 30 * 60,
        "isolated": True,
        "production_touched": False,
        "actual_postgresql_restore": True,
        "disposable_cleanup_count": cleanup_count,
        "network_mode": "none",
        "actual_production_readiness": False,
        "customer_content_in_evidence": False,
        "source_backup_contains_customer_content": bool(
            manifest.get("contains_customer_content")
        ),
        "completed_at": iso_z(utc_now()),
    }
    write_json_atomic(evidence_path, evidence)
    return evidence


def run_pitr_drill(evidence_path: Path, rpo_limit: int, rto_limit: int) -> dict[str, Any]:
    started = time.monotonic()
    base = utc_now().replace(second=0, microsecond=0) - timedelta(minutes=12)
    snapshot = synthetic_snapshot(base)
    wal_events = [
        {"event_id": "wal-001", "at": base + timedelta(minutes=3), "table": "orders", "row_id": "order-004"},
        {"event_id": "wal-002", "at": base + timedelta(minutes=7), "table": "imports", "row_id": "import-003"},
        {"event_id": "wal-003", "at": base + timedelta(minutes=11), "table": "scan_codes", "row_id": "scan-005"},
    ]
    selected_timestamp = base + timedelta(minutes=9)
    applied = [event for event in wal_events if event["at"] <= selected_timestamp]
    for event in applied:
        snapshot["tables"][event["table"]].append(event["row_id"])
    counts, invariants = validate_snapshot(snapshot)
    last_recovered_at = max((event["at"] for event in applied), default=base)
    rpo_minutes = (selected_timestamp - last_recovered_at).total_seconds() / 60
    duration_seconds = round(time.monotonic() - started, 3)
    if rpo_minutes > rpo_limit:
        raise RuntimeError(f"synthetic RPO {rpo_minutes:.3f}m exceeds {rpo_limit}m")
    if duration_seconds > rto_limit * 60:
        raise RuntimeError(f"synthetic RTO {duration_seconds:.3f}s exceeds {rto_limit}m")

    evidence = {
        "schema": "taksklad-pitr-drill-evidence-v1",
        "drill_mode": "synthetic-wal-model",
        "drill_id": f"pitr-{utc_now().strftime('%Y%m%dT%H%M%SZ')}",
        "base_backup_id": snapshot["backup_id"],
        "selected_timestamp": iso_z(selected_timestamp),
        "last_recovered_timestamp": iso_z(last_recovered_at),
        "applied_wal_event_ids": [event["event_id"] for event in applied],
        "migration_head": snapshot["migration_head"],
        "counts": counts,
        "invariants": invariants,
        "rpo_minutes": round(rpo_minutes, 3),
        "rpo_target_minutes": rpo_limit,
        "rpo_met": True,
        "rto_seconds": duration_seconds,
        "rto_target_minutes": rto_limit,
        "rto_met": True,
        "isolated": True,
        "production_touched": False,
        "actual_postgresql_pitr": False,
        "actual_production_rpo_rto": False,
        "customer_content_in_evidence": False,
        "completed_at": iso_z(utc_now()),
    }
    write_json_atomic(evidence_path, evidence)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    restore = subparsers.add_parser("restore-drill")
    restore.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE_DIR / "restore-drill.json")
    restore.add_argument("--manifest", type=Path)
    pitr = subparsers.add_parser("pitr-drill")
    pitr.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE_DIR / "pitr-drill.json")
    pitr.add_argument("--assert-rpo-minutes", type=int, required=True)
    pitr.add_argument("--assert-rto-minutes", type=int, required=True)
    args = parser.parse_args()

    if args.command == "restore-drill":
        evidence = run_restore_drill(
            args.evidence,
            manifest_path=args.manifest,
            require_synthetic=args.manifest is None,
        )
        print(
            "RESTORE_DRILL_OK "
            f"drill_id={evidence['drill_id']} backup_id={evidence['backup_id']} "
            f"migration_head={evidence['migration_head']} counts={json.dumps(evidence['counts'], sort_keys=True)} "
            f"invariants=ok readiness=database-and-migrations-ok rto_seconds={evidence['duration_seconds']} "
            f"actual_postgresql=true cleanup_zero=true production_touched=false "
            f"evidence={args.evidence}"
        )
        return 0

    evidence = run_pitr_drill(args.evidence, args.assert_rpo_minutes, args.assert_rto_minutes)
    print(
        "PITR_DRILL_OK "
        f"drill_id={evidence['drill_id']} selected_timestamp={evidence['selected_timestamp']} "
        f"last_recovered_timestamp={evidence['last_recovered_timestamp']} "
        f"rpo_minutes={evidence['rpo_minutes']} rto_seconds={evidence['rto_seconds']} "
        f"migration_head={evidence['migration_head']} synthetic_model=true actual_postgresql=false "
        f"production_touched=false evidence={args.evidence}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
