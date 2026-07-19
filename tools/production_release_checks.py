#!/usr/bin/env python3
"""Fail-closed validators for sanitized Phase 27 production evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any

from tools.release_artifacts import ReleaseArtifactError, verify_manifest as verify_full_manifest


ROOT = Path(__file__).resolve().parents[1]
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PRE_EXPAND_DEFERRED_INVARIANTS = {
    "source_identity_pair_mismatch",
    "blank_materialized_identity",
    "duplicate_active_order_identity",
    "duplicate_order_source_identity",
    "duplicate_order_item_fallback_identity",
}
HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class ProductionCheckError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionCheckError(f"cannot read sanitized evidence: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise ProductionCheckError("sanitized evidence must be an object")
    return value


def parse_utc(value: Any, field: str) -> datetime:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductionCheckError(f"{field} is not an ISO UTC timestamp") from exc
    if parsed.tzinfo is None:
        raise ProductionCheckError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def age_seconds(timestamp: Any, field: str, *, now: datetime) -> float:
    age = (now - parse_utc(timestamp, field)).total_seconds()
    if age < -300:
        raise ProductionCheckError(f"{field} is unexpectedly in the future")
    return max(0.0, age)


def require_zero(value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value != 0:
        raise ProductionCheckError(f"{field} must be zero")


def load_production_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        release_kind = raw.get("release_kind")
        if release_kind in (None, "full"):
            return verify_full_manifest(path, local=False)
        if release_kind == "server":
            from tools.server_release_artifacts import verify_manifest as verify_server_manifest

            return verify_server_manifest(path)
        raise ProductionCheckError("unsupported release manifest kind")
    except (ReleaseArtifactError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise ProductionCheckError(str(exc)) from exc


def common_evidence(evidence: dict[str, Any], manifest: dict[str, Any], *, mode: str) -> None:
    if evidence.get("schema_version") != 1 or evidence.get("mode") != mode:
        raise ProductionCheckError(f"invalid {mode} evidence identity")
    source_sha = str(evidence.get("source_sha") or "")
    if not SHA_RE.fullmatch(source_sha) or source_sha != manifest.get("source_sha"):
        raise ProductionCheckError("evidence source SHA differs from release manifest")
    if evidence.get("read_only") is not True:
        raise ProductionCheckError("evidence is not marked read-only")
    require_zero(evidence.get("external_sends"), "external_sends")
    require_zero(evidence.get("data_mutations"), "data_mutations")
    if evidence.get("restore_executed") is not False:
        raise ProductionCheckError("production restore must not execute")
    if evidence.get("schema_downgrade") is not False:
        raise ProductionCheckError("schema downgrade must remain disabled")


def validate_preflight(
    evidence: dict[str, Any],
    manifest: dict[str, Any],
    *,
    require_current_backup: bool,
    require_zero_blockers: bool,
    now: datetime,
    max_backup_age_hours: float,
    max_restore_drill_age_hours: float,
) -> dict[str, Any]:
    common_evidence(evidence, manifest, mode="production-preflight")
    backup = evidence.get("backup") or {}
    backup_id = str(backup.get("backup_id") or "")
    backup_sha = str(backup.get("sha256") or "")
    if require_current_backup:
        if not backup_id or not HEX_RE.fullmatch(backup_sha):
            raise ProductionCheckError("verified backup identity is missing")
        if backup.get("validated") is not True or backup.get("atomic_bundle") is not True:
            raise ProductionCheckError("latest backup is not validated and atomic")
        if backup.get("format") != "postgresql-custom":
            raise ProductionCheckError("latest backup format is not PostgreSQL custom")
        if age_seconds(backup.get("created_at_utc"), "backup.created_at_utc", now=now) > max_backup_age_hours * 3600:
            raise ProductionCheckError("latest verified backup is stale")
    drill = evidence.get("restore_drill") or {}
    if drill.get("status") != "pass" or drill.get("isolated") is not True:
        raise ProductionCheckError("isolated restore drill evidence is not green")
    if age_seconds(drill.get("completed_at_utc"), "restore_drill.completed_at_utc", now=now) > max_restore_drill_age_hours * 3600:
        raise ProductionCheckError("isolated restore drill evidence is stale")
    migration = evidence.get("migration") or {}
    if migration.get("current_revision") != migration.get("expected_current_revision"):
        raise ProductionCheckError("current production migration identity is unexpected")
    if not str(migration.get("target_revision") or ""):
        raise ProductionCheckError("target migration revision is missing")
    require_zero(migration.get("blockers"), "migration.blockers")
    if migration.get("read_only") is not True or migration.get("apply_executed") is not False:
        raise ProductionCheckError("migration preflight was not read-only")
    observed = float(migration.get("observed_seconds") or -1)
    budget = float(migration.get("runtime_budget_seconds") or 0)
    if observed < 0 or budget <= 0 or observed > budget:
        raise ProductionCheckError("migration preflight exceeded its runtime budget")
    if manifest.get("release_kind") == "server":
        database_contract = manifest.get("database") or {}
        if database_contract.get("migration_policy") != "no_change":
            raise ProductionCheckError("server-only release migration policy must remain no_change")
        expected_head = str(database_contract.get("alembic_head") or "")
        if not expected_head:
            raise ProductionCheckError("server-only release Alembic head is missing")
        if (
            migration.get("current_revision") != expected_head
            or migration.get("target_revision") != expected_head
        ):
            raise ProductionCheckError("server-only release cannot change the production schema")
    invariants = evidence.get("invariants") or {}
    require_zero(invariants.get("violations"), "invariants.violations")
    if invariants.get("zero_mutation") is not True or invariants.get("automatic_repairs") != 0:
        raise ProductionCheckError("invariant preflight was not count-only")
    deferred_invariants = set(invariants.get("deferred_invariants") or [])
    if not deferred_invariants.issubset(PRE_EXPAND_DEFERRED_INVARIANTS):
        raise ProductionCheckError("invariant preflight deferred an unexpected check")
    if deferred_invariants and migration.get("current_revision") == migration.get("target_revision"):
        raise ProductionCheckError("target schema cannot retain deferred invariant checks")
    config = evidence.get("config") or {}
    require_zero(config.get("blockers"), "config.blockers")
    readiness = evidence.get("readiness") or {}
    if readiness.get("database_status") != "ok":
        raise ProductionCheckError("production database readiness is not green")
    if require_zero_blockers:
        for field in ("blockers", "active_duplicates", "lost_outbox", "stale_release_blockers"):
            require_zero(evidence.get(field), field)
    return {
        "source_sha": manifest["source_sha"],
        "backup_id": backup_id,
        "backup_sha256": backup_sha,
        "current_revision": migration.get("current_revision"),
        "target_revision": migration.get("target_revision"),
        "migration_seconds": observed,
        "blockers": 0,
    }


def validate_live(
    evidence: dict[str, Any],
    manifest: dict[str, Any],
    *,
    require_same_sha: bool,
    require_slo_window: bool,
) -> dict[str, Any]:
    common_evidence(evidence, manifest, mode="live-release-verification")
    runtime = evidence.get("runtime") or {}
    expected_digest = manifest["images"]["backend"]["digest"]
    if require_same_sha and runtime.get("source_sha") != manifest.get("source_sha"):
        raise ProductionCheckError("live runtime SHA differs from release manifest")
    if runtime.get("backend_digest") != expected_digest or not DIGEST_RE.fullmatch(str(runtime.get("backend_digest") or "")):
        raise ProductionCheckError("live backend digest differs from release manifest")
    compatibility = manifest.get("compatibility") or {}
    expected_runtime_version = (
        compatibility.get("min_desktop_version")
        if manifest.get("release_kind") == "server"
        else manifest.get("windows", {}).get("version")
    )
    if runtime.get("version") != expected_runtime_version:
        raise ProductionCheckError("live version differs from release manifest")
    if manifest.get("release_kind") == "server":
        if runtime.get("server_release_id") != manifest.get("server_release_id"):
            raise ProductionCheckError("live server release identity differs from release manifest")
        if runtime.get("desktop_api_contract") != compatibility.get("desktop_api_contract"):
            raise ProductionCheckError("live desktop API contract differs from release manifest")
    health = evidence.get("health") or {}
    readiness = evidence.get("readiness") or {}
    if health.get("status") != "ok" or health.get("http_status") != 200:
        raise ProductionCheckError("live health is not green")
    if readiness.get("ready") is not True or readiness.get("http_status") != 200:
        raise ProductionCheckError("live readiness is not green")
    if readiness.get("database_status") != "ok" or readiness.get("migration_status") != "ok":
        raise ProductionCheckError("live database or migration readiness is not green")
    if readiness.get("worker_status") != "ok" or readiness.get("mandatory_status") != "ok":
        raise ProductionCheckError("live workers or mandatory readiness are not green")
    invariants = evidence.get("invariants") or {}
    if invariants.get("deferred_invariants"):
        raise ProductionCheckError("live invariant verification has deferred checks")
    for field in ("queue_blockers", "stale_processing", "active_duplicates", "lost_outbox", "stale_release_blockers"):
        require_zero(evidence.get(field), field)
    alerts = evidence.get("alerts") or {}
    require_zero(alerts.get("firing_mandatory"), "alerts.firing_mandatory")
    if require_slo_window:
        slo = evidence.get("slo") or {}
        if slo.get("status") != "pass":
            raise ProductionCheckError("SLO observation window did not pass")
        duration = int(slo.get("duration_seconds") or 0)
        samples = int(slo.get("samples") or 0)
        errors_value = slo.get("errors")
        errors = int(errors_value) if errors_value is not None else -1
        if duration < 300 or samples < 20 or errors != 0:
            raise ProductionCheckError("SLO observation window is incomplete")
        p95 = float(slo.get("latency_p95_ms") or 0)
        budget = float(slo.get("latency_budget_ms") or 0)
        if p95 <= 0 or budget <= 0 or p95 > budget:
            raise ProductionCheckError("live latency exceeded the SLO budget")
    return {
        "source_sha": manifest["source_sha"],
        "backend_digest": expected_digest,
        "version": runtime.get("version"),
        "slo_duration_seconds": int((evidence.get("slo") or {}).get("duration_seconds") or 0),
        "slo_samples": int((evidence.get("slo") or {}).get("samples") or 0),
        "blockers": 0,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    subparsers = root.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--manifest", type=Path, default=Path("release.json"))
    preflight.add_argument("--evidence", type=Path, default=Path(".release-state/production-preflight.json"))
    preflight.add_argument("--read-only", action="store_true")
    preflight.add_argument("--require-current-backup", action="store_true")
    preflight.add_argument("--require-zero-blockers", action="store_true")
    preflight.add_argument("--max-backup-age-hours", type=float, default=24)
    preflight.add_argument("--max-restore-drill-age-hours", type=float, default=192)
    live = subparsers.add_parser("live")
    live.add_argument("--manifest", type=Path, default=Path("release.json"))
    live.add_argument("--evidence", type=Path, default=Path(".release-state/live-release-verification.json"))
    live.add_argument("--read-only", action="store_true")
    live.add_argument("--same-sha", action="store_true")
    live.add_argument("--slo-window", action="store_true")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if not args.read_only:
            raise ProductionCheckError("--read-only is required")
        manifest = load_production_manifest(args.manifest.resolve())
        evidence = load_json(args.evidence.resolve())
        if args.command == "preflight":
            result = validate_preflight(
                evidence,
                manifest,
                require_current_backup=args.require_current_backup,
                require_zero_blockers=args.require_zero_blockers,
                now=datetime.now(timezone.utc),
                max_backup_age_hours=args.max_backup_age_hours,
                max_restore_drill_age_hours=args.max_restore_drill_age_hours,
            )
            sys.stdout.write("PRODUCTION_PREFLIGHT_OK " + " ".join(f"{key}={value}" for key, value in result.items()) + "\n")
        else:
            result = validate_live(
                evidence,
                manifest,
                require_same_sha=args.same_sha,
                require_slo_window=args.slo_window,
            )
            sys.stdout.write("LIVE_RELEASE_VERIFY_OK " + " ".join(f"{key}={value}" for key, value in result.items()) + "\n")
    except (ProductionCheckError, OSError, ValueError, TypeError, KeyError) as exc:
        sys.stderr.write(f"PRODUCTION_RELEASE_CHECK_ERROR: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
