#!/usr/bin/env python3
"""Create and verify synthetic Windows release provenance without production trust.

The generated evidence is deliberately scoped to ``local-test``.  It proves
that the release controls can bind an Authenticode-signed PE to an in-toto
subject digest.  It does not claim a GitHub Actions identity or a production
code-signing certificate.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


TOOLCHAIN_IMAGE = (
    "debian@sha256:28de0877c2189802884ccd20f15ee41c203573bd87bb6b883f5f46362d24c5c2"
)
TOOLCHAIN_PACKAGES = (
    "ca-certificates=20250419",
    "gcc-mingw-w64-x86-64=14.2.0-17+27",
    "openssl=3.5.6-1~deb13u2",
    "osslsigncode=2.9-2",
)
OCI_PLATFORM = "linux/amd64"
BACKEND_CONTEXT_ENTRIES = (
    "Dockerfile",
    "requirements.lock",
    "alembic.ini",
    "app",
    "migrations",
    "sql",
)
FRONTEND_CONTEXT_ENTRIES = (
    "Dockerfile",
    "package.json",
    "package-lock.json",
    "index.html",
    "nginx.conf.template",
    "tsconfig.json",
    "vite.config.ts",
    "src",
    "public",
)
FORBIDDEN_CONTEXT_PARTS = {
    "__pycache__",
    "backups",
    "coverage",
    "credentials.json",
    "dist",
    "node_modules",
    "outputs",
    "reports",
    "test-results",
}
AUTHORITY = "local-test"
GITHUB_IDENTITY_STATUS = "GITHUB_IDENTITY_ATTESTATION_NOT_AVAILABLE"
PRODUCTION_CERTIFICATE_STATUS = "WINDOWS_CODESIGN_CERTIFICATE_NOT_AVAILABLE"
DSSE_PAYLOAD_TYPE = "application/vnd.in-toto+json"

TINY_PE_SOURCE = """\
int main(void) {
    return 0;
}
"""

LEAF_EXTENSIONS = """\
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=codeSigning
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
"""

TOOLCHAIN_COMMAND = r"""
export DEBIAN_FRONTEND=noninteractive
apt-get update >/dev/null
apt-get install -y --no-install-recommends \
  ca-certificates=20250419 \
  gcc-mingw-w64-x86-64=14.2.0-17+27 \
  openssl=3.5.6-1~deb13u2 \
  osslsigncode=2.9-2 >/dev/null

dpkg-query -W -f='${Package}=${Version}\n' | LC_ALL=C sort > /work/toolchain-packages.txt
x86_64-w64-mingw32-gcc \
  -Os -s -Wl,--no-insert-timestamp \
  -o /work/TakSklad-synthetic-unsigned.exe /work/tiny.c

openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 3650 -set_serial 1 \
  -subj '/CN=TakSklad Local Test Root/' \
  -addext 'basicConstraints=critical,CA:TRUE' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -keyout /work/root-key.pem -out /work/root-certificate.pem >/dev/null 2>&1
openssl req -newkey rsa:3072 -nodes -sha256 \
  -subj '/CN=TakSklad Local Test Artifact/' \
  -keyout /work/leaf-key.pem -out /work/leaf.csr.pem >/dev/null 2>&1
openssl x509 -req -sha256 -days 3650 -set_serial 2 \
  -in /work/leaf.csr.pem \
  -CA /work/root-certificate.pem -CAkey /work/root-key.pem \
  -extfile /work/leaf-extensions.cnf \
  -out /work/leaf-certificate.pem >/dev/null 2>&1
openssl pkey -in /work/leaf-key.pem -pubout -out /work/local-test-public-key.pem
osslsigncode sign \
  -certs /work/leaf-certificate.pem \
  -key /work/leaf-key.pem \
  -h sha256 \
  -n 'TakSklad Local Test Artifact' \
  -i 'https://example.invalid/taksklad-local-test' \
  -in /work/TakSklad-synthetic-unsigned.exe \
  -out /work/TakSklad-synthetic-signed.exe >/work/authenticode-sign.log 2>&1
osslsigncode verify \
  -CAfile /work/root-certificate.pem \
  -in /work/TakSklad-synthetic-signed.exe >/work/authenticode-verify.log 2>&1

cp /work/TakSklad-synthetic-signed.exe /work/TakSklad-synthetic-tampered.exe
printf '\377' | dd \
  of=/work/TakSklad-synthetic-tampered.exe \
  bs=1 seek=512 count=1 conv=notrunc status=none
if osslsigncode verify \
  -CAfile /work/root-certificate.pem \
  -in /work/TakSklad-synthetic-tampered.exe >/work/authenticode-tamper.log 2>&1; then
  echo 'tampered Authenticode artifact unexpectedly verified' >&2
  exit 1
fi

chown -R "${HOST_UID}:${HOST_GID}" /work
"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_commit(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def build_statement(
    *,
    subjects: dict[str, str],
    source_commit: str,
    candidate_inputs: dict[str, str],
) -> dict:
    if not subjects:
        raise ValueError("at least one provenance subject is required")
    normalized_subjects = [
        {"name": name, "digest": {"sha256": digest}}
        for name, digest in sorted(subjects.items())
    ]
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": normalized_subjects,
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://taksklad.local/rehearsal/authenticode/v1",
                "externalParameters": {
                    "authority": AUTHORITY,
                    "production": False,
                },
                "internalParameters": {
                    "sourceCommit": source_commit,
                    "dirtyCandidate": True,
                    "candidateInputSha256": dict(sorted(candidate_inputs.items())),
                },
                "resolvedDependencies": [
                    {"uri": f"pkg:oci/debian@{TOOLCHAIN_IMAGE.split('@', 1)[1]}"},
                ],
            },
            "runDetails": {
                "builder": {"id": "local-test://taksklad/release-rehearsal"},
                "metadata": {
                    "invocationId": "local-test:sha256:"
                    + hashlib.sha256(
                        json.dumps(subjects, sort_keys=True).encode("utf-8")
                    ).hexdigest(),
                },
            },
        },
    }


def run_checked(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        diagnostic = "\n".join(
            (completed.stdout + "\n" + completed.stderr).strip().splitlines()[-20:]
        )
        raise RuntimeError(
            f"local provenance command failed with exit {completed.returncode}:\n{diagnostic}"
        )
    return completed


def assert_pe(path: Path) -> None:
    payload = path.read_bytes()
    if len(payload) < 1024 or payload[:2] != b"MZ" or b"PE\x00\x00" not in payload[:1024]:
        raise RuntimeError("synthetic Windows artifact is not a valid PE container")


def assert_toolchain_manifest(path: Path) -> None:
    installed = set(path.read_text(encoding="utf-8").splitlines())
    missing = sorted(set(TOOLCHAIN_PACKAGES) - installed)
    if missing:
        raise RuntimeError(f"pinned toolchain package missing at runtime: {', '.join(missing)}")


def copy_safe_context(
    source_root: Path,
    destination: Path,
    entries: tuple[str, ...],
    *,
    owned_overrides: dict[str, bytes | None] | None = None,
) -> int:
    owned_overrides = owned_overrides or {}
    destination.mkdir(parents=True, exist_ok=False)
    copied_files = 0
    for relative_name in entries:
        source = source_root / relative_name
        if not source.exists():
            if relative_name == "public":
                continue
            raise RuntimeError(f"required OCI context path is missing: {source}")
        if source.is_symlink():
            raise RuntimeError(f"symlink is forbidden in OCI build context: {source}")
        target = destination / relative_name
        if source.is_file():
            if relative_name in owned_overrides:
                content = owned_overrides[relative_name]
                if content is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                copied_files += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied_files += 1
            continue
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise RuntimeError(f"symlink is forbidden in OCI build context: {path}")
            if not path.is_file():
                continue
            relative_path = path.relative_to(source_root)
            if (
                any(
                    part in FORBIDDEN_CONTEXT_PARTS or part.startswith(".env")
                    for part in relative_path.parts
                )
                or path.suffix == ".pyc"
            ):
                continue
            target_path = destination / relative_path
            relative_text = relative_path.as_posix()
            if relative_text in owned_overrides:
                content = owned_overrides[relative_text]
                if content is None:
                    continue
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(content)
                copied_files += 1
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target_path)
            copied_files += 1
    return copied_files


def owned_context_overrides(repo_root: Path, context_name: str) -> dict[str, bytes | None]:
    manifest_path = repo_root / ".release-state" / "owned-tree-manifest.json"
    if not manifest_path.is_file():
        return {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    prefix = f"{context_name}/"
    overrides: dict[str, bytes | None] = {}
    for change in manifest.get("changes", []):
        repo_relative = str(change.get("path", ""))
        if not repo_relative.startswith(prefix):
            continue
        context_relative = repo_relative[len(prefix) :]
        completed = subprocess.run(
            ["git", "show", f"HEAD:{repo_relative}"],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
        overrides[context_relative] = completed.stdout if completed.returncode == 0 else None
    return overrides


def context_input_hash(context_dir: Path) -> str:
    manifest = []
    for path in sorted(context_dir.rglob("*")):
        if path.is_file():
            manifest.append(
                {
                    "path": path.relative_to(context_dir).as_posix(),
                    "sha256": sha256_file(path),
                }
            )
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_local_oci_subject(
    *,
    context_dir: Path,
    destination: Path,
    image_name: str,
) -> str:
    run_checked(
        [
            "docker",
            "buildx",
            "build",
            "--file",
            str(context_dir / "Dockerfile"),
            "--platform",
            OCI_PLATFORM,
            "--provenance=false",
            "--tag",
            f"local/taksklad-{image_name}:phase21-candidate",
            "--output",
            f"type=oci,dest={destination}",
            str(context_dir),
        ]
    )
    if not destination.is_file():
        raise RuntimeError(f"OCI output was not created: {destination}")
    return sha256_file(destination)


def write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


PUBLIC_EVIDENCE_NAMES = (
    "TakSklad-synthetic-signed.exe",
    "local-test-public-key.pem",
    "leaf-certificate.pem",
    "root-certificate.pem",
    "toolchain-packages.txt",
    "provenance.intoto.json",
    "provenance.dsse.json",
    "verification.json",
)


def copy_public_evidence(work_dir: Path, output_dir: Path) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in PUBLIC_EVIDENCE_NAMES:
        shutil.copyfile(work_dir / name, output_dir / name)
    return list(PUBLIC_EVIDENCE_NAMES)


def dsse_pae(payload_type: str, payload: bytes) -> bytes:
    type_bytes = payload_type.encode("utf-8")
    return b" ".join(
        (
            b"DSSEv1",
            str(len(type_bytes)).encode("ascii"),
            type_bytes,
            str(len(payload)).encode("ascii"),
            payload,
        )
    )


def create_local_dsse(
    statement_path: Path,
    private_key_path: Path,
    public_key_path: Path,
    bundle_path: Path,
) -> None:
    payload = statement_path.read_bytes()
    pae_path = statement_path.with_suffix(".pae")
    signature_path = statement_path.with_suffix(".signature")
    pae_path.write_bytes(dsse_pae(DSSE_PAYLOAD_TYPE, payload))
    run_checked(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-sign",
            str(private_key_path),
            "-out",
            str(signature_path),
            str(pae_path),
        ]
    )
    key_id = hashlib.sha256(public_key_path.read_bytes()).hexdigest()
    envelope = {
        "payloadType": DSSE_PAYLOAD_TYPE,
        "payload": base64.b64encode(payload).decode("ascii"),
        "signatures": [
            {
                "keyid": f"local-test:sha256:{key_id}",
                "sig": base64.b64encode(signature_path.read_bytes()).decode("ascii"),
            }
        ],
    }
    write_json(bundle_path, envelope)


def verify_local_dsse(
    bundle_path: Path,
    public_key_path: Path,
    expected_subjects: dict[str, str],
    scratch_dir: Path,
) -> dict:
    envelope = json.loads(bundle_path.read_text(encoding="utf-8"))
    if envelope.get("payloadType") != DSSE_PAYLOAD_TYPE:
        raise RuntimeError("unexpected DSSE payload type")
    signatures = envelope.get("signatures")
    if not isinstance(signatures, list) or len(signatures) != 1:
        raise RuntimeError("local DSSE envelope must contain exactly one signature")
    payload = base64.b64decode(envelope["payload"], validate=True)
    signature = base64.b64decode(signatures[0]["sig"], validate=True)
    pae_path = scratch_dir / "verify-dsse.pae"
    signature_path = scratch_dir / "verify-dsse.signature"
    pae_path.write_bytes(dsse_pae(DSSE_PAYLOAD_TYPE, payload))
    signature_path.write_bytes(signature)
    run_checked(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            str(public_key_path),
            "-signature",
            str(signature_path),
            str(pae_path),
        ]
    )
    statement = json.loads(payload)
    subjects = statement.get("subject")
    if not isinstance(subjects, list):
        raise RuntimeError("local provenance subjects must be a list")
    actual_subjects = {
        subject.get("name"): subject.get("digest", {}).get("sha256")
        for subject in subjects
        if isinstance(subject, dict)
    }
    if actual_subjects != expected_subjects:
        raise RuntimeError("local provenance subject SHA256 set does not match artifacts")
    return statement


def execute(repo_root: Path, output_dir: Path) -> dict:
    if not shutil.which("docker"):
        raise RuntimeError("Docker is required for the pinned local signing rehearsal")

    disposable_root = repo_root / ".release-state"
    disposable_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="taksklad-local-provenance-",
        dir=disposable_root,
    ) as temp_dir:
        work_dir = Path(temp_dir).resolve()
        (work_dir / "tiny.c").write_text(TINY_PE_SOURCE, encoding="utf-8")
        (work_dir / "leaf-extensions.cnf").write_text(LEAF_EXTENSIONS, encoding="utf-8")

        run_checked(
            [
                "docker",
                "run",
                "--rm",
                "-e",
                f"HOST_UID={getattr(os, 'getuid', lambda: 0)()}",
                "-e",
                f"HOST_GID={getattr(os, 'getgid', lambda: 0)()}",
                "-v",
                f"{work_dir}:/work",
                TOOLCHAIN_IMAGE,
                "sh",
                "-ceu",
                TOOLCHAIN_COMMAND,
            ]
        )

        signed_artifact = work_dir / "TakSklad-synthetic-signed.exe"
        assert_pe(signed_artifact)
        assert_toolchain_manifest(work_dir / "toolchain-packages.txt")
        artifact_sha256 = sha256_file(signed_artifact)
        source_commit = current_commit(repo_root)

        backend_context = work_dir / "backend-context"
        frontend_context = work_dir / "frontend-context"
        backend_owned_overrides = owned_context_overrides(repo_root, "backend")
        frontend_owned_overrides = owned_context_overrides(repo_root, "frontend")
        context_counts = {
            "backend": copy_safe_context(
                repo_root / "backend",
                backend_context,
                BACKEND_CONTEXT_ENTRIES,
                owned_overrides=backend_owned_overrides,
            ),
            "frontend": copy_safe_context(
                repo_root / "frontend",
                frontend_context,
                FRONTEND_CONTEXT_ENTRIES,
                owned_overrides=frontend_owned_overrides,
            ),
        }
        candidate_inputs = {
            "backend": context_input_hash(backend_context),
            "frontend": context_input_hash(frontend_context),
        }
        backend_oci = work_dir / "taksklad-backend.oci.tar"
        frontend_oci = work_dir / "taksklad-frontend.oci.tar"
        oci_subjects = {
            backend_oci.name: build_local_oci_subject(
                context_dir=backend_context,
                destination=backend_oci,
                image_name="backend",
            ),
            frontend_oci.name: build_local_oci_subject(
                context_dir=frontend_context,
                destination=frontend_oci,
                image_name="frontend",
            ),
        }
        subjects = {
            "TakSklad-synthetic-signed.exe": artifact_sha256,
            **oci_subjects,
        }
        statement = build_statement(
            subjects=subjects,
            source_commit=source_commit,
            candidate_inputs=candidate_inputs,
        )
        write_json(work_dir / "provenance.intoto.json", statement)

        dsse_path = work_dir / "provenance.dsse.json"
        create_local_dsse(
            work_dir / "provenance.intoto.json",
            work_dir / "leaf-key.pem",
            work_dir / "local-test-public-key.pem",
            dsse_path,
        )
        verify_local_dsse(
            dsse_path,
            work_dir / "local-test-public-key.pem",
            subjects,
            work_dir,
        )

        tampered_envelope = json.loads(dsse_path.read_text(encoding="utf-8"))
        tampered_statement = copy.deepcopy(statement)
        tampered_statement["subject"][0]["digest"]["sha256"] = "0" * 64
        tampered_envelope["payload"] = base64.b64encode(
            json.dumps(tampered_statement, sort_keys=True).encode("utf-8")
        ).decode("ascii")
        tampered_dsse_path = work_dir / "provenance-tampered.dsse.json"
        write_json(tampered_dsse_path, tampered_envelope)
        try:
            verify_local_dsse(
                tampered_dsse_path,
                work_dir / "local-test-public-key.pem",
                subjects,
                work_dir,
            )
        except RuntimeError:
            pass
        else:
            raise RuntimeError("tampered local DSSE provenance unexpectedly verified")

        verification = {
            "schema_version": 1,
            "status": "pass",
            "authority": AUTHORITY,
            "artifact": "TakSklad-synthetic-signed.exe",
            "artifact_sha256": artifact_sha256,
            "authenticode": {
                "embedded_signature_verified": True,
                "tamper_rejected": True,
                "certificate_scope": AUTHORITY,
                "production_certificate_verified": False,
                "production_gate": PRODUCTION_CERTIFICATE_STATUS,
            },
            "provenance": {
                "format": "SLSA v1 in-toto Statement in local-test DSSE envelope",
                "subject_sha256_verified": True,
                "signature_tamper_rejected": True,
                "authority": AUTHORITY,
                "github_identity_verified": False,
                "github_gate": GITHUB_IDENTITY_STATUS,
                "sigstore_bundle_verified": False,
            },
            "oci_subjects": {
                "platform": OCI_PLATFORM,
                "build_output": "type=oci,dest",
                "context_file_counts": context_counts,
                "candidate_input_sha256": candidate_inputs,
                "source_commit": source_commit,
                "dirty_candidate": True,
                "local_tag": "phase21-candidate",
                "owned_worktree_paths_restored_from_head": sorted(
                    [f"backend/{path}" for path in backend_owned_overrides]
                    + [f"frontend/{path}" for path in frontend_owned_overrides]
                ),
                "sha256": oci_subjects,
                "tar_files_retained": False,
            },
            "private_material_retained": False,
            "toolchain_image": TOOLCHAIN_IMAGE,
            "evidence_files": list(PUBLIC_EVIDENCE_NAMES),
        }
        write_json(work_dir / "verification.json", verification)

        for private_name in (
            "root-key.pem",
            "leaf-key.pem",
            "leaf.csr.pem",
        ):
            (work_dir / private_name).unlink(missing_ok=True)
        for oci_path, expected_sha256 in (
            (backend_oci, oci_subjects[backend_oci.name]),
            (frontend_oci, oci_subjects[frontend_oci.name]),
        ):
            if sha256_file(oci_path) != expected_sha256:
                raise RuntimeError(f"OCI subject changed before cleanup: {oci_path.name}")
            oci_path.unlink()
            if oci_path.exists():
                raise RuntimeError(f"OCI subject cleanup failed: {oci_path.name}")
        copy_public_evidence(work_dir, output_dir)

    if any(output_dir.glob("*key*.pem")):
        allowed_public_key = output_dir / "local-test-public-key.pem"
        unexpected = [path for path in output_dir.glob("*key*.pem") if path != allowed_public_key]
        if unexpected:
            raise RuntimeError("private key material escaped the disposable workspace")
    return verification


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        default="test-artifacts/provenance",
        help="directory for sanitized public local-test evidence",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    output_dir = Path(args.output_dir).resolve()
    verification = execute(repo_root, output_dir)
    print(
        json.dumps(
            {
                "status": verification["status"],
                "authority": verification["authority"],
                "artifact_sha256": verification["artifact_sha256"],
                "github_identity_verified": False,
                "production_certificate_verified": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
