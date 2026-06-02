import unittest

from pydantic import ValidationError

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


if __name__ == "__main__":
    unittest.main()
