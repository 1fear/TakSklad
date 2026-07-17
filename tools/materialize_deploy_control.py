#!/usr/bin/env python3
"""Materialize the complete deployment control plane from one exact git commit."""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


DEPLOY_CONTROL_PATHS = (
    "backend/app/daily_report_config.py",
    "backend/app/telegram_output_contract.py",
    "backend/app/telegram_routing_contract.py",
    "backend/app/telegram_routing_manifest.json",
    "deploy/vds/deploy_from_git.sh",
    "deploy/vds/docker-compose.yml",
    "deploy/vds/acceptance_status.sh",
    "deploy/vds/backup_postgres.sh",
    "test-artifacts/disaster-recovery/restore-drill.json",
    "tools/check_data_invariants.py",
    "tools/collect_phase27_evidence.py",
    "tools/google_cutover_audit.py",
    "tools/live_release_verifier.sh",
    "tools/materialize_deploy_control.py",
    "tools/phase27_routing_candidate_guard.sh",
    "tools/prepare_notification_routing_env.py",
    "tools/production_preflight.sh",
    "tools/production_release_checks.py",
    "tools/release_artifacts.py",
    "tools/validate_daily_report_config.py",
    "tools/validate_deploy_probe.py",
    "tools/verify_telegram_routing_contract.py",
    "tools/verify_postgres_only_cutover.py",
    "tools/write_maintenance_marker.py",
)


class MaterializationError(RuntimeError):
    pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root")
    parser.add_argument("--source-sha")
    parser.add_argument("--output-dir")
    parser.add_argument("--print-paths", action="store_true")
    return parser.parse_args(argv)


def _git(repo_root: Path, *args: str) -> bytes:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_root), *args],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise MaterializationError("exact-SHA git object is unavailable") from exc


def materialize(repo_root: Path, source_sha: str, output_dir: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise MaterializationError("exact 40-character deployment control SHA is required")
    _git(repo_root, "cat-file", "-e", f"{source_sha}^{{commit}}")
    if output_dir.exists() or output_dir.is_symlink():
        raise MaterializationError("deployment control output directory must not already exist")
    output_dir.mkdir(mode=0o700, parents=True)
    os.chmod(output_dir, 0o700)
    for relative in DEPLOY_CONTROL_PATHS:
        posix_path = PurePosixPath(relative)
        if posix_path.is_absolute() or ".." in posix_path.parts:
            raise MaterializationError("unsafe deployment control path")
        payload = _git(repo_root, "cat-file", "blob", f"{source_sha}:{relative}")
        destination = output_dir.joinpath(*posix_path.parts)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.write_bytes(payload)
        os.chmod(destination, 0o600)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.print_paths:
        if any((args.repo_root, args.source_sha, args.output_dir)):
            print("DEPLOY_CONTROL_MATERIALIZATION_BLOCKED reason=print-paths-is-exclusive", file=sys.stderr)
            return 1
        print("\n".join(DEPLOY_CONTROL_PATHS))
        return 0
    if not all((args.repo_root, args.source_sha, args.output_dir)):
        print("DEPLOY_CONTROL_MATERIALIZATION_BLOCKED reason=required-arguments", file=sys.stderr)
        return 1
    try:
        materialize(Path(args.repo_root), str(args.source_sha), Path(args.output_dir))
    except (OSError, MaterializationError):
        print("DEPLOY_CONTROL_MATERIALIZATION_BLOCKED reason=exact-sha-closure", file=sys.stderr)
        return 1
    print(
        "DEPLOY_CONTROL_MATERIALIZATION_OK "
        f"files={len(DEPLOY_CONTROL_PATHS)} exact_sha=1 values_redacted=1"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
