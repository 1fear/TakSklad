import unittest
from unittest import mock

from taksklad import desktop_diagnostics


class DesktopDiagnosticsTests(unittest.TestCase):
    def test_refresh_diagnostic_summary_uses_counts_without_payload_values(self):
        orders = [
            {
                "Дата отгрузки": "01.06.2026",
                "Клиент": "SECRET CLIENT",
                "Тип оплаты": "Терминал",
                "Адрес": "SECRET ADDRESS",
                "Товары": "Chapman Brown OP 20",
                "Отсканированные коды": "010-secret-code",
            },
            {
                "Дата отгрузки": "02.06.2026",
                "Клиент": "SECRET CLIENT",
                "Тип оплаты": "Терминал",
                "Адрес": "SECRET ADDRESS",
                "Товары": "Chapman Red SSL 20",
            },
        ]
        sync_result = {
            "synced": 1,
            "failed": 0,
            "remaining": 2,
            "primary_source": "google_emergency_fallback",
            "backend_only_refresh": True,
            "emergency_google_fallback": True,
            "backend": {"enabled": True, "synced": 1, "failed": 1, "remaining": 3},
            "google_sheets_pending": {
                "status": "completed_with_errors",
                "synced": 2,
                "failed": 1,
                "remaining": 4,
                "error": "Bearer SECRET TOKEN",
            },
            "skladbot": {"enabled": True, "matched": 4, "not_found": 5, "multiple": 1, "errors": 0},
        }

        with (
            mock.patch.object(desktop_diagnostics, "load_pending_saves", return_value=[{}]),
            mock.patch.object(desktop_diagnostics, "load_pending_prints", return_value=[{}, {}]),
            mock.patch.object(
                desktop_diagnostics,
                "load_pending_backend_events",
                return_value=[
                    {
                        "type": "scan",
                        "attempts": 0,
                        "last_error": "",
                        "payload": {"code": "010-secret-code"},
                    },
                    {
                        "type": "scan",
                        "attempts": 2,
                        "last_error": "timeout with SECRET TOKEN",
                        "payload": {"code": "020-secret-code"},
                    },
                    {
                        "type": "order_complete",
                        "attempts": 1,
                        "last_error": "Backend HTTP 504",
                        "payload": {"order_id": "secret-order"},
                    },
                    {
                        "type": "unexpected",
                        "attempts": "3",
                        "last_error": "",
                        "payload": {"Authorization": "Bearer secret"},
                    },
                ],
            ),
            mock.patch.object(desktop_diagnostics, "load_pending_telegram", return_value=[]),
        ):
            summary = desktop_diagnostics.build_refresh_diagnostic_summary(
                orders,
                {"010-secret-code", "020-secret-code"},
                sync_result=sync_result,
                source="backend",
            )
            text = desktop_diagnostics.format_refresh_diagnostic_summary(summary)

        self.assertEqual(summary["source"], "google_emergency_fallback")
        self.assertEqual(summary["primary_source"], "google_emergency_fallback")
        self.assertTrue(summary["backend_only_refresh"])
        self.assertTrue(summary["emergency_google_fallback"])
        self.assertEqual(summary["orders"], 2)
        self.assertEqual(summary["groups"], 1)
        self.assertEqual(summary["order_dates"], 2)
        self.assertEqual(summary["known_codes"], 2)
        self.assertEqual(summary["pending_prints"], 2)
        self.assertEqual(summary["pending_backend_events"], 4)
        self.assertEqual(summary["pending_backend_scan_events"], 2)
        self.assertEqual(summary["pending_backend_order_complete_events"], 1)
        self.assertEqual(summary["pending_backend_other_events"], 1)
        self.assertEqual(summary["pending_backend_failed_events"], 2)
        self.assertEqual(summary["pending_backend_attempted_events"], 3)
        self.assertEqual(summary["pending_backend_max_attempts"], 3)
        self.assertEqual(summary["google_mirror_status"], "completed_with_errors")
        self.assertEqual(summary["google_mirror_synced_exports"], 2)
        self.assertEqual(summary["google_mirror_failed_exports"], 1)
        self.assertEqual(summary["google_mirror_pending_exports"], 4)
        self.assertEqual(summary["skladbot_not_found"], 5)
        self.assertIn("source=google_emergency_fallback", text)
        self.assertIn("backend_only_refresh=True", text)
        self.assertIn("emergency_google_fallback=True", text)
        self.assertIn("google_mirror_pending_exports=4", text)
        self.assertNotIn("SECRET CLIENT", text)
        self.assertNotIn("SECRET ADDRESS", text)
        self.assertNotIn("010-secret-code", text)
        self.assertNotIn("020-secret-code", text)
        self.assertNotIn("SECRET TOKEN", text)
        self.assertNotIn("secret-order", text)
        self.assertNotIn("Authorization", text)


if __name__ == "__main__":
    unittest.main()
