import unittest
from unittest import mock

from taksklad import pending_store


class DesktopPendingStoreTests(unittest.TestCase):
    def test_undo_updates_pending_save_codes_for_active_position(self):
        order = {
            "ID заказа": "order-1",
            "ID импорта": "import-1",
            "_row_number": 2,
            "Дата отгрузки": "29.05.2026",
            "Тип оплаты": "Перечисление",
            "Клиент": "Client",
            "Адрес": "Address",
            "Товары": "Product",
        }
        pending = [{
            "id": pending_store.make_pending_save_id(
                order,
                ["01012345678901234567A", "01012345678901234567B"],
            ),
            "order": order.copy(),
            "codes": ["01012345678901234567A", "01012345678901234567B"],
            "last_error": "timeout",
        }]
        saved = []

        with (
            mock.patch.object(pending_store, "load_pending_saves", return_value=pending),
            mock.patch.object(pending_store, "save_pending_saves", side_effect=lambda value: saved.append(value)),
        ):
            updated = pending_store.update_pending_save_codes_for_undo(
                order,
                ["01012345678901234567A", "01012345678901234567B"],
                ["01012345678901234567A"],
                "undo",
            )

        self.assertTrue(updated)
        self.assertEqual(saved[0][0]["codes"], ["01012345678901234567A"])
        self.assertEqual(saved[0][0]["last_error"], "undo")

    def test_undo_removes_pending_save_when_no_codes_left(self):
        order = {"ID заказа": "order-1", "ID импорта": "import-1", "_row_number": 2}
        pending = [{
            "id": pending_store.make_pending_save_id(order, ["01012345678901234567A"]),
            "order": order.copy(),
            "codes": ["01012345678901234567A"],
            "last_error": "timeout",
        }]
        saved = []

        with (
            mock.patch.object(pending_store, "load_pending_saves", return_value=pending),
            mock.patch.object(pending_store, "save_pending_saves", side_effect=lambda value: saved.append(value)),
        ):
            updated = pending_store.update_pending_save_codes_for_undo(
                order,
                ["01012345678901234567A"],
                [],
                "undo",
            )

        self.assertTrue(updated)
        self.assertEqual(saved, [[]])

    def test_undo_updates_only_matching_pending_save(self):
        target_order = {"ID заказа": "order-1", "ID импорта": "import-1", "_row_number": 2}
        other_order = {"ID заказа": "order-2", "ID импорта": "import-2", "_row_number": 3}
        target_codes = ["01012345678901234567A", "01012345678901234567B"]
        other_codes = ["01012345678901234567C", "01012345678901234567D"]
        pending = [
            {
                "id": pending_store.make_pending_save_id(target_order, target_codes),
                "order": target_order.copy(),
                "codes": target_codes.copy(),
                "last_error": "timeout",
            },
            {
                "id": pending_store.make_pending_save_id(other_order, other_codes),
                "order": other_order.copy(),
                "codes": other_codes.copy(),
                "last_error": "other timeout",
            },
        ]
        saved = []

        with (
            mock.patch.object(pending_store, "load_pending_saves", return_value=pending),
            mock.patch.object(pending_store, "save_pending_saves", side_effect=lambda value: saved.append(value)),
        ):
            updated = pending_store.update_pending_save_codes_for_undo(
                target_order,
                target_codes,
                ["01012345678901234567A"],
                "undo",
            )

        self.assertTrue(updated)
        self.assertEqual(saved[0][0]["codes"], ["01012345678901234567A"])
        self.assertEqual(saved[0][1]["id"], pending[1]["id"])
        self.assertEqual(saved[0][1]["codes"], other_codes)
        self.assertEqual(saved[0][1]["last_error"], "other timeout")


if __name__ == "__main__":
    unittest.main()
