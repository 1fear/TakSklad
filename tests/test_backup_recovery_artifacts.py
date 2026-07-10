import json
import gzip
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = PROJECT_ROOT / "deploy" / "vds" / "backup_postgres.sh"
OFFSITE_SCRIPT = PROJECT_ROOT / "tools" / "verify_offsite_backup.sh"
LEGACY_SCRIPT = PROJECT_ROOT / "tools" / "register_legacy_backup.sh"


class BackupRecoveryArtifactTests(unittest.TestCase):
    def run_script(self, script: Path, *args: str, env: dict[str, str]):
        return subprocess.run(
            [str(script), *args],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def isolated_env(self, root: Path) -> dict[str, str]:
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(root),
            "TMPDIR": str(root / "tmp"),
            "TAKSKLAD_BACKUP_TEST_DIR": str(root / "backups"),
            "TAKSKLAD_OFFSITE_TEST_BUCKET_DIR": str(root / "bucket"),
            "TAKSKLAD_OFFSITE_TEST_KEY_VAULT_DIR": str(root / "vault"),
            "TAKSKLAD_OFFSITE_EVIDENCE_DIR": str(root / "evidence"),
        }
        Path(env["TMPDIR"]).mkdir()
        return env

    @unittest.skipUnless(
        os.environ.get("TAKSKLAD_RUN_DOCKER_BACKUP_TESTS") == "1",
        "covered by mandatory Phase 24 Docker-backed backup command",
    )
    def test_synthetic_backup_is_atomic_validated_and_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self.isolated_env(root)
            result = self.run_script(
                BACKUP_SCRIPT, "--test-mode", "--synthetic-db", env=env
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            bundles = list((root / "backups" / "completed").iterdir())
            self.assertEqual(len(bundles), 1)
            manifests = list(bundles[0].glob("*.manifest.json"))
            archives = list(bundles[0].glob("*.dump"))
            checksums = list(bundles[0].glob("*.sha256"))
            inventories = list(bundles[0].glob("*.list"))
            self.assertEqual(len(manifests), 1)
            self.assertEqual(len(archives), 1)
            self.assertEqual(len(checksums), 1)
            self.assertEqual(len(inventories), 1)
            self.assertFalse(list((root / "backups").glob(".staging-*")))

            payload = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["source"], "synthetic-postgresql")
            self.assertIs(payload["actual_postgresql"], True)
            self.assertIs(payload["atomic_bundle"], True)
            self.assertEqual(payload["disposable_cleanup_count"], 0)
            self.assertIs(payload["contains_customer_content"], False)
            self.assertIs(payload["sanitized_manifest"], True)
            self.assertIs(payload["archive"]["validated"], True)
            self.assertEqual(
                payload["archive"]["format"],
                "postgresql-custom",
            )
            self.assertEqual(payload["archive"]["checksum_sidecar"], checksums[0].name)
            self.assertEqual(payload["archive"]["list"]["filename"], inventories[0].name)
            self.assertGreaterEqual(payload["archive"]["list"]["entries"], 2)
            actual_sha = subprocess.check_output(
                ["shasum", "-a", "256", str(archives[0])], text=True
            ).split()[0]
            self.assertEqual(payload["archive"]["sha256"], actual_sha)

    def write_legacy(self, root: Path) -> Path:
        path = root / "old.sql.gz"
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            stream.write("-- PostgreSQL database dump\n")
            stream.write("CREATE TABLE public.probe (id integer);\n")
            stream.write("-- PostgreSQL database dump complete\n")
        return path

    def write_synthetic_bundle(self, root: Path) -> Path:
        backup_id = "taksklad-postgres-20260710T000000Z-synthetic-fixture"
        bundle = root / "backups" / "completed" / backup_id
        bundle.mkdir(parents=True)
        archive_name = f"{backup_id}.dump"
        checksum_name = f"{backup_id}.sha256"
        archive = bundle / archive_name
        archive.write_bytes(b"PGDMP-content-free-fixture")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (bundle / checksum_name).write_text(f"{digest}  {archive_name}\n", encoding="utf-8")
        manifest = bundle / f"{backup_id}.manifest.json"
        manifest.write_text(json.dumps({
            "schema_version": 2,
            "backup_id": backup_id,
            "source": "synthetic-postgresql",
            "actual_postgresql": True,
            "contains_customer_content": False,
            "atomic_bundle": True,
            "archive": {"filename": archive_name, "sha256": digest, "checksum_sidecar": checksum_name},
        }), encoding="utf-8")
        return manifest

    def test_failed_legacy_registration_leaves_no_final_or_staging_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self.isolated_env(root)
            legacy = self.write_legacy(root)
            result = self.run_script(LEGACY_SCRIPT, "--input", str(legacy), "--output-root", str(root / "registered"), "--simulate-failure", env=env)
            self.assertEqual(result.returncode, 86)
            self.assertIn("Synthetic failure requested", result.stderr)
            self.assertFalse(list((root / "registered" / "completed").iterdir()))
            self.assertFalse(list((root / "registered").glob(".staging-*")))

    def test_offsite_test_bucket_encrypts_and_verifies_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self.isolated_env(root)
            self.write_synthetic_bundle(root)
            result = self.run_script(
                OFFSITE_SCRIPT, "--test-bucket", "--checksum", env=env
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads(
                (root / "evidence" / "offsite-backup-evidence.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(evidence["mode"], "local-test-bucket")
            self.assertEqual(evidence["external_mutations"], 0)
            self.assertIs(evidence["object"]["encrypted"], True)
            self.assertIs(evidence["object"]["checksum_verified"], True)
            self.assertIs(evidence["recoverable"], True)
            self.assertEqual(evidence["recovery_key"]["mode"], "0600")
            self.assertEqual(len(list((root / "bucket").glob("*.enc"))), 1)
            self.assertEqual(len(list((root / "vault").glob("*.recovery-key"))), 1)

    def test_offsite_refuses_manifest_sha_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self.isolated_env(root)
            manifest_path = self.write_synthetic_bundle(root)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["archive"]["sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")

            result = self.run_script(
                OFFSITE_SCRIPT, "--test-bucket", "--checksum", env=env
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Local archive checksum mismatch", result.stderr)
            self.assertFalse(list((root / "bucket").glob("*.enc")))

    def test_legacy_registration_creates_validated_atomic_transition_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self.isolated_env(root)
            legacy = self.write_legacy(root)
            result = self.run_script(LEGACY_SCRIPT, "--input", str(legacy), "--output-root", str(root / "registered"), env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = next((root / "registered" / "completed").iterdir())
            manifest = json.loads(next(bundle.glob("*.manifest.json")).read_text(encoding="utf-8"))
            self.assertIs(manifest["atomic_bundle"], True)
            self.assertIs(manifest["transition_registered"], True)
            self.assertIs(manifest["isolated_restore_validation_required"], True)

    def test_test_mode_requires_explicit_synthetic_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = self.isolated_env(root)
            result = self.run_script(BACKUP_SCRIPT, "--test-mode", env=env)
            self.assertEqual(result.returncode, 2)
            self.assertIn("requires --synthetic-db", result.stderr)


if __name__ == "__main__":
    unittest.main()
