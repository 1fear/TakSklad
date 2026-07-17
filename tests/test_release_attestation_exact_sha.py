import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERIFIER = PROJECT_ROOT / "tools" / "release_artifacts.py"
WRAPPER = PROJECT_ROOT / "tools" / "verify_release_attestations.sh"


def production_manifest(source_sha: str) -> dict:
    digest = f"sha256:{'b' * 64}"
    value = {
        "schema_version": 1,
        "authority": "github-sigstore",
        "deployable": True,
        "source_sha": source_sha,
        "acceptance_required": True,
        "capabilities": ["returns_auth_canary_v2_exact_identifier"],
        "images": {
            role: {
                "name": f"ghcr.io/1fear/taksklad-{role}",
                "tag": f"sha-{source_sha}",
                "digest": digest,
                "reference": f"ghcr.io/1fear/taksklad-{role}@{digest}",
            }
            for role in ("backend", "frontend")
        },
        "windows": {
            "version": "2.0.26",
            "artifact": "TakSklad.exe",
            "artifact_sha256": "c" * 64,
            "auth_helper": "TakSkladAuth.exe",
            "auth_helper_sha256": "2" * 64,
            "auth_helper_sha256_onedir": "3" * 64,
            "app_sha256_onedir": "4" * 64,
            "acceptance_wrapper": "windows_backend_acceptance.ps1",
            "acceptance_wrapper_sha256": "5" * 64,
            "artifact_onedir": "TakSklad-windows-x64.zip",
            "artifact_sha256_onedir": "e" * 64,
            "manifest": "version.json",
            "manifest_sha256": "f" * 64,
            "dependency_lock_sha256": "d" * 64,
            "signature_type": "authenticode",
            "signature_required": True,
            "signer_certificate_sha256": "1" * 64,
        },
        "release_tag": "v2.0.26",
        "ci": {
            "workflow": "CI",
            "run_id": 123,
            "head_sha": source_sha,
            "event": "push",
            "head_branch": "main",
            "required_check": "Release gate",
            "conclusion": "success",
        },
        "database_rollback": {
            "strategy": "retain-current-schema",
            "alembic_downgrade_allowed": False,
        },
        "attestation": {
            "github_identity_verified": True,
            "registry_attestation_verified": True,
        },
    }
    value["attestation_subjects"] = [
        {"kind": "windows", "name": "TakSklad.exe", "sha256": "c" * 64},
        {"kind": "windows", "name": "TakSkladAuth.exe", "sha256": "2" * 64},
        {"kind": "windows", "name": "TakSklad-windows-x64.zip", "sha256": "e" * 64},
        {"kind": "windows", "name": "version.json", "sha256": "f" * 64},
        *[
            {
                "kind": "oci",
                "name": value["images"][role]["reference"],
                "digest": digest,
            }
            for role in ("backend", "frontend")
        ],
    ]
    return value


class ReleaseAttestationExactShaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout.strip()

    def run_verifier(self, manifest: dict, requested_sha: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = Path(temporary) / "release.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            return subprocess.run(
                [
                    sys.executable,
                    str(VERIFIER),
                    "verify",
                    "--manifest",
                    str(manifest_path),
                    "--sha",
                    requested_sha,
                ],
                cwd=PROJECT_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

    def run_candidate_verifier(self, manifest: dict) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = Path(temporary) / "release.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            return subprocess.run(
                [
                    sys.executable,
                    str(VERIFIER),
                    "verify",
                    "--manifest",
                    str(manifest_path),
                    "--candidate",
                ],
                cwd=PROJECT_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

    def test_exact_current_sha_production_manifest_passes(self):
        result = self.run_verifier(production_manifest(self.head), self.head)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"source_sha={self.head}", result.stdout)
        self.assertIn("authority=github-sigstore", result.stdout)
        self.assertIn("production_deployable=1", result.stdout)

    def test_non_exact_production_candidate_still_requires_canary_capability(self):
        manifest = production_manifest(self.head)
        manifest.pop("capabilities")

        result = self.run_candidate_verifier(manifest)

        self.assertEqual(result.returncode, 1)
        self.assertIn("must declare returns auth canary capability", result.stderr)

    def test_manifest_sha_must_equal_requested_sha(self):
        result = self.run_verifier(production_manifest("a" * 40), self.head)

        self.assertEqual(result.returncode, 1)
        self.assertIn("manifest source SHA differs from requested", result.stderr)

    def test_requested_sha_must_equal_current_commit(self):
        requested_sha = "a" * 40 if self.head != "a" * 40 else "b" * 40
        result = self.run_verifier(production_manifest(requested_sha), requested_sha)

        self.assertEqual(result.returncode, 1)
        self.assertIn("requested source SHA differs from current commit", result.stderr)

    def test_exact_sha_rejects_local_authority_and_non_deployable_manifest(self):
        manifest = production_manifest(self.head)
        manifest["authority"] = "local-test"
        manifest["deployable"] = False
        manifest["attestation"]["github_identity_verified"] = False
        manifest["attestation"]["registry_attestation_verified"] = False

        result = self.run_verifier(manifest, self.head)

        self.assertEqual(result.returncode, 1)
        self.assertIn("production manifest requires GitHub Sigstore authority", result.stderr)

    def test_requested_sha_must_be_exact_lowercase_hex(self):
        result = self.run_verifier(production_manifest(self.head), self.head.upper())

        self.assertEqual(result.returncode, 1)
        self.assertIn("must be exactly 40 lowercase hex", result.stderr)

    def test_shell_wrapper_reverifies_manifest_registry_and_windows_subjects(self):
        source = WRAPPER.read_text(encoding="utf-8")

        self.assertIn('TAKSKLAD_RELEASE_MANIFEST:-release.json', source)
        self.assertIn('TAKSKLAD_RELEASE_ARTIFACT_DIR:-.release-state/production-release', source)
        self.assertIn('--signer-workflow "$SIGNER_WORKFLOW"', source)
        self.assertIn('--source-digest "$REQUESTED_SHA"', source)
        self.assertIn('oci://$BACKEND_REFERENCE', source)
        self.assertIn('oci://$FRONTEND_REFERENCE', source)
        self.assertIn(
            'for subject in "$WINDOWS_EXE" "$WINDOWS_AUTH_HELPER" "$WINDOWS_ZIP" "$WINDOWS_MANIFEST"',
            source,
        )
        self.assertIn("subjects=7", source)
        last_attestation = source.rindex("gh attestation verify")
        zip_validation = source.index("tools/verify_windows_release_zip.py")
        self.assertLess(last_attestation, zip_validation)
        self.assertIn("--extract-windows-to", source)
        self.assertIn("zip_verify_args+=(--extract-to", source)
        self.assertNotIn("Expand-Archive", source)

    def test_local_release_fixture_tracks_current_app_and_lock_contract(self):
        result = subprocess.run(
            [str(WRAPPER), "--local"],
            cwd=PROJECT_ROOT,
            env={**os.environ, "PYTHON_BIN": sys.executable},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("authority=local-test", result.stdout)
        self.assertIn("production_deployable=0", result.stdout)


if __name__ == "__main__":
    unittest.main()
