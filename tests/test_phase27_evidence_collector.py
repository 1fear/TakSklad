import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.collect_phase27_evidence import (
    CollectionError,
    latest_backup,
    live_runtime_invariants,
    percentile,
    readiness_summary,
    restore_drill,
    run,
)


class Phase27EvidenceCollectorTests(unittest.TestCase):
    def test_latest_backup_reads_only_sanitized_manifest_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            older = root / "taksklad-postgres-old"
            latest = root / "taksklad-postgres-new"
            older.mkdir()
            latest.mkdir()
            (older / "old.manifest.json").write_text(
                json.dumps({"backup_id": "old", "archive": {"sha256": "a" * 64}}),
                encoding="utf-8",
            )
            manifest = latest / "new.manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "backup_id": "new",
                        "created_at_utc": "2026-07-12T12:00:00Z",
                        "atomic_bundle": True,
                        "archive": {
                            "sha256": "b" * 64,
                            "validated": True,
                            "format": "postgresql-custom",
                        },
                    }
                ),
                encoding="utf-8",
            )
            manifest.touch()

            result = latest_backup(root)

        self.assertEqual(result["backup_id"], "new")
        self.assertEqual(set(result), {"backup_id", "sha256", "created_at_utc", "validated", "atomic_bundle", "format"})

    def test_readiness_summary_is_bounded_and_sanitized(self):
        result = readiness_summary(
            {
                "ready": True,
                "database": {"status": "ok", "password": "do-not-copy"},
                "migrations": {"status": "ok", "current_revision": "old", "expected_head": "old"},
                "workers": {"status": "ok"},
                "policy": {"mandatory_status": "ok"},
                "queue": {"hot_path_blocking_count": 0, "hot_path_stale_processing_count": 0, "last_errors": ["private"]},
            },
            200,
        )
        self.assertEqual(result["database_status"], "ok")
        self.assertNotIn("password", result)
        self.assertNotIn("last_errors", result)

    def test_restore_drill_accepts_green_isolated_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fallback = root / "restore-drill.json"
            fallback.write_text(
                json.dumps(
                    {
                        "isolated": True,
                        "actual_postgresql_restore": True,
                        "production_touched": False,
                        "rto_met": True,
                        "completed_at": "2026-07-12T12:00:00Z",
                        "readiness": {"database": "ok", "migrations": "ok"},
                    }
                ),
                encoding="utf-8",
            )
            result = restore_drill(root / "missing-maintenance.json", fallback)
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["isolated"])

    def test_percentile_uses_nearest_rank(self):
        self.assertEqual(percentile([1, 2, 3, 4, 5], 0.95), 5)

    def test_command_failure_redacts_secret_like_output(self):
        with self.assertRaises(CollectionError) as raised:
            run(["sh", "-c", "echo token=synthetic-sensitive-value; exit 7"])
        self.assertNotIn("synthetic-sensitive-value", str(raised.exception))
        self.assertIn("token=[REDACTED]", str(raised.exception))

    def test_run_can_stream_read_only_tool_over_stdin(self):
        output = run(["sh", "-c", "read value; printf '%s' \"$value\""], input_text="runtime-invariant\n")
        self.assertEqual(output, "runtime-invariant")

    def test_live_invariants_use_running_backend_without_ephemeral_compose_run(self):
        args = SimpleNamespace(env_file=Path("deploy/vds/.env"), compose_file=Path("deploy/vds/docker-compose.yml"))
        manifest = {
            "source_sha": "a" * 40,
            "images": {
                "backend": {"reference": "backend@example", "digest": "sha256:" + "b" * 64},
                "frontend": {"reference": "frontend@example"},
            },
        }
        responses = ["backend-container", json.dumps({"status": "pass", "zero_mutation": True})]
        with patch("tools.collect_phase27_evidence.run", side_effect=responses) as mocked:
            result = live_runtime_invariants(args, manifest)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(mocked.call_args_list[0].args[0][-3:], ["ps", "-q", "backend-api"])
        self.assertEqual(mocked.call_args_list[1].args[0][:4], ["docker", "exec", "-i", "backend-container"])
        self.assertIn("Count-only PostgreSQL invariant preflight", mocked.call_args_list[1].kwargs["input_text"])


if __name__ == "__main__":
    unittest.main()
