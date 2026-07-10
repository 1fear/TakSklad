#!/usr/bin/env python3
"""Fail-closed pre-production scorecard for three isolated rehearsals."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from tools.release_artifacts import sha256_file, verify_manifest


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "test-artifacts/release-rehearsal"
SUMMARY = EVIDENCE / "summary.json"
MATRIX = EVIDENCE / "gate-matrix.json"
RELEASE = ROOT / "test-artifacts/release.json"
SBOM_MANIFEST = ROOT / "test-artifacts/sbom/manifest.sha256"
EXPECTED_DOMAINS = (
    "source_integrity",
    "code_quality",
    "data_integrity",
    "migration",
    "performance",
    "security",
    "browser_a11y",
    "supply_chain",
    "disaster_recovery",
    "observability",
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ScorecardError(RuntimeError):
    pass


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ScorecardError(f"{path.name} must contain an object")
    return value


def verify_sbom_manifest() -> int:
    count = 0
    for raw_line in SBOM_MANIFEST.read_text(encoding="utf-8").splitlines():
        digest, separator, relative = raw_line.partition("  ")
        if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ScorecardError("invalid SBOM manifest line")
        path = SBOM_MANIFEST.parent / relative
        if not path.is_file() or sha256_file(path) != digest:
            raise ScorecardError(f"SBOM hash mismatch: {relative}")
        count += 1
    if count < 4:
        raise ScorecardError("SBOM manifest is incomplete")
    return count


def provenance_source_sha(release: dict) -> str:
    envelope_path = ROOT / str(release.get("attestation", {}).get("bundle") or "")
    envelope = load_json(envelope_path)
    payload = json.loads(base64.b64decode(envelope.get("payload") or "", validate=True))
    return str(
        payload.get("predicate", {})
        .get("buildDefinition", {})
        .get("externalParameters", {})
        .get("sourceSha", "")
    )


def validate_run(path: Path, identity: dict) -> dict:
    run = load_json(path)
    if run.get("status") != "pass" or run.get("identity") != identity:
        raise ScorecardError(f"{path.name} did not pass with the shared identity")
    if (run.get("fresh_environment") or {}).get("cleanup_zero") is not True:
        raise ScorecardError(f"{path.name} did not clean its fresh environment")
    if any(int(run.get(key, -1)) != 0 for key in ("production_mutations", "external_sends", "production_deploys")):
        raise ScorecardError(f"{path.name} reports a forbidden side effect")
    deploy = run.get("deploy") or {}
    rollback = run.get("rollback") or {}
    if deploy.get("readiness") != "green" or deploy.get("worker_heartbeats") != "green":
        raise ScorecardError(f"{path.name} readiness/heartbeats are not green")
    if float(deploy.get("migration_seconds", 1e9)) > float(deploy.get("migration_budget_seconds", 0)):
        raise ScorecardError(f"{path.name} migration exceeded budget")
    if float(rollback.get("rollback_seconds", 1e9)) > 300:
        raise ScorecardError(f"{path.name} rollback exceeded 300 seconds")
    if int(rollback.get("database_downgrade", -1)) != 0 or int(rollback.get("data_loss", -1)) != 0:
        raise ScorecardError(f"{path.name} rollback changed schema or lost data")
    if any(gate.get("status") != "pass" or int(gate.get("exit_code", -1)) != 0 for gate in run.get("gates") or []):
        raise ScorecardError(f"{path.name} contains a failed gate")
    return run


def build_scorecard() -> dict:
    summary = load_json(SUMMARY)
    matrix = load_json(MATRIX)
    if summary.get("status") != "pass" or summary.get("all_gates_passed") is not True:
        raise ScorecardError("aggregated rehearsal did not pass")
    if summary.get("environment") != "isolated" or summary.get("repeat") != 3:
        raise ScorecardError("scorecard requires exactly three isolated rehearsals")
    if summary.get("same_artifact") is not True or summary.get("identities_equal") is not True:
        raise ScorecardError("rehearsals did not use one immutable artifact")
    if matrix.get("all_passed") is not True:
        raise ScorecardError("gate matrix is not green")
    domain_status = summary.get("domain_status") or {}
    if set(domain_status) != set(EXPECTED_DOMAINS) or any(domain_status[name] != "pass" for name in EXPECTED_DOMAINS):
        raise ScorecardError("ten-domain gate status is incomplete or failed")
    if any(int(summary.get(key, -1)) != 0 for key in ("production_mutations", "external_sends", "production_deploys")):
        raise ScorecardError("summary reports a forbidden side effect")

    identity = summary.get("identity") or {}
    source_sha = str(identity.get("source_sha") or "")
    if not SHA_RE.fullmatch(source_sha):
        raise ScorecardError("release source SHA is invalid")
    head_exists = subprocess.run(
        ["git", "cat-file", "-e", f"{source_sha}^{{commit}}"], cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if head_exists.returncode:
        raise ScorecardError("release source commit is unavailable")

    release = verify_manifest(RELEASE, local=True)
    if release.get("source_sha") != source_sha:
        raise ScorecardError("release manifest and rehearsal source SHA differ")
    windows = release.get("windows") or {}
    if windows.get("release_source_sha") != source_sha or windows.get("artifact_source_sha") != source_sha:
        raise ScorecardError("Windows artifact is not built from the rehearsal release SHA")
    if sha256_file(RELEASE) != identity.get("release_manifest_sha256"):
        raise ScorecardError("release manifest hash differs from rehearsal identity")
    if windows.get("artifact_sha256") != identity.get("windows_artifact_sha256"):
        raise ScorecardError("Windows artifact hash differs from rehearsal identity")
    if sha256_file(SBOM_MANIFEST) != identity.get("sbom_manifest_sha256"):
        raise ScorecardError("SBOM manifest hash differs from rehearsal identity")
    bundle = ROOT / str(release.get("attestation", {}).get("bundle") or "")
    if sha256_file(bundle) != identity.get("provenance_sha256"):
        raise ScorecardError("provenance hash differs from rehearsal identity")
    if provenance_source_sha(release) != source_sha:
        raise ScorecardError("provenance source SHA differs from rehearsal identity")
    sbom_subjects = verify_sbom_manifest()

    runs = summary.get("runs") or []
    if len(runs) != 3:
        raise ScorecardError("three run manifests are required")
    validated_runs = [validate_run(EVIDENCE / str(item.get("manifest") or ""), identity) for item in runs]
    blockers = [
        blocker for blocker in summary.get("blockers") or []
        if str(blocker.get("severity") or "").upper() in {"P0", "P1"}
    ]
    if blockers:
        raise ScorecardError("P0/P1 blockers remain")
    domains = {name: {"score": 10, "status": "pass"} for name in EXPECTED_DOMAINS}
    return {
        "schema_version": 1,
        "status": "pass",
        "mode": "preproduction",
        "score": 100,
        "max_score": 100,
        "domains": domains,
        "source_sha": source_sha,
        "runs": len(validated_runs),
        "gate_count": len(matrix.get("gate_ids") or []),
        "sbom_subjects": sbom_subjects,
        "windows_signature_authority": windows.get("signature_type"),
        "p0_p1_blockers": 0,
        "production_approval_gates": [
            "GitHub protected settings and CI identity",
            "production Windows certificate",
            "production deploy and live observation",
            "operator and physical warehouse acceptance",
        ],
        "truth": {
            "code": "confirmed",
            "tests": "confirmed",
            "isolated_runtime": "confirmed",
            "production_live": "pending approval",
            "operator_physical": "pending approval",
        },
        "production_mutations": 0,
        "external_sends": 0,
        "production_deploys": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preproduction", action="store_true")
    parser.add_argument("--require-no-p0-p1", action="store_true")
    args = parser.parse_args(argv)
    if not args.preproduction or not args.require_no_p0_p1:
        parser.error("--preproduction --require-no-p0-p1 are required")
    try:
        scorecard = build_scorecard()
    except (OSError, ValueError, ScorecardError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"RELEASE_SCORECARD_FAIL error={exc.__class__.__name__}:{exc}\n")
        return 1
    output = EVIDENCE / "scorecard.json"
    output.write_text(json.dumps(scorecard, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sys.stdout.write(
        "RELEASE_SCORECARD_OK score=100/100 domains=10 runs=3 p0_p1=0 "
        f"source_sha={scorecard['source_sha']} production_mutations=0 external_sends=0 "
        f"evidence={output}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
