import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from tools.production_release_checks import (
    ProductionCheckError,
    validate_live,
    validate_preflight,
)


SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


def manifest():
    return {
        "source_sha": SHA,
        "images": {"backend": {"digest": DIGEST}},
        "windows": {"version": "2.0.26"},
    }


class ProductionReleaseChecksTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)

    def preflight(self):
        return {
            "schema_version": 1,
            "mode": "production-preflight",
            "source_sha": SHA,
            "read_only": True,
            "external_sends": 0,
            "data_mutations": 0,
            "restore_executed": False,
            "schema_downgrade": False,
            "backup": {
                "backup_id": "taksklad-postgres-safe",
                "sha256": "c" * 64,
                "created_at_utc": (self.now - timedelta(minutes=5)).isoformat(),
                "validated": True,
                "atomic_bundle": True,
                "format": "postgresql-custom",
            },
            "restore_drill": {
                "status": "pass",
                "isolated": True,
                "completed_at_utc": (self.now - timedelta(days=1)).isoformat(),
            },
            "migration": {
                "current_revision": "old",
                "expected_current_revision": "old",
                "target_revision": "new",
                "blockers": 0,
                "read_only": True,
                "apply_executed": False,
                "observed_seconds": 0.4,
                "runtime_budget_seconds": 120,
            },
            "invariants": {"violations": 0, "zero_mutation": True, "automatic_repairs": 0},
            "config": {"blockers": 0},
            "readiness": {"database_status": "ok"},
            "blockers": 0,
            "active_duplicates": 0,
            "lost_outbox": 0,
            "stale_release_blockers": 0,
        }

    def live(self):
        return {
            "schema_version": 1,
            "mode": "live-release-verification",
            "source_sha": SHA,
            "read_only": True,
            "external_sends": 0,
            "data_mutations": 0,
            "restore_executed": False,
            "schema_downgrade": False,
            "runtime": {"source_sha": SHA, "backend_digest": DIGEST, "version": "2.0.26"},
            "health": {"status": "ok", "http_status": 200},
            "readiness": {
                "ready": True,
                "http_status": 200,
                "database_status": "ok",
                "migration_status": "ok",
                "worker_status": "ok",
                "mandatory_status": "ok",
            },
            "queue_blockers": 0,
            "stale_processing": 0,
            "active_duplicates": 0,
            "lost_outbox": 0,
            "stale_release_blockers": 0,
            "alerts": {"firing_mandatory": 0},
            "slo": {
                "status": "pass",
                "duration_seconds": 300,
                "samples": 30,
                "errors": 0,
                "latency_p95_ms": 120,
                "latency_budget_ms": 500,
            },
        }

    def test_preflight_accepts_fresh_count_only_zero_blocker_evidence(self):
        result = validate_preflight(
            self.preflight(), manifest(), require_current_backup=True,
            require_zero_blockers=True, now=self.now,
            max_backup_age_hours=24, max_restore_drill_age_hours=192,
        )
        self.assertEqual(result["blockers"], 0)

    def test_preflight_rejects_stale_backup_and_any_violation(self):
        evidence = self.preflight()
        evidence["backup"]["created_at_utc"] = (self.now - timedelta(hours=25)).isoformat()
        with self.assertRaises(ProductionCheckError):
            validate_preflight(
                evidence, manifest(), require_current_backup=True,
                require_zero_blockers=True, now=self.now,
                max_backup_age_hours=24, max_restore_drill_age_hours=192,
            )
        evidence = self.preflight()
        evidence["invariants"]["violations"] = 1
        with self.assertRaises(ProductionCheckError):
            validate_preflight(
                evidence, manifest(), require_current_backup=True,
                require_zero_blockers=True, now=self.now,
                max_backup_age_hours=24, max_restore_drill_age_hours=192,
            )

    def test_live_accepts_exact_identity_and_complete_slo_window(self):
        result = validate_live(self.live(), manifest(), require_same_sha=True, require_slo_window=True)
        self.assertEqual(result["source_sha"], SHA)
        self.assertEqual(result["slo_samples"], 30)

    def test_live_rejects_identity_worker_and_slo_failures(self):
        for mutate in (
            lambda value: value["runtime"].update(source_sha="d" * 40),
            lambda value: value["readiness"].update(worker_status="unhealthy"),
            lambda value: value["slo"].update(duration_seconds=299),
        ):
            evidence = self.live()
            mutate(evidence)
            with self.assertRaises(ProductionCheckError):
                validate_live(evidence, manifest(), require_same_sha=True, require_slo_window=True)


if __name__ == "__main__":
    unittest.main()
