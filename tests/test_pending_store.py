import unittest
from unittest import mock

from taksklad import pending_store


class PendingStoreTests(unittest.TestCase):
    def test_pending_print_add_reports_save_failure(self):
        with mock.patch.object(
            pending_store,
            "append_queue_item",
            side_effect=OSError("disk unavailable"),
        ) as append_pending:
            pending_id = pending_store.add_pending_print(
                "Tashkent",
                [{"Клиент": "Client", "Адрес": "Tashkent", "Товары": "Product", "Коды": ["0101"]}],
            )

        self.assertEqual(pending_id, "")
        append_pending.assert_called_once()

    def test_pending_print_remove_reports_save_result(self):
        pending = [{"id": "print-1", "address": "Tashkent", "products": []}]

        with mock.patch.object(
            pending_store,
            "mutate_queue_section",
            side_effect=OSError("disk unavailable"),
        ) as mutate_pending:
            removed = pending_store.remove_pending_print("print-1")

        self.assertFalse(removed)
        mutate_pending.assert_called_once()
        self.assertFalse(pending_store.remove_pending_print(""))

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
            mock.patch.object(
                pending_store,
                "reconcile_queue_section",
                side_effect=lambda _section, _snapshot, remaining: saved.append(remaining) or remaining,
            ),
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
            mock.patch.object(
                pending_store,
                "reconcile_queue_section",
                side_effect=lambda _section, _snapshot, remaining: saved.append(remaining) or remaining,
            ),
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
