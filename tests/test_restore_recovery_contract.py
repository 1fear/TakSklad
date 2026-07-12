import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESTORE_SCRIPT = PROJECT_ROOT / "deploy" / "vds" / "restore_postgres.sh"
RESTORE_DRILL = PROJECT_ROOT / "deploy" / "vds" / "restore_drill.sh"
PITR_DRILL = PROJECT_ROOT / "tools" / "run_pitr_drill.sh"


class RestoreRecoveryContractTests(unittest.TestCase):
    def run_command(self, command, *, env=None):
        return subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_synthetic_restore_uses_disposable_postgresql_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            env = os.environ.copy()
            env["TAKSKLAD_BACKUP_TEST_DIR"] = str(temp_path / "backups")
            env["TAKSKLAD_PYTHON_BIN"] = sys.executable
            result = self.run_command(
                [str(RESTORE_DRILL), "--isolated", "--synthetic-db", "--assert-invariants"],
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("RESTORE_DRILL_OK", result.stdout)
            self.assertIn("actual_postgresql=true", result.stdout)
            self.assertIn("cleanup_zero=true", result.stdout)
            evidence = json.loads(
                (PROJECT_ROOT / "test-artifacts" / "disaster-recovery" / "restore-drill.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(evidence["drill_mode"], "disposable-postgresql-custom-archive")
            self.assertEqual(evidence["counts"]["synthetic_restore_probe"], 1)
            self.assertIs(evidence["actual_postgresql_restore"], True)
            self.assertEqual(evidence["disposable_cleanup_count"], 0)
            self.assertIs(evidence["production_touched"], False)
            self.assertTrue(all(evidence["invariants"].values()))

    def test_synthetic_pitr_uses_physical_wal_and_is_bounded(self):
        result = self.run_command(
            [
                str(PITR_DRILL),
                "--synthetic-db",
                "--assert-rpo-minutes",
                "15",
                "--assert-rto-minutes",
                "30",
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PITR_DRILL_OK", result.stdout)
        self.assertIn("actual_postgresql=true", result.stdout)
        self.assertIn("cleanup_zero=true", result.stdout)
        evidence = json.loads(
            (PROJECT_ROOT / "test-artifacts" / "disaster-recovery" / "pitr-drill.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertLessEqual(evidence["rpo_minutes"], 15)
        self.assertLessEqual(evidence["rto_seconds"], 30 * 60)
        self.assertIs(evidence["actual_postgresql_pitr"], True)
        self.assertEqual(evidence["event_before_target_count"], 1)
        self.assertEqual(evidence["event_after_target_count"], 0)
        self.assertIs(evidence["production_touched"], False)

    def make_production_manifest(self, directory: Path):
        backup_id = "taksklad-postgres-20990101T000000Z"
        archive = directory / f"{backup_id}.dump"
        archive.write_bytes(b"PGDMP-synthetic-contract-fixture")
        archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        inventory = directory / f"{backup_id}.list"
        inventory.write_text("TABLE public fixture\n", encoding="utf-8")
        inventory_digest = hashlib.sha256(inventory.read_bytes()).hexdigest()
        checksum = directory / f"{backup_id}.sha256"
        checksum.write_text(f"{archive_digest}  {archive.name}\n", encoding="utf-8")
        manifest = directory / f"{backup_id}.manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "backup_id": backup_id,
                    "archive": {
                        "filename": archive.name,
                        "format": "postgresql-custom",
                        "sha256": archive_digest,
                        "bytes": archive.stat().st_size,
                        "validated": True,
                        "checksum_sidecar": checksum.name,
                        "list": {
                            "filename": inventory.name,
                            "sha256": inventory_digest,
                            "entries": 1,
                            "validated": True,
                        },
                    },
                    "source": "postgresql",
                    "actual_postgresql": True,
                    "postgres_image": "postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777",
                    "contains_customer_content": True,
                    "sanitized_manifest": True,
                    "atomic_bundle": True,
                }
            ),
            encoding="utf-8",
        )
        return manifest, archive, backup_id, archive_digest

    def test_production_restore_rejects_without_exact_dynamic_approval_before_env_access(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, _, backup_id, digest = self.make_production_manifest(Path(temp))
            env = os.environ.copy()
            env["TAKSKLAD_ENV_FILE"] = str(Path(temp) / "must-not-be-read.env")
            result = self.run_command([str(RESTORE_SCRIPT), "--restore", str(manifest)], env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f"APPROVE_TAKSKLAD_PRODUCTION_RESTORE {backup_id} {digest}", result.stderr)
            self.assertNotIn("Missing env file", result.stderr)

    def test_production_restore_rejects_tampered_archive_before_approval_or_env(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, archive, _, _ = self.make_production_manifest(Path(temp))
            archive.write_bytes(archive.read_bytes() + b"tampered")
            env = os.environ.copy()
            env["TAKSKLAD_ENV_FILE"] = str(Path(temp) / "must-not-be-read.env")
            result = self.run_command([str(RESTORE_SCRIPT), "--restore", str(manifest)], env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("backup checksum mismatch", result.stderr)
            self.assertNotIn("Missing env file", result.stderr)

    def test_production_restore_contract_drains_writers_and_prebacks_up_before_drop(self):
        script = RESTORE_SCRIPT.read_text(encoding="utf-8")
        approval = 'expected_approval="APPROVE_TAKSKLAD_PRODUCTION_RESTORE $BACKUP_ID $BACKUP_SHA256"'
        disposable_validation = '"$SCRIPT_DIR/restore_drill.sh" "$INPUT_FILE"'
        drain = 'stop "${WRITER_SERVICES[@]}"'
        sessions = 'select count(*) from pg_stat_activity'
        prebackup = 'pre_restore_output="$($SCRIPT_DIR/backup_postgres.sh)"'
        destructive = '"DROP SCHEMA public CASCADE; CREATE SCHEMA public;"'
        self.assertIn(approval, script)
        self.assertLess(script.index(approval), script.index(disposable_validation))
        self.assertLess(script.index(disposable_validation), script.index(drain))
        self.assertLess(script.index(drain), script.index(prebackup))
        self.assertLess(script.index(sessions), script.index(prebackup))
        self.assertLess(script.index(prebackup), script.index(destructive))
        self.assertIn('archive_format not in {"postgresql-custom", "postgresql-plain-sql-gzip-legacy-transition"}', script)
        self.assertIn("backup archive list checksum mismatch", script)
        self.assertIn("backup checksum sidecar mismatch", script)
        self.assertIn("pg_restore", script)
        self.assertIn("gzip -dc", script)
        self.assertIn("awaiting_operator_validation", script)
        self.assertIn("all writers remain stopped", script)
        self.assertIn('"database": "ok"', script)
        self.assertIn('"migrations": "ok"', script)
        self.assertIn("full_policy_readiness", script)
        self.assertIn("active_sessions=0", script)
        self.assertIn("disposable_prevalidation=pass", script)
        self.assertNotIn("grep -q '\"ready\": true' <<<\"$readiness\"", script)
        self.assertNotIn("alembic downgrade", script)

    def test_weekly_drill_timer_is_isolated_and_records_freshness_evidence(self):
        service = (
            PROJECT_ROOT / "deploy/vds/systemd/taksklad-postgres-restore-drill.service"
        ).read_text(encoding="utf-8")
        timer = (
            PROJECT_ROOT / "deploy/vds/systemd/taksklad-postgres-restore-drill.timer"
        ).read_text(encoding="utf-8")
        installer = (PROJECT_ROOT / "deploy/vds/install_backup_timer.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("--isolated --synthetic-db --assert-invariants", service)
        self.assertIn("TAKSKLAD_DR_EVIDENCE_DIR=/opt/taksklad/dr-evidence", service)
        self.assertIn("OnCalendar=Sun", timer)
        self.assertIn('systemctl enable --now "$DRILL_NAME.timer"', installer)


if __name__ == "__main__":
    unittest.main()
