import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.access_policy import AUTH_PROTECTED, ROUTE_POLICIES
from backend.app.models import ImportJob, PendingEvent, WorkerHeartbeat
from backend.app.observability_metrics import (
    BoundedMetricsRegistry,
    RequestMetric,
    read_maintenance_timestamps,
    route_group,
    runtime_signal_snapshot,
)
from tools.alert_smoke import run_smoke


ROOT = Path(__file__).resolve().parents[1]


class OperationalObservabilityTests(unittest.TestCase):
    def test_request_metrics_have_bounded_labels_and_histogram(self):
        registry = BoundedMetricsRegistry()
        registry.observe_request(RequestMetric("TRACE", "orders/real-id", "unexpected", 900))

        output = registry.render(
            db_pool={"checked_out": 2, "checked_in": 3, "size": 5},
            runtime={"queue_age": 7, "queue_pickup": 2.5, "provider_failures": 1},
        )

        self.assertIn('method="OTHER",route_group="other",outcome="server_error"', output)
        self.assertIn('le="+Inf"} 1', output)
        self.assertIn("taksklad_db_pool_size 5", output)
        self.assertIn("taksklad_readiness 0", output)
        self.assertIn("taksklad_http_5xx_ratio 1.0", output)
        self.assertIn("taksklad_http_p95_seconds 300.0", output)
        self.assertIn("taksklad_runtime_identity_valid 0", output)
        self.assertNotIn("orders/real-id", output)

    def test_runtime_snapshot_uses_bounded_database_state(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        for table in (ImportJob.__table__, PendingEvent.__table__, WorkerHeartbeat.__table__):
            table.create(engine)
        now = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
        with Session(engine) as db:
            db.add_all([
                PendingEvent(
                    event_type="smartup_synthetic",
                    status="failed",
                    payload={},
                    created_at=now - timedelta(seconds=120),
                    updated_at=now - timedelta(seconds=100),
                ),
                PendingEvent(
                    event_type="google_synthetic",
                    status="pending",
                    payload={},
                    created_at=now - timedelta(seconds=90),
                    updated_at=now - timedelta(seconds=90),
                ),
                PendingEvent(
                    event_type="telegram_synthetic",
                    status="processing",
                    payload={},
                    created_at=now - timedelta(seconds=50),
                    updated_at=now - timedelta(seconds=40),
                ),
                PendingEvent(
                    event_type="telegram_completed",
                    status="completed",
                    payload={},
                    created_at=now - timedelta(seconds=300),
                    updated_at=now - timedelta(seconds=5),
                ),
                ImportJob(
                    source="synthetic",
                    status="completed",
                    rows_total=1,
                    rows_imported=1,
                    raw_payload={},
                    created_at=now - timedelta(seconds=30),
                ),
                WorkerHeartbeat(
                    worker_name="skladbot",
                    interval_seconds=60,
                    grace_seconds=15,
                    status="success",
                    correlation_id="00000000-0000-4000-8000-000000000001",
                    last_cycle_started_at=now - timedelta(seconds=10),
                    last_success_at=now - timedelta(seconds=9),
                ),
            ])
            db.commit()
            snapshot = runtime_signal_snapshot(db, now=now)

        self.assertEqual(snapshot["queue_age"], 90)
        self.assertEqual(snapshot["import_age"], 30)
        self.assertEqual(snapshot["provider_failures"], 1)
        self.assertEqual(snapshot["queue_pickup"], 10)
        self.assertEqual(snapshot["workers"], {"skladbot": 10})

    def test_maintenance_freshness_is_recorded_and_missing_is_not_false_green(self):
        registry = BoundedMetricsRegistry()
        missing = registry.render()
        missing_age = int(
            next(line.rsplit(" ", 1)[1] for line in missing.splitlines() if line.startswith("taksklad_backup_last"))
        )
        self.assertGreater(missing_age, 86400)

        registry.record_maintenance_success("backup", datetime.now(timezone.utc) - timedelta(seconds=15))
        recorded = registry.render()
        recorded_age = int(
            next(line.rsplit(" ", 1)[1] for line in recorded.splitlines() if line.startswith("taksklad_backup_last"))
        )
        self.assertLessEqual(recorded_age, 16)

    def test_maintenance_marker_reads_only_approved_timestamps(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "maintenance.json"
            path.write_text(json.dumps({
                "backup_success_at": "2026-07-11T11:00:00Z",
                "restore_drill_success_at": "2026-07-10T11:00:00+00:00",
                "ignored_identifier": "synthetic-should-not-propagate",
            }), encoding="utf-8")

            result = read_maintenance_timestamps(path)

        self.assertEqual(set(result), {"backup", "restore_drill"})
        self.assertEqual(result["backup"].tzinfo, timezone.utc)

    def test_metrics_route_is_private_and_sensitive(self):
        policy = ROUTE_POLICIES[("GET", "/api/v1/admin/metrics")]

        self.assertEqual(policy.authentication, AUTH_PROTECTED)
        self.assertEqual(policy.service_scope, "diagnostics:read")
        self.assertTrue(policy.sensitive)
        self.assertEqual(route_group("/api/v1/admin/metrics"), "metrics")

    def test_metrics_route_wires_actual_readiness_and_bounded_identity_validity(self):
        from backend.app import main

        identity = {"commit_sha": "a" * 40, "image_digest": "sha256:" + "b" * 64}
        with (
            mock.patch.object(main, "runtime_signal_snapshot", return_value={}),
            mock.patch.object(main, "build_readiness_report", return_value={"ready": True}),
            mock.patch.object(main, "runtime_build_identity", return_value=identity),
            mock.patch.object(main, "db_pool_snapshot", return_value={}),
            mock.patch.object(main, "read_maintenance_timestamps", return_value={}),
        ):
            response = main.admin_operational_metrics(db=mock.Mock())

        output = response.body.decode("utf-8")
        self.assertIn("taksklad_readiness 1", output)
        self.assertIn("taksklad_runtime_identity_valid 1", output)
        self.assertNotIn("a" * 40, output)
        self.assertNotIn("sha256:", output)

    def test_dashboard_and_alert_catalog_cover_phase_contract(self):
        dashboard = json.loads(
            (ROOT / "monitoring/observability/dashboard.json").read_text(encoding="utf-8")
        )
        queries = " ".join(panel["query"] for panel in dashboard["panels"])
        for expected in (
            "histogram_quantile(0.50",
            "histogram_quantile(0.95",
            "histogram_quantile(0.99",
            "server_error",
            "queue_pickup",
            "queue_oldest",
            "db_pool",
            "backup_last_success",
        ):
            self.assertIn(expected, queries)
        smoke = run_smoke(300)
        self.assertEqual(smoke["alerts"], 8)
        self.assertEqual(smoke["firing"], smoke["resolved"])
        self.assertLessEqual(smoke["maximum_raise_seconds"], 300)
        self.assertLessEqual(smoke["maximum_recovery_seconds"], 1)
        self.assertLessEqual(smoke["observed_elapsed_seconds"], 300)
        self.assertLessEqual(smoke["observed_first_monotonic"], smoke["observed_last_monotonic"])
        self.assertEqual(smoke["external_sends"], 0)

    def test_mandatory_tools_pass(self):
        commands = (
            [sys.executable, "tools/audit_metric_labels.py", "--strict"],
            [str(ROOT / "tools/run_alert_smoke.sh"), "--synthetic-only", "--timeout-seconds", "300"],
            [str(ROOT / "tools/check_runtime_identity.py"), "--local-stack"],
        )
        for command in commands:
            with self.subTest(command=command[0]):
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=360 if command[-1] == "--local-stack" else 60,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stdout)


if __name__ == "__main__":
    unittest.main()
