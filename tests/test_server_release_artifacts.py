import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools.server_release_artifacts import (
    ServerReleaseArtifactError,
    emit_shell,
    verify_manifest,
)


SOURCE_SHA = "a" * 40
BACKEND_DIGEST = "sha256:" + "b" * 64
FRONTEND_DIGEST = "sha256:" + "c" * 64


def valid_manifest():
    backend_reference = f"ghcr.io/1fear/taksklad-backend@{BACKEND_DIGEST}"
    frontend_reference = f"ghcr.io/1fear/taksklad-frontend@{FRONTEND_DIGEST}"
    return {
        "schema_version": 1,
        "release_kind": "server",
        "authority": "github-sigstore",
        "deployable": True,
        "source_sha": SOURCE_SHA,
        "server_release_id": f"server-{SOURCE_SHA}",
        "capabilities": ["returns_auth_canary_v2_exact_identifier"],
        "ci": {
            "workflow": "CI",
            "run_id": 123,
            "run_attempt": 1,
            "head_sha": SOURCE_SHA,
            "event": "push",
            "head_branch": "main",
            "required_check": "Release gate",
            "conclusion": "success",
        },
        "images": {
            "backend": {
                "name": "ghcr.io/1fear/taksklad-backend",
                "digest": BACKEND_DIGEST,
                "reference": backend_reference,
            },
            "frontend": {
                "name": "ghcr.io/1fear/taksklad-frontend",
                "digest": FRONTEND_DIGEST,
                "reference": frontend_reference,
            },
        },
        "compatibility": {
            "desktop_api_contract": 1,
            "min_desktop_version": "2.0.49",
        },
        "database": {
            "migration_policy": "no_change",
            "alembic_head": "20260719_0020",
            "destructive_migrations_allowed": False,
            "alembic_downgrade_allowed": False,
        },
        "attestation": {"registry_attestation_verified": True},
        "attestation_subjects": [
            {
                "kind": "oci",
                "role": "backend",
                "name": backend_reference,
                "digest": BACKEND_DIGEST,
            },
            {
                "kind": "oci",
                "role": "frontend",
                "name": frontend_reference,
                "digest": FRONTEND_DIGEST,
            },
        ],
    }


class ServerReleaseArtifactTests(unittest.TestCase):
    def write_manifest(self, directory, manifest):
        path = Path(directory) / "server-release.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_valid_server_manifest_is_bound_to_exact_source_and_has_no_windows_subjects(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_manifest(directory, valid_manifest())

            verified = verify_manifest(
                path,
                expected_sha=SOURCE_SHA,
                current_sha=SOURCE_SHA,
            )

        self.assertEqual(verified["release_kind"], "server")
        self.assertNotIn("windows", verified)

    def test_candidate_verification_binds_manifest_to_current_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_manifest(directory, valid_manifest())
            verified = verify_manifest(
                path,
                current_sha=SOURCE_SHA,
                candidate=True,
            )
            self.assertEqual(verified["source_sha"], SOURCE_SHA)

            with self.assertRaises(ServerReleaseArtifactError):
                verify_manifest(
                    path,
                    current_sha="d" * 40,
                    candidate=True,
                )

    def test_verifier_rejects_source_ci_and_release_id_mismatch(self):
        mutations = (
            lambda value: value.update(source_sha="d" * 40),
            lambda value: value["ci"].update(head_sha="d" * 40),
            lambda value: value.update(server_release_id="server-" + "d" * 40),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory:
                manifest = valid_manifest()
                mutate(manifest)
                path = self.write_manifest(directory, manifest)
                with self.assertRaises(ServerReleaseArtifactError):
                    verify_manifest(path)

    def test_verifier_rejects_mutable_image_reference_and_subject_mismatch(self):
        mutations = (
            lambda value: value["images"]["backend"].update(
                reference="ghcr.io/1fear/taksklad-backend:latest"
            ),
            lambda value: value["attestation_subjects"][0].update(digest=FRONTEND_DIGEST),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory:
                manifest = valid_manifest()
                mutate(manifest)
                path = self.write_manifest(directory, manifest)
                with self.assertRaises(ServerReleaseArtifactError):
                    verify_manifest(path)

    def test_verifier_rejects_desktop_or_migration_contract_drift(self):
        mutations = (
            lambda value: value["compatibility"].update(desktop_api_contract=2),
            lambda value: value["compatibility"].update(min_desktop_version="2.0.50"),
            lambda value: value["database"].update(migration_policy="expand_only"),
            lambda value: value["database"].update(alembic_head="invalid revision!"),
            lambda value: value["database"].update(destructive_migrations_allowed=True),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory:
                manifest = valid_manifest()
                mutate(manifest)
                path = self.write_manifest(directory, manifest)
                with self.assertRaises(ServerReleaseArtifactError):
                    verify_manifest(path)

    def test_historical_manifest_is_shape_valid_but_not_a_candidate_for_current_head(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = valid_manifest()
            manifest["database"]["alembic_head"] = "20260716_0018"
            path = self.write_manifest(directory, manifest)

            self.assertEqual(verify_manifest(path)["database"]["alembic_head"], "20260716_0018")
            with self.assertRaisesRegex(ServerReleaseArtifactError, "repository"):
                verify_manifest(
                    path,
                    expected_sha=SOURCE_SHA,
                    current_sha=SOURCE_SHA,
                )

    def test_verifier_requires_exact_identifier_auth_canary_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = valid_manifest()
            manifest["capabilities"] = []
            path = self.write_manifest(directory, manifest)

            with self.assertRaisesRegex(ServerReleaseArtifactError, "capabilities"):
                verify_manifest(path)

    def test_emit_shell_contains_only_bounded_release_identity(self):
        manifest = valid_manifest()
        output = io.StringIO()

        with redirect_stdout(output):
            emit_shell(manifest)

        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 11)
        self.assertIn(f"RELEASE_SOURCE_SHA={SOURCE_SHA}", lines)
        self.assertIn(f"RELEASE_SERVER_RELEASE_ID=server-{SOURCE_SHA}", lines)
        self.assertIn("RELEASE_DESKTOP_API_CONTRACT=1", lines)
        self.assertIn("RELEASE_MIN_DESKTOP_VERSION=2.0.49", lines)
        self.assertIn("RELEASE_DATABASE_MIGRATION_POLICY=no_change", lines)
        self.assertIn("RELEASE_ALEMBIC_HEAD=20260719_0020", lines)
        self.assertNotIn("TOKEN", output.getvalue())
        self.assertNotIn("PASSWORD", output.getvalue())


if __name__ == "__main__":
    unittest.main()
