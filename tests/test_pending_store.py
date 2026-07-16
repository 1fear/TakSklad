import unittest
from unittest import mock

from taksklad import pending_store


class PendingStoreTests(unittest.TestCase):
    def test_pending_print_add_reports_save_failure(self):
        with mock.patch.object(pending_store, "append_queue_item", side_effect=OSError("disk full")):
            pending_id = pending_store.add_pending_print("Address", [{"Товары": "Product"}])
        self.assertEqual(pending_id, "")

    def test_pending_print_remove_reports_save_result(self):
        with mock.patch.object(
            pending_store,
            "mutate_queue_section",
            side_effect=lambda _section, mutator: mutator([{"id": "print-1"}]),
        ):
            self.assertTrue(pending_store.remove_pending_print("print-1"))


if __name__ == "__main__":
    unittest.main()
