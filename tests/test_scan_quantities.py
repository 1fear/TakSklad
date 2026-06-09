import unittest

from backend.app.scan_quantities import scanned_blocks_for_scans
from taksklad.scan_quantities import (
    SCAN_TYPE_AGGREGATE_BOX,
    block_quantity_for_code,
    scan_code_product_key,
    scan_metadata_for_code,
    scan_product_mismatch,
    scanned_blocks_for_order_codes,
)


class ScanQuantitiesTests(unittest.TestCase):
    def test_aggregate_box_code_counts_as_fifty_blocks_in_backend_lists(self):
        codes = [
            "010400639605401221UZ1112022525522513824013040046110ZIG1218229310000",
            "UNIT-CODE-1",
        ]

        self.assertEqual(scanned_blocks_for_scans(codes), 51)

    def test_desktop_classifies_aggregate_box_code(self):
        code = "010400639605398521UZ1112032606318314924013040029410ZIG1228249310000"

        self.assertEqual(block_quantity_for_code(code), 50)
        self.assertEqual(scan_metadata_for_code(code)["scan_type"], SCAN_TYPE_AGGREGATE_BOX)

    def test_desktop_identifies_unit_kiz_product_key(self):
        self.assertEqual(scan_code_product_key("0104006396053978217KDAUbG93OVvXgs6C"), "brown")
        self.assertEqual(scan_code_product_key("0104006396053947217p-30o933ZXHZKjx"), "red")
        self.assertEqual(scan_code_product_key("010400639605400521UNIT"), "gold")

    def test_desktop_rejects_unit_kiz_for_wrong_chapman_product(self):
        self.assertTrue(
            scan_product_mismatch(
                "0104006396053947217p-30o933ZXHZKjx",
                "Chapman Gold SSL 100`20",
            )
        )
        self.assertFalse(
            scan_product_mismatch(
                "0104006396053978217KDAUbG93OVvXgs6C",
                "Chapman Brown OP 20",
            )
        )

    def test_desktop_rejects_unknown_unit_kiz_for_known_chapman_product(self):
        self.assertTrue(scan_product_mismatch("01000000000000000001", "Chapman Brown OP 20"))
        self.assertFalse(scan_product_mismatch("01000000000000000001", "Other Product"))

    def test_desktop_prefers_existing_scan_entry_quantity(self):
        order = {
            "_existing_scan_entries": [
                {
                    "code": "LEGACY-AGGREGATE",
                    "scan_type": SCAN_TYPE_AGGREGATE_BOX,
                    "block_quantity": 50,
                }
            ]
        }

        self.assertEqual(scanned_blocks_for_order_codes(order, ["LEGACY-AGGREGATE", "UNIT-CODE-1"]), 51)


if __name__ == "__main__":
    unittest.main()
