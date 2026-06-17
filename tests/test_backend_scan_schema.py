import unittest
from unittest import mock

from pydantic import ValidationError

from backend.app.kiz_movements_service import advisory_lock_keys, lock_kiz_code_for_transaction
from backend.app.schemas import ScanCreate


class BackendScanSchemaTests(unittest.TestCase):
    def test_scan_create_accepts_gs1_group_separator(self):
        payload = ScanCreate(
            order_item_id="item-1",
            code="01012345678901234567\x1dABC123",
        )

        self.assertEqual(payload.code, "01012345678901234567\x1dABC123")

    def test_scan_create_rejects_line_break_inside_code(self):
        with self.assertRaises(ValidationError):
            ScanCreate(order_item_id="item-1", code="01012345678901234567\nABC123")

    def test_postgres_kiz_lock_uses_stable_advisory_keys(self):
        db = mock.Mock()
        db.bind.dialect.name = "postgresql"

        locked = lock_kiz_code_for_transaction(db, " 0104006396053947217ABC ")

        self.assertTrue(locked)
        expected_keys = advisory_lock_keys("0104006396053947217ABC")
        self.assertEqual(db.execute.call_args.args[1], {"first": expected_keys[0], "second": expected_keys[1]})

    def test_non_postgres_kiz_lock_is_noop(self):
        db = mock.Mock()
        db.bind.dialect.name = "sqlite"

        locked = lock_kiz_code_for_transaction(db, "0104006396053947217ABC")

        self.assertFalse(locked)
        db.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
