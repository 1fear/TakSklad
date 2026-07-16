import unittest
from pathlib import Path
from unittest import mock

from taksklad import backend_events, config
from taksklad.app_telegram import desktop_telegram_polling_enabled


class DesktopDbOnlyContractTests(unittest.TestCase):
    def test_legacy_mode_flags_are_hard_disabled(self):
        self.assertTrue(config.TAKSKLAD_BACKEND_ENABLED)
        self.assertTrue(config.TAKSKLAD_BACKEND_READ_ORDERS_ENABLED)
        self.assertTrue(config.TAKSKLAD_BACKEND_ONLY_REFRESH)
        self.assertFalse(config.TELEGRAM_DESKTOP_POLLING_ENABLED)
        self.assertFalse(desktop_telegram_polling_enabled({"enabled": True, "bot_token": "x"}))
        self.assertFalse(hasattr(config, "TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED"))

    def test_runtime_modules_do_not_import_removed_sheet_client(self):
        package_root = Path(__file__).parents[1] / "src" / "taksklad"
        self.assertFalse((package_root / "sheets.py").exists())
        for path in package_root.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("from .sheets", source, path.name)
            self.assertNotIn("import gspread", source, path.name)
            self.assertNotIn("oauth2client", source, path.name)

    def test_legacy_scan_queue_migrates_only_records_with_backend_item_id(self):
        legacy = [
            {
                "id": "convertible",
                "created_at": "2026-07-16T10:00:00+05:00",
                "order": {"_backend_order_item_id": "item-1"},
                "codes": ["0104006396053978TEST1"],
            },
            {"id": "blocked", "order": {"Клиент": "legacy"}, "codes": ["0104006396053978TEST2"]},
        ]
        saved = []
        queued = []

        with (
            mock.patch.object(backend_events, "load_data_section", return_value=legacy),
            mock.patch.object(
                backend_events,
                "add_pending_backend_event",
                side_effect=lambda event_type, payload: queued.append((event_type, payload)) or "event-1",
            ),
            mock.patch.object(
                backend_events,
                "save_data_section",
                side_effect=lambda section, items: saved.append((section, items)) or True,
            ),
        ):
            result = backend_events.migrate_legacy_pending_saves_to_backend_events()

        self.assertEqual(result, {"migrated": 1, "remaining": 1})
        self.assertEqual(queued[0][0], "scan")
        self.assertEqual(queued[0][1]["order_item_id"], "item-1")
        self.assertEqual(saved, [("pending_saves", [legacy[1]])])


if __name__ == "__main__":
    unittest.main()
