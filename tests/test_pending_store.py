import unittest
from unittest import mock

from taksklad import pending_store


class PendingStoreTests(unittest.TestCase):
    def test_sync_pending_saves_drops_non_retryable_missing_row(self):
        pending = [{
            "id": "save-1",
            "order": {"Клиент": "Client"},
            "codes": ["01000000000000000001"],
            "last_error": "",
        }]
        saved = []

        with (
            mock.patch.object(pending_store, "load_pending_saves", return_value=pending),
            mock.patch.object(pending_store, "save_pending_saves", side_effect=lambda value: saved.append(value)),
            mock.patch.object(
                pending_store,
                "update_scanned_codes_to_gsheet",
                return_value=(False, "Не найдена строка заказа для записи кодов"),
            ),
            mock.patch.object(pending_store, "write_scan_backup", return_value=True),
        ):
            result = pending_store.sync_pending_saves(sheet=object())

        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(saved, [[]])

    def test_sync_pending_saves_keeps_retryable_quota_error(self):
        pending = [{
            "id": "save-1",
            "order": {"Клиент": "Client"},
            "codes": ["01000000000000000001"],
            "last_error": "",
        }]
        saved = []

        with (
            mock.patch.object(pending_store, "load_pending_saves", return_value=pending),
            mock.patch.object(pending_store, "save_pending_saves", side_effect=lambda value: saved.append(value)),
            mock.patch.object(
                pending_store,
                "update_scanned_codes_to_gsheet",
                return_value=(False, "APIError: [429]: quota exceeded"),
            ),
        ):
            result = pending_store.sync_pending_saves(sheet=object())

        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["dropped"], 0)
        self.assertEqual(result["remaining"], 1)
        self.assertEqual(saved[0][0]["last_error"], "APIError: [429]: quota exceeded")


if __name__ == "__main__":
    unittest.main()
