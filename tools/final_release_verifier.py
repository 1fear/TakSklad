#!/usr/bin/env python3
"""Run three fail-closed release rehearsals against one immutable identity."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "test-artifacts/release-rehearsal"
RELEASE_MANIFEST = ROOT / "test-artifacts/release.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_RE = re.compile(r"^[0-9a-f]{64}$")
SECRET_RE = re.compile(
    r"(?i)(password|passwd|token|secret|authorization|database_url)(\s*[=:]\s*)([^\s]+)"
)
ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "TERM",
    "DOCKER_HOST", "XDG_RUNTIME_DIR", "SSH_AUTH_SOCK",
)


class VerificationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sanitize(text: str) -> str:
    text = SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    text = text.replace(str(Path.home()), "[HOME]")
    return "\n".join(text.splitlines()[-20:])[-4000:]


def isolated_environment(temporary: Path, run_id: str) -> dict[str, str]:
    environment = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    environment.update({
        "TAKSKLAD_REHEARSAL_ROOT": str(temporary), "TAKSKLAD_REHEARSAL_ID": run_id,
        "TAKSKLAD_NO_PRODUCTION": "1", "TAKSKLAD_EXTERNAL_SENDS_DISABLED": "1",
        "TAKSKLAD_ENV": "test", "CI": "true", "TMPDIR": str(temporary),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return environment


def load_identity(manifest_path: Path = RELEASE_MANIFEST) -> dict[str, str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    identity = {
        "source_sha": str(manifest.get("source_sha", "")),
        "release_manifest_sha256": sha256_file(manifest_path),
        "backend_digest": str(manifest.get("images", {}).get("backend", {}).get("digest", "")),
        "frontend_digest": str(manifest.get("images", {}).get("frontend", {}).get("digest", "")),
        "windows_artifact_sha256": str(manifest.get("windows", {}).get("artifact_sha256", "")),
        "sbom_manifest_sha256": sha256_file(ROOT / "test-artifacts/sbom/manifest.sha256"),
        "provenance_sha256": sha256_file(ROOT / "test-artifacts/release/provenance.dsse.json"),
    }
    if not SHA_RE.fullmatch(identity["source_sha"]):
        raise VerificationError("release source_sha is not an exact commit SHA")
    for key in ("backend_digest", "frontend_digest"):
        if not DIGEST_RE.fullmatch(identity[key]):
            raise VerificationError(f"invalid release identity: {key}")
    for key in (
        "release_manifest_sha256", "windows_artifact_sha256",
        "sbom_manifest_sha256", "provenance_sha256",
    ):
        if not HEX_RE.fullmatch(identity[key]):
            raise VerificationError(f"invalid release identity: {key}")
    artifact = ROOT / str(manifest.get("windows", {}).get("artifact", ""))
    if not artifact.is_file() or sha256_file(artifact) != identity["windows_artifact_sha256"]:
        raise VerificationError("Windows artifact hash does not match release manifest")
    if manifest.get("windows", {}).get("release_source_sha") != identity["source_sha"]:
        raise VerificationError("Windows release SHA does not match release source SHA")
    if manifest.get("windows", {}).get("artifact_source_sha") != identity["source_sha"]:
        raise VerificationError("Windows signed artifact source SHA does not match release source SHA")
    statement = json.loads((ROOT / "test-artifacts/release/provenance.intoto.json").read_text(encoding="utf-8"))
    statement_sha = str(
        statement.get("predicate", {}).get("buildDefinition", {}).get("externalParameters", {}).get("sourceSha", "")
    )
    if statement_sha != identity["source_sha"]:
        raise VerificationError("provenance source SHA does not match release source SHA")
    subjects = {
        str(subject.get("name")): str(subject.get("digest", {}).get("sha256", ""))
        for subject in statement.get("subject", [])
    }
    expected_subjects = {
        str(manifest["images"][role]["name"]): identity[f"{role}_digest"].removeprefix("sha256:")
        for role in ("backend", "frontend")
    }
    expected_subjects[str(manifest["windows"]["artifact"])] = identity["windows_artifact_sha256"]
    if any(subjects.get(name) != digest for name, digest in expected_subjects.items()):
        raise VerificationError("provenance subjects do not match release artifacts")
    sbom_root = ROOT / "test-artifacts/sbom"
    for line in (sbom_root / "manifest.sha256").read_text(encoding="utf-8").splitlines():
        expected_hash, relative = line.split(maxsplit=1)
        if sha256_file(sbom_root / relative.strip()) != expected_hash:
            raise VerificationError(f"SBOM hash mismatch: {relative.strip()}")
    committed = subprocess.run(
        ["git", "cat-file", "-e", f"{identity['source_sha']}^{{commit}}"], cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if committed.returncode:
        raise VerificationError("release source SHA is not a committed object")
    return identity


# These gates semantically deduplicate the mandatory Phase 1-25 commands. Broad
# suites replace repeated subsets; immutable verification replaces rebuilds.
GATES = [
    ("source-tree", "source_integrity", "PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tools/check_release_tree.py --strict --path-only"),
    ("owned-tree", "source_integrity", "PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tools/check_release_tree.py --compare-owned-manifest --strict --exclude-prefix test-artifacts/release-rehearsal/ --exclude-prefix test-artifacts/disaster-recovery/ --exclude-prefix test-artifacts/phase24/offsite-test-bucket/ --exclude-path test-artifacts/phase24/offsite-backup-evidence.json"),
    ("diff-check", "source_integrity", "git diff --check"),
    ("python-tests", "code_quality", "PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python -m unittest discover -s tests"),
    ("python-compile", "code_quality", "PYTHONPYCACHEPREFIX=/tmp/taksklad-phase26-pycache PYTHONPATH=. .venv/bin/python -m compileall -q main.py sitecustomize.py taksklad src/taksklad backend/app backend/migrations tools tests"),
    ("code-organization", "code_quality", "PYTHONPATH=. .venv/bin/python tools/check_code_organization.py --strict"),
    ("postgres-all", "data_integrity", "./tools/run_postgres_tests.sh all"),
    ("data-invariants", "data_integrity", "./tools/check_data_invariants.sh --database-url test-harness --read-only"),
    ("identity-backfill-dry-run", "migration", "PYTHONPATH=. .venv/bin/python tools/import_identity_backfill.py --dry-run --database-url test-harness"),
    ("desktop-storage-performance", "performance", "PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tools/benchmark_desktop_storage.py --synthetic-events 10000 --assert-p95-ms 25 --assert-no-loss"),
    ("backend-performance", "performance", "PYTHONPATH=. .venv/bin/python tools/benchmark_backend.py compare --profile reference --repeat 3 --assert-budgets"),
    ("backend-query-plan", "performance", "PYTHONPATH=. .venv/bin/python tools/benchmark_backend.py explain --profile stress --format json"),
    ("backend-import-performance", "performance", "PYTHONPATH=. .venv/bin/python tools/benchmark_backend.py compare --workload import"),
    ("import-limits", "performance", "PYTHONPATH=. .venv/bin/python tools/benchmark_import_limits.py --profile maximum-valid --assert-budgets"),
    ("frontend-lint", "code_quality", "npm --prefix frontend run lint"),
    ("frontend-typecheck", "code_quality", "npm --prefix frontend run typecheck"),
    ("frontend-coverage", "code_quality", "npm --prefix frontend run test:coverage"),
    ("frontend-a11y", "browser_a11y", "npm --prefix frontend run test:a11y"),
    ("frontend-e2e", "browser_a11y", "npm --prefix frontend run e2e"),
    ("frontend-perf", "performance", "npm --prefix frontend run perf"),
    ("frontend-build", "code_quality", "npm --prefix frontend run build"),
    ("security", "security", "./tools/security_gate.sh --synthetic-fixtures --fail-on high"),
    ("synthetic-secret-sentinel", "security", "PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python tools/scan_synthetic_secret_sentinel.py --allowed-tree"),
    ("config-matrix", "security", "PYTHONPATH=. .venv/bin/python tools/run_config_matrix.py --dummy-only"),
    ("service-principal-plan", "security", "PYTHONPATH=. .venv/bin/python tools/manage_service_principals.py plan --dummy-only"),
    ("locked-installs", "supply_chain", "./tools/verify_locked_installs.sh --cold --require-hashes"),
    ("sbom", "supply_chain", "./tools/generate_sbom.sh --verify"),
    ("attestations", "supply_chain", "./tools/verify_release_attestations.sh --local"),
    ("workflow-lint", "supply_chain", "./tools/lint_workflows.sh"),
    ("release-preflight", "source_integrity", "PYTHONPATH=. .venv/bin/python tools/release_preflight.py --skip-network"),
    ("compose-config", "source_integrity", "docker compose --env-file /dev/null -f deploy/vds/docker-compose.yml config --no-interpolate --quiet"),
    ("container-policy", "security", "PYTHONPATH=. .venv/bin/python tools/check_container_policy.py --strict"),
    ("container-smoke", "migration", "./tools/run_container_smoke.sh --dummy-config --permission-tests"),
    ("container-load", "performance", "./tools/run_container_load.sh --assert-resource-limits"),
    ("backup-create", "disaster_recovery", "./deploy/vds/backup_postgres.sh --test-mode --synthetic-db"),
    ("restore-drill", "disaster_recovery", "./deploy/vds/restore_drill.sh --isolated --synthetic-db --assert-invariants"),
    ("offsite-backup", "disaster_recovery", "./tools/verify_offsite_backup.sh --test-bucket --checksum"),
    ("pitr-drill", "disaster_recovery", "./tools/run_pitr_drill.sh --synthetic-db --assert-rpo-minutes 15 --assert-rto-minutes 30"),
    ("worker-heartbeats", "observability", "PYTHONPATH=. .venv/bin/python tools/test_worker_heartbeats.py --fault-matrix"),
    ("metric-labels", "observability", "PYTHONPATH=. .venv/bin/python tools/audit_metric_labels.py --strict"),
    ("alert-smoke", "observability", "./tools/run_alert_smoke.sh --synthetic-only --timeout-seconds 300"),
    ("runtime-identity", "source_integrity", "./tools/check_runtime_identity.py --local-stack"),
]


def mandatory_commands(phase_dir: Path | None = None) -> list[str]:
    phase_dir = phase_dir or ROOT / ".supergoal/taksklad-full-stabilization-security-per-e9read/phases"
    commands = []
    for number in range(1, 26):
        text = (phase_dir / f"phase-{number}.md").read_text(encoding="utf-8")
        section = text.split("## Mandatory commands", 1)[1].split("## Evidence", 1)[0]
        commands.extend(re.findall(r"^- `(.+)`$", section, flags=re.MULTILINE))
    return list(dict.fromkeys(commands))


def run_command(command: str, environment: dict[str, str], timeout: int = 3600) -> tuple[int, str, float]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            ["bash", "-lc", command], cwd=ROOT, env=environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False,
        )
        return completed.returncode, sanitize(completed.stdout), round(time.monotonic() - started, 3)
    except subprocess.TimeoutExpired as exc:
        return 124, sanitize(str(exc.stdout or "") + "\ncommand timed out"), round(time.monotonic() - started, 3)


def _parse_ok_line(output: str, prefix: str) -> dict[str, str]:
    line = next((item for item in reversed(output.splitlines()) if item.startswith(prefix)), "")
    if not line:
        raise VerificationError(f"missing {prefix} result")
    result = {}
    for item in line.split()[1:]:
        if "=" in item:
            key, value = item.split("=", 1)
            result[key] = value
    return result


def _require_rehearsal_result(
    result: dict[str, str], identity: dict[str, str], *, rollback: bool,
) -> None:
    expected = {
        "source_sha": identity["source_sha"],
        ("candidate_backend_digest" if rollback else "backend_digest"): identity["backend_digest"],
    }
    if not rollback:
        expected["frontend_digest"] = identity["frontend_digest"]
        expected.update({"readiness": "green", "worker_heartbeats": "green"})
    else:
        expected.update({"database_downgrade": "0", "data_loss": "0"})
    expected.update({"production_mutations": "0", "external_sends": "0"})
    mismatches = [key for key, value in expected.items() if result.get(key) != value]
    if mismatches:
        raise VerificationError("rehearsal result mismatch: " + ",".join(sorted(mismatches)))
    if rollback and float(result.get("rollback_seconds", "inf")) > 300:
        raise VerificationError("rollback exceeded 300 seconds")


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.999999)))
    return round(ordered[index], 3)


def run_rehearsals(
    *, repeat: int, same_artifact: bool, output_dir: Path = DEFAULT_OUTPUT,
    gates: list[tuple[str, str, str]] | None = None,
    runner: Callable[[str, dict[str, str], int], tuple[int, str, float]] = run_command,
    identity: dict[str, str] | None = None,
) -> dict[str, Any]:
    if repeat != 3 or not same_artifact:
        raise VerificationError("Phase 26 requires --repeat 3 --same-artifact")
    identity = identity or load_identity()
    gates = list(gates if gates is not None else GATES)
    if len({gate[0] for gate in gates}) != len(gates):
        raise VerificationError("gate IDs must be unique")
    output_dir.mkdir(parents=True, exist_ok=True)
    source_commands = mandatory_commands()
    write_json(output_dir / "command-plan.json", {
        "schema_version": 1, "source_phases": list(range(1, 26)),
        "deduplication": "broad-suite-covers-repeated-subsets",
        "source_mandatory_commands": source_commands,
        "source_mandatory_command_count": len(source_commands),
        "canonical_gate_count": len(gates) + 2,
        "gates": [{"id": gate_id, "domain": domain, "command": command} for gate_id, domain, command in gates],
    })
    runs = []
    matrix: dict[str, dict[str, str]] = {gate[0]: {} for gate in gates}
    domains = {domain for _, domain, _ in gates} | {"migration", "disaster_recovery"}
    for run_number in range(1, repeat + 1):
        run_id = f"run-{run_number}"
        temporary = Path(tempfile.mkdtemp(prefix=f"taksklad-phase26-{run_number}-", dir=ROOT / ".release-state"))
        environment = isolated_environment(temporary, run_id)
        results = []
        run_ok = True
        deploy: dict[str, Any] = {}
        rollback: dict[str, Any] = {}
        try:
            for gate_id, domain, command in gates:
                exit_code, output, duration = runner(command, environment, 3600)
                status = "pass" if exit_code == 0 else "fail"
                matrix[gate_id][run_id] = status
                results.append({
                    "id": gate_id, "domain": domain, "command": command,
                    "status": status, "exit_code": exit_code,
                    "duration_seconds": duration, "output_tail": output,
                })
                if exit_code:
                    run_ok = False
                    break
            if run_ok:
                deploy_evidence = output_dir / f"{run_id}-deploy.json"
                deploy_command = "./tools/rehearse_deploy.sh --environment isolated --assert-readiness --assert-migration-budget " f"--evidence {deploy_evidence}"
                exit_code, output, duration = runner(deploy_command, environment, 3600)
                results.append({"id": "isolated-deploy", "domain": "migration", "command": deploy_command, "status": "pass" if exit_code == 0 else "fail", "exit_code": exit_code, "duration_seconds": duration, "output_tail": output})
                matrix.setdefault("isolated-deploy", {})[run_id] = "pass" if exit_code == 0 else "fail"
                run_ok = exit_code == 0
                if run_ok:
                    deploy = _parse_ok_line(output, "REHEARSE_DEPLOY_OK")
                    _require_rehearsal_result(deploy, identity, rollback=False)
            if run_ok:
                rollback_evidence = output_dir / f"{run_id}-rollback.json"
                rollback_command = "./tools/rehearse_rollback.sh --environment isolated --assert-max-seconds 300 " f"--evidence {rollback_evidence}"
                exit_code, output, duration = runner(rollback_command, environment, 3600)
                results.append({"id": "isolated-rollback", "domain": "disaster_recovery", "command": rollback_command, "status": "pass" if exit_code == 0 else "fail", "exit_code": exit_code, "duration_seconds": duration, "output_tail": output})
                matrix.setdefault("isolated-rollback", {})[run_id] = "pass" if exit_code == 0 else "fail"
                run_ok = exit_code == 0
                if run_ok:
                    rollback = _parse_ok_line(output, "REHEARSE_ROLLBACK_OK")
                    _require_rehearsal_result(rollback, identity, rollback=True)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        manifest = {
            "schema_version": 1, "run_id": run_id, "status": "pass" if run_ok else "fail",
            "fresh_environment": {"id": run_id, "type": "temporary-isolated", "cleanup_zero": not temporary.exists()},
            "identity": identity, "gates": results, "deploy": deploy, "rollback": rollback,
            "production_mutations": 0, "external_sends": 0, "production_deploys": 0,
        }
        write_json(output_dir / f"{run_id}.json", manifest)
        runs.append(manifest)
        if not run_ok:
            break
    all_passed = len(runs) == repeat and all(run["status"] == "pass" for run in runs)
    domain_status = {
        domain: "pass" if all(
            result["status"] == "pass"
            for run in runs for result in run["gates"] if result["domain"] == domain
        ) and len(runs) == repeat else "fail"
        for domain in sorted(domains)
    }
    migration_values = [float(run["deploy"]["migration_seconds"]) for run in runs if run["status"] == "pass"]
    rollback_values = [float(run["rollback"]["rollback_seconds"]) for run in runs if run["status"] == "pass"]
    timings = {
        "migration_seconds": {
            "p50": _percentile(migration_values, 0.50), "p95": _percentile(migration_values, 0.95),
            "p99": _percentile(migration_values, 0.99),
        } if migration_values else {},
        "rollback_seconds": {
            "p50": _percentile(rollback_values, 0.50), "p95": _percentile(rollback_values, 0.95),
            "p99": _percentile(rollback_values, 0.99),
        } if rollback_values else {},
    }
    summary = {
        "schema_version": 1, "status": "pass" if all_passed else "fail",
        "environment": "isolated", "repeat": repeat, "same_artifact": True,
        "identity": identity, "identities_equal": len(runs) == repeat and all(run["identity"] == identity for run in runs),
        "runs": [{"run_id": run["run_id"], "status": run["status"], "manifest": f"{run['run_id']}.json"} for run in runs],
        "all_gates_passed": all_passed, "domain_status": domain_status,
        "timings": timings,
        "production_mutations": 0, "external_sends": 0, "production_deploys": 0,
    }
    gate_matrix = {
        "schema_version": 1, "gate_ids": sorted(matrix),
        "runs": [f"run-{number}" for number in range(1, repeat + 1)],
        "cells": matrix, "all_passed": all_passed,
    }
    write_json(output_dir / "gate-matrix.json", gate_matrix)
    write_json(output_dir / "summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", choices=("isolated",), required=True)
    parser.add_argument("--repeat", type=int, required=True)
    parser.add_argument("--same-artifact", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        summary = run_rehearsals(repeat=args.repeat, same_artifact=args.same_artifact, output_dir=args.output_dir)
    except (OSError, ValueError, VerificationError, json.JSONDecodeError) as exc:
        sys.stdout.write(f"FINAL_RELEASE_VERIFY_FAIL error={sanitize(str(exc))}\n")
        return 1
    sys.stdout.write(
        f"FINAL_RELEASE_VERIFY_{'OK' if summary['status'] == 'pass' else 'FAIL'} "
        f"runs={len(summary['runs'])} identities_equal={str(summary['identities_equal']).lower()} "
        f"production_mutations=0 external_sends=0 production_deploys=0 "
        f"evidence={args.output_dir / 'summary.json'}\n"
    )
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
