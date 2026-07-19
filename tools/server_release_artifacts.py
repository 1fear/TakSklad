#!/usr/bin/env python3
"""Fail-closed verifier for immutable server-only TakSklad releases."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
APPROVED_IMAGES = {
    "backend": "ghcr.io/1fear/taksklad-backend",
    "frontend": "ghcr.io/1fear/taksklad-frontend",
}
DESKTOP_API_CONTRACT = 1
MIN_DESKTOP_VERSION = "2.0.49"
MIGRATION_POLICY = "no_change"
REQUIRED_CAPABILITIES = frozenset({"returns_auth_canary_v2_exact_identifier"})
ALEMBIC_REVISION_RE = re.compile(r"^[A-Za-z0-9_]+$")


class ServerReleaseArtifactError(RuntimeError):
    """The server release manifest is incomplete, mutable, or inconsistent."""


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ServerReleaseArtifactError(
            f"server release manifest cannot be read: {type(exc).__name__}"
        ) from exc
    if not isinstance(value, dict):
        raise ServerReleaseArtifactError("server release manifest must be an object")
    return value


def _validate_ci_identity(ci: Any, source_sha: str) -> None:
    if not isinstance(ci, dict):
        raise ServerReleaseArtifactError("server release CI identity is missing")
    expected = {
        "workflow": "CI",
        "head_sha": source_sha,
        "event": "push",
        "head_branch": "main",
        "required_check": "Release gate",
        "conclusion": "success",
    }
    if any(ci.get(key) != value for key, value in expected.items()):
        raise ServerReleaseArtifactError("server release CI identity is invalid")
    for field in ("run_id", "run_attempt"):
        value = ci.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ServerReleaseArtifactError(f"server release CI {field} is invalid")


def _validate_images(images: Any) -> set[tuple[str, str, str, str]]:
    if not isinstance(images, dict) or set(images) != set(APPROVED_IMAGES):
        raise ServerReleaseArtifactError(
            "server release must contain exactly backend and frontend images"
        )
    expected_subjects = set()
    for role, approved_name in APPROVED_IMAGES.items():
        image = images.get(role)
        if not isinstance(image, dict):
            raise ServerReleaseArtifactError(f"{role} server image is invalid")
        name = str(image.get("name") or "")
        digest = str(image.get("digest") or "")
        reference = str(image.get("reference") or "")
        if name != approved_name:
            raise ServerReleaseArtifactError(f"{role} server image name is not approved")
        if not DIGEST_RE.fullmatch(digest):
            raise ServerReleaseArtifactError(f"{role} server image digest is invalid")
        if reference != f"{name}@{digest}":
            raise ServerReleaseArtifactError(f"{role} server image reference is not immutable")
        expected_subjects.add(("oci", role, reference, digest))
    return expected_subjects


def _validate_attestation(manifest: dict[str, Any], expected_subjects: set[tuple[str, str, str, str]]) -> None:
    attestation = manifest.get("attestation")
    if (
        not isinstance(attestation, dict)
        or attestation.get("registry_attestation_verified") is not True
    ):
        raise ServerReleaseArtifactError("server registry attestations are not verified")
    subjects = manifest.get("attestation_subjects")
    if not isinstance(subjects, list) or len(subjects) != len(expected_subjects):
        raise ServerReleaseArtifactError("server attestation subjects are incomplete")
    actual_subjects = set()
    for item in subjects:
        if not isinstance(item, dict):
            raise ServerReleaseArtifactError("server attestation subject is invalid")
        actual_subjects.add(
            (
                str(item.get("kind") or ""),
                str(item.get("role") or ""),
                str(item.get("name") or ""),
                str(item.get("digest") or ""),
            )
        )
    if actual_subjects != expected_subjects:
        raise ServerReleaseArtifactError(
            "server attestation subjects differ from immutable image references"
        )


def read_alembic_head(root: Path = ROOT) -> str:
    revisions = set()
    parents = set()
    migrations = root / "backend" / "migrations" / "versions"
    for path in sorted(migrations.glob("*.py")):
        try:
            module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            raise ServerReleaseArtifactError(
                f"Alembic revision cannot be read: {path.name}"
            ) from exc
        values = {}
        for node in module.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name) or target.id not in {"revision", "down_revision"}:
                continue
            try:
                values[target.id] = ast.literal_eval(node.value)
            except (ValueError, TypeError) as exc:
                raise ServerReleaseArtifactError(
                    f"Alembic revision metadata is not literal: {path.name}"
                ) from exc
        revision = values.get("revision")
        if not isinstance(revision, str) or not revision:
            raise ServerReleaseArtifactError(f"Alembic revision is missing: {path.name}")
        revisions.add(revision)
        down_revision = values.get("down_revision")
        if isinstance(down_revision, str) and down_revision:
            parents.add(down_revision)
        elif isinstance(down_revision, (tuple, list)):
            if any(not isinstance(value, str) or not value for value in down_revision):
                raise ServerReleaseArtifactError(
                    f"Alembic down_revision is invalid: {path.name}"
                )
            parents.update(down_revision)
        elif down_revision is not None:
            raise ServerReleaseArtifactError(f"Alembic down_revision is invalid: {path.name}")
    heads = revisions - parents
    if len(heads) != 1:
        raise ServerReleaseArtifactError("repository must contain exactly one Alembic head")
    return next(iter(heads))


def validate_manifest_shape(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 1:
        raise ServerReleaseArtifactError("server release schema_version must be 1")
    if manifest.get("release_kind") != "server":
        raise ServerReleaseArtifactError("release_kind must be server")
    if manifest.get("authority") != "github-sigstore" or manifest.get("deployable") is not True:
        raise ServerReleaseArtifactError(
            "server release requires deployable GitHub Sigstore authority"
        )
    if "windows" in manifest:
        raise ServerReleaseArtifactError("server release must not contain Windows artifacts")

    capabilities = manifest.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or any(not isinstance(value, str) or not value for value in capabilities)
        or len(capabilities) != len(set(capabilities))
        or not REQUIRED_CAPABILITIES.issubset(capabilities)
    ):
        raise ServerReleaseArtifactError("server release capabilities are incomplete or invalid")

    source_sha = str(manifest.get("source_sha") or "")
    if not SHA_RE.fullmatch(source_sha):
        raise ServerReleaseArtifactError("server release source_sha is invalid")
    if manifest.get("server_release_id") != f"server-{source_sha}":
        raise ServerReleaseArtifactError("server release id is not bound to source_sha")
    _validate_ci_identity(manifest.get("ci"), source_sha)
    expected_subjects = _validate_images(manifest.get("images"))

    compatibility = manifest.get("compatibility")
    if not isinstance(compatibility, dict):
        raise ServerReleaseArtifactError("server desktop compatibility contract is missing")
    if compatibility.get("desktop_api_contract") != DESKTOP_API_CONTRACT:
        raise ServerReleaseArtifactError("server desktop API contract is incompatible")
    if compatibility.get("min_desktop_version") != MIN_DESKTOP_VERSION:
        raise ServerReleaseArtifactError("server minimum desktop version is incompatible")

    database = manifest.get("database")
    if not isinstance(database, dict):
        raise ServerReleaseArtifactError("server database policy is missing")
    if (
        database.get("migration_policy") != MIGRATION_POLICY
        or not ALEMBIC_REVISION_RE.fullmatch(str(database.get("alembic_head") or ""))
        or database.get("destructive_migrations_allowed") is not False
        or database.get("alembic_downgrade_allowed") is not False
    ):
        raise ServerReleaseArtifactError(
            "server release requires no_change at a valid Alembic head"
        )
    _validate_attestation(manifest, expected_subjects)


def _current_git_sha() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    value = completed.stdout.strip().lower()
    if completed.returncode != 0 or not SHA_RE.fullmatch(value):
        raise ServerReleaseArtifactError("current git source SHA cannot be verified")
    return value


def verify_manifest(
    path: Path,
    *,
    expected_sha: str | None = None,
    current_sha: str | None = None,
    candidate: bool = False,
) -> dict[str, Any]:
    manifest = _load_manifest(path)
    validate_manifest_shape(manifest)
    if candidate and expected_sha is None:
        expected_sha = manifest["source_sha"]
    if expected_sha is None:
        if current_sha is not None:
            raise ServerReleaseArtifactError("current_sha requires expected_sha")
        return manifest
    if not SHA_RE.fullmatch(expected_sha):
        raise ServerReleaseArtifactError("expected source SHA must be exactly 40 lowercase hex")
    checked_current_sha = current_sha if current_sha is not None else _current_git_sha()
    if not SHA_RE.fullmatch(checked_current_sha):
        raise ServerReleaseArtifactError("current source SHA is invalid")
    if checked_current_sha != expected_sha:
        raise ServerReleaseArtifactError("expected source SHA differs from current git commit")
    if manifest["source_sha"] != expected_sha:
        raise ServerReleaseArtifactError("server manifest source SHA differs from expected source SHA")
    if manifest["database"]["alembic_head"] != read_alembic_head():
        raise ServerReleaseArtifactError(
            "candidate server release Alembic head differs from the repository"
        )
    return manifest


def emit_shell(manifest: dict[str, Any]) -> None:
    validate_manifest_shape(manifest)
    images = manifest["images"]
    compatibility = manifest["compatibility"]
    values = {
        "RELEASE_KIND": manifest["release_kind"],
        "RELEASE_SOURCE_SHA": manifest["source_sha"],
        "RELEASE_SERVER_RELEASE_ID": manifest["server_release_id"],
        "RELEASE_BACKEND_IMAGE": images["backend"]["reference"],
        "RELEASE_FRONTEND_IMAGE": images["frontend"]["reference"],
        "RELEASE_BACKEND_DIGEST": images["backend"]["digest"],
        "RELEASE_FRONTEND_DIGEST": images["frontend"]["digest"],
        "RELEASE_DESKTOP_API_CONTRACT": compatibility["desktop_api_contract"],
        "RELEASE_MIN_DESKTOP_VERSION": compatibility["min_desktop_version"],
        "RELEASE_DATABASE_MIGRATION_POLICY": manifest["database"]["migration_policy"],
        "RELEASE_ALEMBIC_HEAD": manifest["database"]["alembic_head"],
    }
    for key, value in values.items():
        print(f"{key}={shlex.quote(str(value))}")


def print_plan(manifest: dict[str, Any]) -> None:
    validate_manifest_shape(manifest)
    print(
        "SERVER_DEPLOY_DRY_RUN "
        f"source_sha={manifest['source_sha']} "
        f"server_release_id={manifest['server_release_id']} "
        f"backend={manifest['images']['backend']['reference']} "
        f"frontend={manifest['images']['frontend']['reference']} "
        f"desktop_api_contract={manifest['compatibility']['desktop_api_contract']} "
        f"database_migration_policy={manifest['database']['migration_policy']} "
        f"alembic_head={manifest['database']['alembic_head']} "
        "windows_artifacts=0 version_json_mutations=0 production_mutations=0"
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--sha")
    verify.add_argument("--candidate", action="store_true")
    emit = subparsers.add_parser("emit-shell")
    emit.add_argument("--manifest", type=Path, required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--manifest", type=Path, required=True)
    plan.add_argument("--local", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.command == "plan" and not args.local:
            raise ServerReleaseArtifactError("server deployment plan requires --local no-mutation mode")
        manifest = verify_manifest(
            args.manifest.resolve(),
            expected_sha=args.sha if args.command == "verify" else None,
            candidate=args.candidate if args.command == "verify" else False,
        )
        if args.command == "verify":
            print(
                "SERVER_RELEASE_OK "
                f"source_sha={manifest['source_sha']} "
                f"server_release_id={manifest['server_release_id']} "
                "desktop_api_contract=1 migration_policy=no_change"
            )
        elif args.command == "emit-shell":
            emit_shell(manifest)
        else:
            print_plan(manifest)
    except (ServerReleaseArtifactError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"SERVER_RELEASE_ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
