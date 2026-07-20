import unittest

from tools.verify_telegram_worker_repair_preflight import (
    RepairPreflightBlocked,
    REQUIRED_SERVICES,
    verify,
)


def ready_payload():
    return {
        "http_status": 503,
        "payload": {
            "ready": False,
            "database": {"status": "ok"},
            "migrations": {"status": "ok"},
            "queue": {
                "hot_path_stale_processing_count": 0,
                "hot_path_blocking_count": 0,
                "hot_path_error_count": 0,
            },
            "imports": {"recent_error_count": 0},
            "workers": {"unhealthy_count": 1, "missing_count": 0},
            "daily_report": {"status": "ok"},
            "desktop_pairing": {"status": "ok"},
        },
    }


def compose_rows():
    return [
        {
            "Service": service,
            "State": "running",
            "Health": "unhealthy" if service == "telegram-worker" else "healthy",
        }
        for service in sorted(REQUIRED_SERVICES)
    ]


class TelegramWorkerRepairPreflightTests(unittest.TestCase):
    def test_accepts_only_one_telegram_worker_failure(self):
        result = verify(ready_payload(), compose_rows())
        self.assertEqual(result["status"], "repairable")
        self.assertEqual(result["unhealthy_service"], "telegram-worker")
        self.assertTrue(result["values_redacted"])

    def test_rejects_queue_or_import_blockers(self):
        for path in ("queue", "imports"):
            with self.subTest(path=path):
                ready = ready_payload()
                if path == "queue":
                    ready["payload"]["queue"]["hot_path_blocking_count"] = 1
                else:
                    ready["payload"]["imports"]["recent_error_count"] = 1
                with self.assertRaises(RepairPreflightBlocked):
                    verify(ready, compose_rows())

    def test_rejects_an_unrelated_unhealthy_service(self):
        rows = compose_rows()
        for row in rows:
            if row["Service"] == "backend-api":
                row["Health"] = "unhealthy"
        with self.assertRaisesRegex(RepairPreflightBlocked, "unrelated_service_unhealthy"):
            verify(ready_payload(), rows)
