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
            "backend": {"enabled": True, "synced": 1, "failed": 1, "remaining": 3},
            "skladbot": {"enabled": True, "matched": 4, "not_found": 5, "multiple": 1, "errors": 0},
        }

        with (
            mock.patch.object(desktop_diagnostics, "load_pending_saves", return_value=[{}]),
            mock.patch.object(desktop_diagnostics, "load_pending_prints", return_value=[{}, {}]),
            mock.patch.object(desktop_diagnostics, "load_pending_backend_events", return_value=[{}, {}, {}]),
            mock.patch.object(desktop_diagnostics, "load_pending_telegram", return_value=[]),
        ):
            summary = desktop_diagnostics.build_refresh_diagnostic_summary(
                orders,
                {"010-secret-code", "020-secret-code"},
                sync_result=sync_result,
                source="backend",
            )
            text = desktop_diagnostics.format_refresh_diagnostic_summary(summary)

        self.assertEqual(summary["source"], "backend")
        self.assertEqual(summary["orders"], 2)
        self.assertEqual(summary["groups"], 1)
        self.assertEqual(summary["order_dates"], 2)
        self.assertEqual(summary["known_codes"], 2)
        self.assertEqual(summary["pending_prints"], 2)
        self.assertEqual(summary["pending_backend_events"], 3)
        self.assertEqual(summary["skladbot_not_found"], 5)
        self.assertNotIn("SECRET CLIENT", text)
        self.assertNotIn("SECRET ADDRESS", text)
        self.assertNotIn("010-secret-code", text)
        self.assertNotIn("020-secret-code", text)


if __name__ == "__main__":
    unittest.main()
