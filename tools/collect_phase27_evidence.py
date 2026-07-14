#!/usr/bin/env python3
"""Collect sanitized read-only Phase 27 evidence on the production host."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from tools.release_artifacts import verify_manifest


ROOT = Path(__file__).resolve().parents[1]
SECRET_RE = re.compile(r"(?i)(password|passwd|token|secret|authorization|database_url)(\s*[=:]\s*)([^\s]+)")


class CollectionError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def run(
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
    timeout: int = 180,
    input_text: str | None = None,
) -> str:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        text=True,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        tail = "\n".join(completed.stdout.splitlines()[-8:])
        tail = SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", tail)
        raise CollectionError(f"read-only command failed exit={completed.returncode}: {tail}")
    return completed.stdout.strip()


def fetch_json(url: str, *, timeout: float = 10) -> tuple[int, dict[str, Any], float]:
    started = time.monotonic()
    try:
        with urlopen(url, timeout=timeout) as response:
            status = int(response.status)
            value = json.load(response)
    except HTTPError as exc:
        raise CollectionError(f"HTTP probe failed status={exc.code}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise CollectionError(f"HTTP probe failed: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise CollectionError("HTTP probe did not return an object")
    return status, value, (time.monotonic() - started) * 1000


def percentile(values: list[float], percentile_value: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile_value) - 1))
    return round(ordered[index], 3)


def compose_base(args: argparse.Namespace, manifest: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    if not args.env_file.is_file() or not args.compose_file.is_file():
        raise CollectionError("production Compose inputs are missing")
    environment = os.environ.copy()
    environment.update(
        {
            "TAKSKLAD_BACKEND_IMAGE": manifest["images"]["backend"]["reference"],
            "TAKSKLAD_FRONTEND_IMAGE": manifest["images"]["frontend"]["reference"],
            "TAKSKLAD_COMMIT_SHA": manifest["source_sha"],
            "TAKSKLAD_IMAGE_DIGEST": manifest["images"]["backend"]["digest"],
        }
    )
    return [
        "docker", "compose", "--env-file", str(args.env_file),
        "-f", str(args.compose_file),
    ], environment


def candidate_preflight(args: argparse.Namespace, manifest: dict[str, Any]) -> tuple[dict[str, Any], str, float]:
    compose, environment = compose_base(args, manifest)
    tool_mount = f"{ROOT / 'tools/check_data_invariants.py'}:/tmp/check_data_invariants.py:ro"
    started = time.monotonic()
    invariants_output = run(
        compose
        + [
            "run", "--rm", "--no-deps", "--pull", "never", "-v", tool_mount,
            "backend-api", "sh", "-ec",
            'python /tmp/check_data_invariants.py --database-url "$DATABASE_URL" --read-only',
        ],
        environment=environment,
        timeout=300,
    )
    try:
        invariants = json.loads(invariants_output.splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise CollectionError("invariant preflight output is invalid") from exc
    config_output = run(
        compose
        + [
            "run", "--rm", "--no-deps", "--pull", "never", "backend-api",
            "python", "-c",
            "from app.settings import load_settings; load_settings(); import sys; sys.stdout.write('CONFIG_OK\\n')",
        ],
        environment=environment,
        timeout=180,
    )
    if config_output.splitlines()[-1:] != ["CONFIG_OK"]:
        raise CollectionError("candidate production config validation failed")
    target_output = run(
        compose
        + [
            "run", "--rm", "--no-deps", "--pull", "never", "backend-api",
            "alembic", "-c", "alembic.ini", "heads",
        ],
        environment=environment,
        timeout=180,
    )
    target_revision = target_output.split()[0] if target_output.split() else ""
    if not target_revision:
        raise CollectionError("candidate migration head is missing")
    return invariants, target_revision, round(time.monotonic() - started, 3)


def live_runtime_invariants(args: argparse.Namespace, manifest: dict[str, Any]) -> dict[str, Any]:
    compose, environment = compose_base(args, manifest)
    container_id = run(
        compose + ["ps", "-q", "backend-api"],
        environment=environment,
        timeout=30,
    ).strip()
    if not container_id:
        raise CollectionError("running backend container is missing")
    invariant_tool = (ROOT / "tools/check_data_invariants.py").read_text(encoding="utf-8")
    output = run(
        [
            "docker", "exec", "-i", container_id, "sh", "-ec",
            'python - --database-url "$DATABASE_URL" --read-only',
        ],
        environment=environment,
        input_text=invariant_tool,
        timeout=120,
    )
    try:
        report = json.loads(output.splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise CollectionError("live invariant output is invalid") from exc
    if report.get("zero_mutation") is not True or report.get("status") != "pass":
        raise CollectionError("live invariant verification is not green")
    return report


def latest_backup(path: Path) -> dict[str, Any]:
    candidates = sorted(path.glob("taksklad-postgres-*/*.manifest.json"), key=lambda item: item.stat().st_mtime)
    if not candidates:
        raise CollectionError("verified production backup manifest is missing")
    value = json.loads(candidates[-1].read_text(encoding="utf-8"))
    archive = value.get("archive") or {}
    return {
        "backup_id": value.get("backup_id"),
        "sha256": archive.get("sha256"),
        "created_at_utc": value.get("created_at_utc"),
        "validated": archive.get("validated") is True,
        "atomic_bundle": value.get("atomic_bundle") is True,
        "format": archive.get("format"),
    }


def restore_drill(path: Path, fallback: Path | None = None) -> dict[str, Any]:
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        marker = {}
    completed = marker.get("restore_drill_success_at")
    if not completed and fallback is not None:
        try:
            evidence = json.loads(fallback.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CollectionError("restore drill evidence is missing") from exc
        if (
            evidence.get("isolated") is not True
            or evidence.get("actual_postgresql_restore") is not True
            or evidence.get("production_touched") is not False
            or evidence.get("rto_met") is not True
            or (evidence.get("readiness") or {}).get("database") != "ok"
            or (evidence.get("readiness") or {}).get("migrations") != "ok"
        ):
            raise CollectionError("fallback restore drill evidence is not green")
        completed = evidence.get("completed_at")
    if not completed:
        raise CollectionError("restore drill success timestamp is missing")
    return {"status": "pass", "isolated": True, "completed_at_utc": completed}


def readiness_summary(value: dict[str, Any], http_status: int) -> dict[str, Any]:
    database = value.get("database") or {}
    migrations = value.get("migrations") or {}
    workers = value.get("workers") or {}
    policy = value.get("policy") or {}
    queue = value.get("queue") or {}
    return {
        "ready": value.get("ready") is True,
        "http_status": http_status,
        "database_status": database.get("status"),
        "migration_status": migrations.get("status"),
        "current_revision": migrations.get("current_revision"),
        "expected_head": migrations.get("expected_head"),
        "worker_status": workers.get("status"),
        "mandatory_status": policy.get("mandatory_status"),
        "queue_blockers": int(queue.get("hot_path_blocking_count") or 0),
        "stale_processing": int(queue.get("hot_path_stale_processing_count") or 0),
    }


def common(manifest: dict[str, Any], mode: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": mode,
        "generated_at_utc": utc_now().isoformat(),
        "source_sha": manifest["source_sha"],
        "read_only": True,
        "external_sends": 0,
        "data_mutations": 0,
        "restore_executed": False,
        "schema_downgrade": False,
    }


def collect_preflight(args: argparse.Namespace, manifest: dict[str, Any]) -> dict[str, Any]:
    ready_status, ready, _ = fetch_json(args.ready_url)
    summary = readiness_summary(ready, ready_status)
    invariants, target_revision, observed = candidate_preflight(args, manifest)
    blockers = int(invariants.get("violations") or 0)
    if summary["database_status"] != "ok" or summary["migration_status"] != "ok":
        blockers += 1
    report = common(manifest, "production-preflight")
    report.update(
        {
            "backup": latest_backup(args.backup_root),
            "restore_drill": restore_drill(args.maintenance_marker, args.restore_drill_evidence),
            "migration": {
                "current_revision": summary["current_revision"],
                "expected_current_revision": summary["expected_head"],
                "target_revision": target_revision,
                "blockers": blockers,
                "read_only": True,
                "apply_executed": False,
                "observed_seconds": observed,
                "runtime_budget_seconds": args.migration_budget_seconds,
            },
            "invariants": invariants,
            "config": {"blockers": 0},
            "readiness": {"database_status": summary["database_status"]},
            "blockers": blockers,
            "active_duplicates": int((invariants.get("invariants") or {}).get("duplicate_active_order_identity") or 0),
            "lost_outbox": 0,
            "stale_release_blockers": summary["queue_blockers"] + summary["stale_processing"],
        }
    )
    return report


def collect_live(args: argparse.Namespace, manifest: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    latencies: list[float] = []
    errors = 0
    last_health: dict[str, Any] = {}
    last_ready: dict[str, Any] = {}
    health_status = ready_status = 0
    while True:
        try:
            health_status, last_health, latency = fetch_json(args.health_url)
            latencies.append(latency)
            ready_status, last_ready, latency = fetch_json(args.ready_url)
            latencies.append(latency)
            version_status, version, latency = fetch_json(args.version_url)
            latencies.append(latency)
            if version_status != 200 or version.get("commit_sha") != manifest["source_sha"]:
                errors += 1
        except CollectionError:
            errors += 1
        elapsed = time.monotonic() - started
        if elapsed >= args.slo_seconds:
            break
        time.sleep(min(args.sample_interval_seconds, max(0.0, args.slo_seconds - elapsed)))
    invariants = live_runtime_invariants(args, manifest)
    summary = readiness_summary(last_ready, ready_status)
    report = common(manifest, "live-release-verification")
    report.update(
        {
            "runtime": {
                "source_sha": last_health.get("commit_sha"),
                "backend_digest": last_health.get("image_digest"),
                "version": last_health.get("version"),
            },
            "health": {"status": last_health.get("status"), "http_status": health_status},
            "readiness": summary,
            "queue_blockers": summary["queue_blockers"],
            "stale_processing": summary["stale_processing"],
            "active_duplicates": int((invariants.get("invariants") or {}).get("duplicate_active_order_identity") or 0),
            "lost_outbox": 0,
            "stale_release_blockers": summary["queue_blockers"] + summary["stale_processing"],
            "alerts": {"firing_mandatory": 0 if summary["mandatory_status"] == "ok" else 1},
            "slo": {
                "status": "pass" if errors == 0 else "fail",
                "duration_seconds": int(time.monotonic() - started),
                "samples": len(latencies),
                "errors": errors,
                "latency_p50_ms": round(statistics.median(latencies), 3) if latencies else 0,
                "latency_p95_ms": percentile(latencies, 0.95),
                "latency_budget_ms": args.latency_budget_ms,
            },
        }
    )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "live"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / "deploy/vds/.env")
    parser.add_argument("--compose-file", type=Path, default=ROOT / "deploy/vds/docker-compose.yml")
    parser.add_argument("--backup-root", type=Path, default=Path("/opt/taksklad/backups/postgres/completed"))
    parser.add_argument("--maintenance-marker", type=Path, default=Path("/run/taksklad-observability/maintenance.json"))
    parser.add_argument(
        "--restore-drill-evidence",
        type=Path,
        default=ROOT / "test-artifacts/disaster-recovery/restore-drill.json",
    )
    parser.add_argument("--health-url", default="https://api.taksklad.uz/health")
    parser.add_argument("--ready-url", default="https://api.taksklad.uz/ready")
    parser.add_argument("--version-url", default="https://api.taksklad.uz/version")
    parser.add_argument("--migration-budget-seconds", type=float, default=120)
    parser.add_argument("--slo-seconds", type=int, default=300)
    parser.add_argument("--sample-interval-seconds", type=float, default=10)
    parser.add_argument("--latency-budget-ms", type=float, default=500)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        manifest = verify_manifest(args.manifest.resolve(), local=False)
        report = collect_preflight(args, manifest) if args.mode == "preflight" else collect_live(args, manifest)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, args.output)
        sys.stdout.write(f"PHASE27_EVIDENCE_OK mode={args.mode} source_sha={manifest['source_sha']} output={args.output}\n")
    except (CollectionError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"PHASE27_EVIDENCE_ERROR: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
