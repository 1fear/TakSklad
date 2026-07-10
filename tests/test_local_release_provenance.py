import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.verify_local_release_provenance import (
    AUTHORITY,
    GITHUB_IDENTITY_STATUS,
    PRODUCTION_CERTIFICATE_STATUS,
    OCI_PLATFORM,
    TOOLCHAIN_COMMAND,
    TOOLCHAIN_IMAGE,
    TOOLCHAIN_PACKAGES,
    assert_toolchain_manifest,
    build_statement,
    copy_safe_context,
    dsse_pae,
    execute,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class LocalReleaseProvenanceTests(unittest.TestCase):
    def test_static_toolchain_manifest_matches_executable_contract(self):
        manifest = json.loads(
            (
                REPO_ROOT
                / "test-artifacts"
                / "provenance"
                / "local-toolchain-manifest.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(manifest["authority"], AUTHORITY)
        self.assertEqual(manifest["images"]["toolchain"], TOOLCHAIN_IMAGE)
        self.assertEqual(tuple(manifest["packages"]), TOOLCHAIN_PACKAGES)
        self.assertFalse(manifest["trust"]["github_identity_verified"])
        self.assertEqual(manifest["trust"]["github_gate"], GITHUB_IDENTITY_STATUS)
        self.assertFalse(manifest["trust"]["production_certificate_verified"])
        self.assertEqual(
            manifest["trust"]["production_gate"],
            PRODUCTION_CERTIFICATE_STATUS,
        )
        self.assertEqual(manifest["trust"]["local_bundle_format"], "openssl-signed-dsse")
        self.assertFalse(manifest["trust"]["sigstore_bundle_verified"])
        self.assertEqual(manifest["oci_build"]["platform"], OCI_PLATFORM)
        self.assertEqual(manifest["oci_build"]["output"], "type=oci,dest")
        self.assertFalse(manifest["oci_build"]["retains_oci_tar"])
        self.assertEqual(manifest["oci_build"]["local_tag"], "phase21-candidate")

    def test_tool_images_and_packages_are_immutable(self):
        self.assertRegex(TOOLCHAIN_IMAGE, r"@?sha256:[0-9a-f]{64}$")
        self.assertTrue(TOOLCHAIN_PACKAGES)
        for package in TOOLCHAIN_PACKAGES:
            with self.subTest(package=package):
                self.assertRegex(package, r"^[a-z0-9+.-]+=[^=]+$")
                self.assertIn(package, TOOLCHAIN_COMMAND)

    def test_statement_is_subject_bound_slsa_v1_and_local_only(self):
        digest = "a" * 64
        statement = build_statement(
            subjects={
                "TakSklad-synthetic-signed.exe": digest,
                "taksklad-backend.oci.tar": "c" * 64,
                "taksklad-frontend.oci.tar": "d" * 64,
            },
            source_commit="b" * 40,
            candidate_inputs={"backend": "e" * 64, "frontend": "f" * 64},
        )

        self.assertEqual(statement["_type"], "https://in-toto.io/Statement/v1")
        self.assertEqual(statement["predicateType"], "https://slsa.dev/provenance/v1")
        subjects = {
            item["name"]: item["digest"]["sha256"] for item in statement["subject"]
        }
        self.assertEqual(subjects["TakSklad-synthetic-signed.exe"], digest)
        self.assertEqual(subjects["taksklad-backend.oci.tar"], "c" * 64)
        self.assertEqual(subjects["taksklad-frontend.oci.tar"], "d" * 64)
        internal = statement["predicate"]["buildDefinition"]["internalParameters"]
        self.assertIs(internal["dirtyCandidate"], True)
        self.assertEqual(internal["candidateInputSha256"]["backend"], "e" * 64)
        self.assertEqual(
            statement["predicate"]["buildDefinition"]["externalParameters"]["authority"],
            AUTHORITY,
        )
        serialized = json.dumps(statement, sort_keys=True)
        self.assertNotIn("token.actions.githubusercontent.com", serialized)
        self.assertNotIn("github.com/1fear/TakSklad/.github/workflows", serialized)

    def test_contract_keeps_external_trust_gates_explicit(self):
        self.assertEqual(AUTHORITY, "local-test")
        self.assertEqual(
            GITHUB_IDENTITY_STATUS,
            "GITHUB_IDENTITY_ATTESTATION_NOT_AVAILABLE",
        )
        self.assertEqual(
            PRODUCTION_CERTIFICATE_STATUS,
            "WINDOWS_CODESIGN_CERTIFICATE_NOT_AVAILABLE",
        )
        self.assertIn("osslsigncode verify", TOOLCHAIN_COMMAND)
        self.assertIn("tampered Authenticode artifact unexpectedly verified", TOOLCHAIN_COMMAND)

    def test_dsse_pae_is_length_prefixed(self):
        self.assertEqual(
            dsse_pae("application/test", b"payload"),
            b"DSSEv1 16 application/test 7 payload",
        )

    def test_runtime_package_manifest_requires_every_pin(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest_path = Path(temp_dir) / "packages.txt"
            manifest_path.write_text("\n".join(TOOLCHAIN_PACKAGES) + "\n", encoding="utf-8")
            assert_toolchain_manifest(manifest_path)
            manifest_path.write_text(TOOLCHAIN_PACKAGES[0] + "\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "pinned toolchain package missing"):
                assert_toolchain_manifest(manifest_path)

    def test_safe_context_excludes_forbidden_local_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            destination = root / "destination"
            (source / "app").mkdir(parents=True)
            (source / "app" / "main.py").write_text("pass\n", encoding="utf-8")
            (source / "app" / ".env.local").write_text("forbidden\n", encoding="utf-8")
            (source / "app" / "outputs").mkdir()
            (source / "app" / "outputs" / "report.json").write_text("{}\n", encoding="utf-8")

            self.assertEqual(copy_safe_context(source, destination, ("app",)), 1)
            self.assertTrue((destination / "app" / "main.py").is_file())
            self.assertFalse((destination / "app" / ".env.local").exists())
            self.assertFalse((destination / "app" / "outputs").exists())

    def test_safe_context_uses_owned_override_without_reading_worktree_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            destination = root / "destination"
            (source / "app").mkdir(parents=True)
            (source / "app" / "service.py").write_text("user dirty\n", encoding="utf-8")

            copied = copy_safe_context(
                source,
                destination,
                ("app",),
                owned_overrides={"app/service.py": b"head version\n"},
            )

            self.assertEqual(copied, 1)
            self.assertEqual(
                (destination / "app" / "service.py").read_text(encoding="utf-8"),
                "head version\n",
            )

    @unittest.skipUnless(
        os.environ.get("TAKSKLAD_RUN_DOCKER_SIGNING_TEST") == "1",
        "set TAKSKLAD_RUN_DOCKER_SIGNING_TEST=1 for pinned Docker integration",
    )
    def test_pinned_docker_signing_and_subject_verification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "evidence"
            result = execute(REPO_ROOT, output_dir)

            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["authority"], "local-test")
            self.assertFalse(result["authenticode"]["production_certificate_verified"])
            self.assertFalse(result["provenance"]["github_identity_verified"])
            self.assertEqual(len(result["oci_subjects"]["sha256"]), 2)
            self.assertFalse(result["oci_subjects"]["tar_files_retained"])
            self.assertTrue(result["oci_subjects"]["dirty_candidate"])
            self.assertEqual(result["oci_subjects"]["local_tag"], "phase21-candidate")
            self.assertFalse(result["private_material_retained"])
            self.assertTrue((output_dir / "provenance.dsse.json").is_file())
            self.assertTrue((output_dir / "TakSklad-synthetic-signed.exe").is_file())
            self.assertFalse((output_dir / "leaf-key.pem").exists())
            self.assertFalse((output_dir / "root-key.pem").exists())


if __name__ == "__main__":
    unittest.main()
