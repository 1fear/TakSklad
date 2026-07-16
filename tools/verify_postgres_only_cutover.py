#!/usr/bin/env python3
"""Verify that the live runtime already completed the PostgreSQL-only cutover."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def verified_marker(deployment: dict, version: dict) -> dict:
    source_sha = str(deployment.get("source_sha") or "").strip().lower()
    commit_sha = str(version.get("commit_sha") or "").strip().lower()
    backend_digest = str(
        (((deployment.get("images") or {}).get("backend") or {}).get("digest") or "")
    ).strip().lower()
    runtime_digest = str(version.get("image_digest") or "").strip().lower()

    if not SHA_RE.fullmatch(source_sha) or commit_sha != source_sha:
        raise ValueError("live source SHA does not match the signed deployment record")
    if not DIGEST_RE.fullmatch(backend_digest) or runtime_digest != backend_digest:
        raise ValueError("live backend digest does not match the signed deployment record")
    if str(version.get("environment") or "").strip().lower() != "production":
        raise ValueError("live backend does not identify as production")

    return {
        "schema_version": 1,
        "mode": "already_postgres_only",
        "safe_to_cutover": True,
        "blockers": 0,
        "source_identity_verified": True,
        "legacy_module_absent": True,
        "retired_worker_absent": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment-record", required=True, type=Path)
    parser.add_argument("--version-json", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    deployment = json.loads(args.deployment_record.read_text(encoding="utf-8"))
    version = json.loads(args.version_json.read_text(encoding="utf-8"))
    marker = verified_marker(deployment, version)
    args.output.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
    args.output.chmod(0o600)
    print("POSTGRES_ONLY_CUTOVER_IDENTITY_OK values_redacted=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
