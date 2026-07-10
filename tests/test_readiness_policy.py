import unittest
from types import SimpleNamespace
from unittest import mock

from sqlalchemy.exc import OperationalError

from backend.app import health_service


class FailingDatabaseSession:
    bind = None

    def execute(self, _statement):
        raise OperationalError("SELECT 1", {}, RuntimeError("synthetic database unavailable"))


class ReadinessPolicyTests(unittest.TestCase):
    def test_database_failure_is_unhealthy_and_http_503(self):
        report = health_service.build_readiness_report(
            FailingDatabaseSession(),
            SimpleNamespace(service_name="test", environment="test"),
        )

        self.assertFalse(report["ready"])
        self.assertEqual(report["status"], "unhealthy")
        self.assertEqual(health_service.readiness_http_status(report), 503)

    def test_public_report_removes_detailed_errors_and_identifiers(self):
        detailed = {
            "generated_at": "2026-07-10T00:00:00+00:00",
            "ready": True,
            "status": "degraded",
            "service": "test",
            "version": "test",
            "commit_sha": "a" * 40,
            "image_digest": "sha256:" + "b" * 64,
            "environment": "test",
            "database": {"status": "ok", "dialect": "postgresql"},
            "migrations": {"status": "ok", "expected_head": "head", "current_revision": "head"},
            "queue": {"hot_path_stale_processing_count": 0, "last_errors": [{"id": "secret-id"}]},
            "google_mirror": {"status": "degraded", "role": "mirror_export", "last_errors": [{"id": "secret-id"}]},
            "imports": {"recent_errors": [{"id": "secret-id"}]},
            "policy": {"mandatory_status": "ok", "optional_status": "degraded"},
        }

        public = health_service.public_readiness_report(detailed)

        self.assertEqual(public["status"], "degraded")
        self.assertEqual(public["commit_sha"], "a" * 40)
        self.assertEqual(public["image_digest"], "sha256:" + "b" * 64)
        self.assertNotIn("last_errors", public["queue"])
        self.assertNotIn("last_errors", public["google_mirror"])
        self.assertNotIn("recent_errors", public["imports"])
        self.assertNotIn("secret-id", str(public))

    def test_multiple_migration_rows_are_not_ready(self):
        db = mock.Mock()
        db.execute.return_value.scalars.return_value.all.return_value = ["head", "other"]

        result = health_service.read_migration_status(db)

        self.assertEqual(result["status"], "multiple_revisions")
        self.assertEqual(result["current_revision"], "head,other")

    def test_health_route_does_not_build_readiness_or_touch_database(self):
        from backend.app import main

        with mock.patch.object(main, "build_readiness_report", side_effect=AssertionError("must not run")):
            payload = main.health()

        self.assertEqual(payload["status"], "ok")
        self.assertIn("commit_sha", payload)
        self.assertIn("image_digest", payload)

    def test_runtime_identity_is_exact_or_unknown(self):
        with mock.patch.dict(
            "os.environ",
            {"TAKSKLAD_COMMIT_SHA": "a" * 40, "TAKSKLAD_IMAGE_DIGEST": "sha256:" + "b" * 64},
            clear=False,
        ):
            self.assertEqual(
                health_service.runtime_build_identity(),
                {"commit_sha": "a" * 40, "image_digest": "sha256:" + "b" * 64},
            )
        with mock.patch.dict(
            "os.environ",
            {"TAKSKLAD_COMMIT_SHA": "main", "TAKSKLAD_IMAGE_DIGEST": "latest"},
            clear=False,
        ):
            self.assertEqual(
                health_service.runtime_build_identity(),
                {"commit_sha": "unknown", "image_digest": "unknown"},
            )


if __name__ == "__main__":
    unittest.main()
