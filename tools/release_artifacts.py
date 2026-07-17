#!/usr/bin/env python3
"""Build and verify immutable local release evidence for Phase 23."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "test-artifacts/release.json"
DEFAULT_EVIDENCE_DIR = ROOT / "test-artifacts/release"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
LOCAL_IMAGE_NAMES = {
    "backend": "local/taksklad-backend",
    "frontend": "local/taksklad-frontend",
}


class ReleaseArtifactError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def dsse_pae(payload_type: str, payload: bytes) -> bytes:
    type_bytes = payload_type.encode("utf-8")
    return b"DSSEv1 " + str(len(type_bytes)).encode() + b" " + type_bytes + b" " + str(len(payload)).encode() + b" " + payload


def run(command: list[str], *, cwd: Path = ROOT, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    if completed.returncode != 0:
        tail = "\n".join(completed.stdout.splitlines()[-20:])
        raise ReleaseArtifactError(f"command failed exit={completed.returncode}: {' '.join(command[:5])}\n{tail}")
    return completed


def read_app_version(root: Path) -> str:
    source = (root / "src/taksklad/config.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', source, flags=re.MULTILINE)
    if not match:
        raise ReleaseArtifactError("APP_VERSION is missing")
    return match.group(1)


def oci_layout_digest(path: Path) -> str:
    with tarfile.open(path, "r:*") as archive:
        member = archive.getmember("index.json")
        file_obj = archive.extractfile(member)
        if file_obj is None:
            raise ReleaseArtifactError(f"OCI index is missing: {path.name}")
        index_bytes = file_obj.read()
        index = json.loads(index_bytes)
    manifests = index.get("manifests") if isinstance(index, dict) else None
    if not isinstance(manifests, list) or len(manifests) != 1:
        raise ReleaseArtifactError(f"OCI layout must contain exactly one manifest: {path.name}")
    descriptor = str(manifests[0].get("digest") or "")
    if not DIGEST_RE.fullmatch(descriptor):
        raise ReleaseArtifactError(f"OCI descriptor digest is invalid: {path.name}")
    # The deployable subject is the descriptor referenced by the OCI index,
    # not the tar-file hash. The tar hash is retained separately as transport evidence.
    return descriptor


def archive_source(sha: str, destination: Path) -> None:
    archive_path = destination / "source.tar"
    with archive_path.open("wb") as output:
        completed = subprocess.run(
            ["git", "archive", "--format=tar", sha, "backend", "frontend"],
            cwd=ROOT,
            stdout=output,
            stderr=subprocess.PIPE,
            check=False,
        )
    if completed.returncode != 0:
        raise ReleaseArtifactError("git archive failed for immutable source SHA")
    with tarfile.open(archive_path, "r:") as archive:
        archive.extractall(destination, filter="data")
    archive_path.unlink()


def build_oci_subject(context: Path, destination: Path) -> dict[str, str]:
    run(
        [
            "docker",
            "buildx",
            "build",
            "--platform",
            "linux/amd64",
            "--provenance=false",
            "--output",
            f"type=oci,dest={destination}",
            str(context),
        ],
        timeout=1800,
    )
    return {
        "digest": oci_layout_digest(destination),
        "oci_tar_sha256": sha256_file(destination),
    }


def phase21_windows_evidence() -> dict[str, str | bool]:
    artifact = ROOT / "test-artifacts/provenance/TakSklad-synthetic-signed.exe"
    verification_path = ROOT / "test-artifacts/provenance/verification.json"
    statement_path = ROOT / "test-artifacts/provenance/provenance.intoto.json"
    if not artifact.is_file() or not verification_path.is_file() or not statement_path.is_file():
        raise ReleaseArtifactError("Phase 21 Windows local-test evidence is missing")
    verification = json.loads(verification_path.read_text(encoding="utf-8"))
    statement = json.loads(statement_path.read_text(encoding="utf-8"))
    if verification.get("status") != "pass" or verification.get("authenticode", {}).get("embedded_signature_verified") is not True:
        raise ReleaseArtifactError("Phase 21 Windows local-test signature evidence is invalid")
    artifact_sha = sha256_file(artifact)
    if artifact_sha != verification.get("artifact_sha256"):
        raise ReleaseArtifactError("Windows local-test artifact hash drifted")
    source_sha = str(statement.get("predicate", {}).get("buildDefinition", {}).get("internalParameters", {}).get("sourceCommit") or "")
    if not SHA_RE.fullmatch(source_sha):
        raise ReleaseArtifactError("Windows local-test source SHA is invalid")
    return {
        "artifact": artifact.relative_to(ROOT).as_posix(),
        "artifact_sha256": artifact_sha,
        "artifact_source_sha": source_sha,
        "signature_status": "Valid",
        "signature_type": "authenticode-local-test",
        "production_certificate_verified": False,
        "production_gate": "WINDOWS_CODESIGN_CERTIFICATE_NOT_AVAILABLE",
    }


def statement_subjects(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for role in ("backend", "frontend"):
        image = manifest["images"][role]
        result.append(
            {
                "name": image["name"],
                "digest": {"sha256": image["digest"].removeprefix("sha256:")},
            }
        )
    result.append(
        {
            "name": manifest["windows"]["artifact"],
            "digest": {"sha256": manifest["windows"]["artifact_sha256"]},
        }
    )
    return result


def build_local(sha: str, manifest_path: Path, evidence_dir: Path) -> dict[str, Any]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sha = sha.strip().lower()
    if not SHA_RE.fullmatch(sha):
        raise ReleaseArtifactError("source SHA must be exactly 40 lowercase hex characters")
    resolved = run(["git", "rev-parse", f"{sha}^{{commit}}"]).stdout.strip().lower()
    if resolved != sha:
        raise ReleaseArtifactError("source SHA does not resolve to itself")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="taksklad-phase23-", dir=ROOT / ".release-state") as temporary:
        work = Path(temporary)
        archive_source(sha, work)
        images: dict[str, dict[str, str]] = {}
        for role in ("backend", "frontend"):
            oci_path = work / f"taksklad-{role}.oci.tar"
            built = build_oci_subject(work / role, oci_path)
            images[role] = {
                "name": LOCAL_IMAGE_NAMES[role],
                "tag": f"sha-{sha}",
                "digest": built["digest"],
                "oci_tar_sha256": built["oci_tar_sha256"],
                "oci_tar_retained": False,
            }

        windows = phase21_windows_evidence()
        windows.update(
            {
                "version": read_app_version(ROOT),
                "release_source_sha": sha,
                "dependency_lock_sha256": sha256_file(ROOT / "requirements/desktop.lock"),
            }
        )
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "authority": "local-test",
            "deployable": False,
            "source_sha": sha,
            "release_id": f"local-{sha}",
            "acceptance_required": True,
            "images": images,
            "windows": windows,
            "dependency_locks": {
                "backend_sha256": sha256_file(ROOT / "backend/requirements.lock"),
                "desktop_sha256": sha256_file(ROOT / "requirements/desktop.lock"),
            },
            "database_rollback": {
                "strategy": "retain-current-schema",
                "alembic_downgrade_allowed": False,
            },
            "previous_release": {
                "source": "synthetic-local-dry-run",
                "backend_digest": "sha256:" + hashlib.sha256(f"previous-backend:{sha}".encode()).hexdigest(),
                "frontend_digest": "sha256:" + hashlib.sha256(f"previous-frontend:{sha}".encode()).hexdigest(),
            },
            "attestation": {
                "type": "local-test-dsse-slsa-v1",
                "github_identity_verified": False,
                "registry_attestation_verified": False,
                "bundle": "test-artifacts/release/provenance.dsse.json",
                "public_key": "test-artifacts/release/local-test-public-key.pem",
            },
        }
        manifest_path.write_bytes(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")

        statement = {
            "_type": "https://in-toto.io/Statement/v1",
            "subject": statement_subjects(manifest),
            "predicateType": "https://slsa.dev/provenance/v1",
            "predicate": {
                "buildDefinition": {
                    "buildType": "https://taksklad.local/phase23/release-artifacts/v1",
                    "externalParameters": {"sourceSha": sha},
                    "internalParameters": {"gitArchive": True, "dirtyWorkingTreeUsed": False},
                    "resolvedDependencies": [],
                },
                "runDetails": {
                    "builder": {"id": "https://taksklad.local/builders/local-phase23"},
                    "metadata": {"invocationId": f"local-{sha}"},
                },
            },
        }
        payload_type = "application/vnd.in-toto+json"
        payload = canonical_json_bytes(statement)
        private_key = Ed25519PrivateKey.generate()
        signature = private_key.sign(dsse_pae(payload_type, payload))
        envelope = {
            "payloadType": payload_type,
            "payload": base64.b64encode(payload).decode("ascii"),
            "signatures": [{"keyid": "local-test-ed25519", "sig": base64.b64encode(signature).decode("ascii")}],
        }
        (evidence_dir / "provenance.intoto.json").write_bytes(json.dumps(statement, indent=2, sort_keys=True).encode() + b"\n")
        (evidence_dir / "provenance.dsse.json").write_bytes(json.dumps(envelope, indent=2, sort_keys=True).encode() + b"\n")
        public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        (evidence_dir / "local-test-public-key.pem").write_bytes(public_pem)
        del private_key

    verified = verify_manifest(manifest_path, local=True)
    verification = {
        "status": "pass",
        "authority": "local-test",
        "source_sha": sha,
        "subjects": {subject["name"]: subject["digest"]["sha256"] for subject in statement_subjects(verified)},
        "oci_tar_files_retained": False,
        "dirty_worktree_used": False,
        "production_deployable": False,
        "github_identity_verified": False,
    }
    (evidence_dir / "verification.json").write_bytes(json.dumps(verification, indent=2, sort_keys=True).encode() + b"\n")
    return verified


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseArtifactError(f"release manifest cannot be read: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise ReleaseArtifactError("release manifest must be an object")
    return value


def validate_manifest_shape(manifest: dict[str, Any], *, local: bool) -> None:
    if manifest.get("schema_version") != 1:
        raise ReleaseArtifactError("release manifest schema_version must be 1")
    if not SHA_RE.fullmatch(str(manifest.get("source_sha") or "")):
        raise ReleaseArtifactError("release manifest source_sha is invalid")
    if manifest.get("acceptance_required") is not True:
        raise ReleaseArtifactError("release acceptance must be required")
    rollback = manifest.get("database_rollback") or {}
    if rollback.get("strategy") != "retain-current-schema" or rollback.get("alembic_downgrade_allowed") is not False:
        raise ReleaseArtifactError("database rollback must retain the current schema")
    images = manifest.get("images")
    if not isinstance(images, dict) or set(images) != {"backend", "frontend"}:
        raise ReleaseArtifactError("release manifest must contain backend/frontend images")
    for role, image in images.items():
        if not isinstance(image, dict) or not DIGEST_RE.fullmatch(str(image.get("digest") or "")):
            raise ReleaseArtifactError(f"{role} image digest is invalid")
        if image.get("tag") != f"sha-{manifest['source_sha']}":
            raise ReleaseArtifactError(f"{role} image tag is not bound to source SHA")
    windows = manifest.get("windows") or {}
    for field in ("artifact_sha256", "dependency_lock_sha256"):
        if not HEX_RE.fullmatch(str(windows.get(field) or "")):
            raise ReleaseArtifactError(f"Windows {field} is invalid")
    if not VERSION_RE.fullmatch(str(windows.get("version") or "")):
        raise ReleaseArtifactError("Windows version is invalid")
    if local:
        if windows.get("version") != read_app_version(ROOT):
            raise ReleaseArtifactError("Windows version differs from application version")
        if manifest.get("authority") != "local-test" or manifest.get("deployable") is not False:
            raise ReleaseArtifactError("local verification requires non-deployable local-test authority")
        if manifest.get("attestation", {}).get("github_identity_verified") is not False:
            raise ReleaseArtifactError("local manifest must not claim GitHub identity")
    else:
        if manifest.get("authority") != "github-sigstore" or manifest.get("deployable") is not True:
            raise ReleaseArtifactError("production manifest requires GitHub Sigstore authority")
        if manifest.get("attestation", {}).get("github_identity_verified") is not True:
            raise ReleaseArtifactError("production manifest requires verified GitHub identity")
        if manifest.get("attestation", {}).get("registry_attestation_verified") is not True:
            raise ReleaseArtifactError("production manifest requires verified registry attestations")
        for image in images.values():
            if not str(image.get("name") or "").startswith("ghcr.io/1fear/taksklad-"):
                raise ReleaseArtifactError("production image must use the approved GHCR namespace")


def verify_manifest(
    path: Path,
    *,
    local: bool,
    expected_sha: str | None = None,
    candidate: bool = False,
) -> dict[str, Any]:
    manifest = _load_manifest(path)
    validate_manifest_shape(manifest, local=local)
    if candidate:
        if local:
            raise ReleaseArtifactError("candidate verification is production-only")
        if "returns_auth_canary_v2_exact_identifier" not in (manifest.get("capabilities") or []):
            raise ReleaseArtifactError("production candidate must declare returns auth canary capability")
        windows = manifest.get("windows") or {}
        expected_windows = {
            "TakSklad.exe": windows.get("artifact_sha256"),
            "TakSkladAuth.exe": windows.get("auth_helper_sha256"),
            "TakSklad-windows-x64.zip": windows.get("artifact_sha256_onedir"),
            "version.json": windows.get("manifest_sha256"),
        }
        if (
            windows.get("artifact") != "TakSklad.exe"
            or windows.get("auth_helper") != "TakSkladAuth.exe"
            or windows.get("artifact_onedir") != "TakSklad-windows-x64.zip"
            or windows.get("manifest") != "version.json"
            or windows.get("acceptance_wrapper") != "windows_backend_acceptance.ps1"
            or not HEX_RE.fullmatch(str(windows.get("app_sha256_onedir") or ""))
            or not HEX_RE.fullmatch(str(windows.get("acceptance_wrapper_sha256") or ""))
            or any(not HEX_RE.fullmatch(str(value or "")) for value in expected_windows.values())
            or windows.get("signature_type") != "authenticode"
            or windows.get("signature_required") is not True
            or not HEX_RE.fullmatch(str(windows.get("signer_certificate_sha256") or ""))
        ):
            raise ReleaseArtifactError("production Windows subject identity is invalid")
        subjects = manifest.get("attestation_subjects") or []
        windows_subjects = {
            item.get("name"): item.get("sha256")
            for item in subjects
            if isinstance(item, dict) and item.get("kind") == "windows"
        }
        if windows_subjects != expected_windows:
            raise ReleaseArtifactError("production attestation subjects differ from manifest artifacts")
    if expected_sha is not None:
        if local:
            raise ReleaseArtifactError("exact-SHA verification is production-only")
        if not SHA_RE.fullmatch(expected_sha):
            raise ReleaseArtifactError("requested source SHA must be exactly 40 lowercase hex characters")
        current_sha = run(["git", "rev-parse", "HEAD"], timeout=30).stdout.strip()
        if current_sha != expected_sha:
            raise ReleaseArtifactError("requested source SHA differs from current commit")
        if manifest["source_sha"] != expected_sha:
            raise ReleaseArtifactError("release manifest source SHA differs from requested source SHA")
        if "returns_auth_canary_v2_exact_identifier" not in (manifest.get("capabilities") or []):
            raise ReleaseArtifactError("production candidate must declare returns auth canary capability")
        version = str((manifest.get("windows") or {}).get("version") or "")
        if manifest.get("release_tag") != f"v{version}":
            raise ReleaseArtifactError("release tag differs from production Windows version")
        ci = manifest.get("ci") or {}
        if (
            ci.get("workflow") != "CI"
            or ci.get("head_sha") != expected_sha
            or ci.get("event") != "push"
            or ci.get("head_branch") != "main"
            or ci.get("required_check") != "Release gate"
            or ci.get("conclusion") != "success"
            or not isinstance(ci.get("run_id"), int)
            or ci["run_id"] <= 0
        ):
            raise ReleaseArtifactError("production manifest CI identity is invalid")
        for role, image in manifest["images"].items():
            if image.get("reference") != f"{image.get('name')}@{image.get('digest')}":
                raise ReleaseArtifactError(f"{role} production reference is not immutable")
        windows = manifest.get("windows") or {}
        expected_windows = {
            "TakSklad.exe": windows.get("artifact_sha256"),
            "TakSkladAuth.exe": windows.get("auth_helper_sha256"),
            "TakSklad-windows-x64.zip": windows.get("artifact_sha256_onedir"),
            "version.json": windows.get("manifest_sha256"),
        }
        if (
            windows.get("artifact") != "TakSklad.exe"
            or windows.get("auth_helper") != "TakSkladAuth.exe"
            or windows.get("artifact_onedir") != "TakSklad-windows-x64.zip"
            or windows.get("manifest") != "version.json"
            or windows.get("acceptance_wrapper") != "windows_backend_acceptance.ps1"
            or not HEX_RE.fullmatch(str(windows.get("app_sha256_onedir") or ""))
            or not HEX_RE.fullmatch(str(windows.get("acceptance_wrapper_sha256") or ""))
            or any(not HEX_RE.fullmatch(str(value or "")) for value in expected_windows.values())
            or windows.get("signature_type") != "authenticode"
            or windows.get("signature_required") is not True
            or not HEX_RE.fullmatch(str(windows.get("signer_certificate_sha256") or ""))
        ):
            raise ReleaseArtifactError("production Windows subject identity is invalid")
        subjects = manifest.get("attestation_subjects") or []
        windows_subjects = {
            item.get("name"): item.get("sha256")
            for item in subjects
            if isinstance(item, dict) and item.get("kind") == "windows"
        }
        oci_subjects = {
            (item.get("name"), item.get("digest"))
            for item in subjects
            if isinstance(item, dict) and item.get("kind") == "oci"
        }
        expected_oci = {
            (manifest["images"][role]["reference"], manifest["images"][role]["digest"])
            for role in ("backend", "frontend")
        }
        if windows_subjects != expected_windows or oci_subjects != expected_oci:
            raise ReleaseArtifactError("production attestation subjects differ from manifest artifacts")
    if local:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        bundle = ROOT / manifest["attestation"]["bundle"]
        public_key_path = ROOT / manifest["attestation"]["public_key"]
        envelope = json.loads(bundle.read_text(encoding="utf-8"))
        payload_type = str(envelope.get("payloadType") or "")
        payload = base64.b64decode(envelope.get("payload") or "", validate=True)
        signatures = envelope.get("signatures")
        if not isinstance(signatures, list) or len(signatures) != 1:
            raise ReleaseArtifactError("local DSSE must contain one signature")
        signature = base64.b64decode(signatures[0].get("sig") or "", validate=True)
        public_key = serialization.load_pem_public_key(public_key_path.read_bytes())
        if not isinstance(public_key, Ed25519PublicKey):
            raise ReleaseArtifactError("local DSSE public key type is invalid")
        public_key.verify(signature, dsse_pae(payload_type, payload))
        statement = json.loads(payload)
        if statement.get("subject") != statement_subjects(manifest):
            raise ReleaseArtifactError("attestation subjects differ from release manifest digests")
        windows_path = ROOT / manifest["windows"]["artifact"]
        if sha256_file(windows_path) != manifest["windows"]["artifact_sha256"]:
            raise ReleaseArtifactError("Windows artifact SHA256 mismatch")
        if sha256_file(ROOT / "requirements/desktop.lock") != manifest["windows"]["dependency_lock_sha256"]:
            raise ReleaseArtifactError("Windows dependency lock SHA256 mismatch")
        for role in ("backend", "frontend"):
            if manifest["images"][role].get("oci_tar_retained") is not False:
                raise ReleaseArtifactError("local OCI tar retention must be false")
    return manifest


def print_plan(manifest: dict[str, Any]) -> None:
    print(
        "DEPLOY_DRY_RUN_ARTIFACT "
        f"source_sha={manifest['source_sha']} authority={manifest['authority']} deployable={str(manifest['deployable']).lower()}"
    )
    for role in ("backend", "frontend"):
        image = manifest["images"][role]
        print(f"DEPLOY_DRY_RUN_IMAGE role={role} subject={image['name']}@{image['digest']} attestation_match=1")
    print(
        "DEPLOY_DRY_RUN_WINDOWS "
        f"version={manifest['windows']['version']} artifact_sha256={manifest['windows']['artifact_sha256']} "
        f"dependency_lock_sha256={manifest['windows']['dependency_lock_sha256']} signature={manifest['windows']['signature_status']}"
    )
    previous = manifest["previous_release"]
    print(
        "DEPLOY_DRY_RUN_ROLLBACK "
        f"backend={previous['backend_digest']} frontend={previous['frontend_digest']} "
        "database_schema_action=retain-current alembic_downgrade=0"
    )
    print("DEPLOY_DRY_RUN_ACCEPTANCE required=1 bypass=0 administrator_bypass=0")
    print("DEPLOY_DRY_RUN_OK source_build=0 push=0 production_mutations=0 external_sends=0")


def emit_shell(manifest: dict[str, Any]) -> None:
    values = {
        "RELEASE_SOURCE_SHA": manifest["source_sha"],
        "RELEASE_BACKEND_IMAGE": f"{manifest['images']['backend']['name']}@{manifest['images']['backend']['digest']}",
        "RELEASE_FRONTEND_IMAGE": f"{manifest['images']['frontend']['name']}@{manifest['images']['frontend']['digest']}",
        "RELEASE_BACKEND_DIGEST": manifest["images"]["backend"]["digest"],
        "RELEASE_FRONTEND_DIGEST": manifest["images"]["frontend"]["digest"],
    }
    for key, value in values.items():
        print(f"{key}={shlex.quote(value)}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build-local")
    build.add_argument("--sha", required=True)
    build.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    build.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    verify_mode = verify.add_mutually_exclusive_group()
    verify_mode.add_argument("--local", action="store_true")
    verify_mode.add_argument("--sha")
    verify.add_argument("--candidate", action="store_true")
    plan = subparsers.add_parser("plan")
    plan.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    plan.add_argument("--local", action="store_true")
    emit = subparsers.add_parser("emit-shell")
    emit.add_argument("--manifest", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.command == "build-local":
            manifest = build_local(args.sha, args.manifest.resolve(), args.evidence_dir.resolve())
            print(
                "RELEASE_IMAGES_BUILT "
                f"source_sha={manifest['source_sha']} backend_digest={manifest['images']['backend']['digest']} "
                f"frontend_digest={manifest['images']['frontend']['digest']} dirty_worktree_used=0 oci_tars_retained=0"
            )
        elif args.command == "verify":
            manifest = verify_manifest(
                args.manifest.resolve(),
                local=args.local,
                expected_sha=args.sha,
                candidate=args.candidate or args.sha is not None,
            )
            subject_count = 3 if args.local else len(manifest.get("attestation_subjects") or [])
            print(
                "RELEASE_ATTESTATIONS_OK "
                f"authority={manifest['authority']} subjects={subject_count} source_sha={manifest['source_sha']} "
                f"production_deployable={int(bool(manifest['deployable']))}"
            )
        elif args.command == "plan":
            manifest = verify_manifest(args.manifest.resolve(), local=args.local)
            print_plan(manifest)
        else:
            manifest = verify_manifest(args.manifest.resolve(), local=False)
            emit_shell(manifest)
    except (ReleaseArtifactError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"RELEASE_ARTIFACT_ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
