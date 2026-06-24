import unittest

from backend.app.scan_quantities import (
    AGGREGATE_BOX_PRODUCT_PREFIXES as BACKEND_AGGREGATE_BOX_PRODUCT_PREFIXES,
    scanned_blocks_for_scans,
)
from taksklad.scan_quantities import (
    AGGREGATE_BOX_PRODUCT_PREFIXES,
    SCAN_TYPE_AGGREGATE_BOX,
    aggregate_product_mismatch,
    block_quantity_for_code,
    product_key_from_name,
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
        self.assertEqual(scan_code_product_key("0104006396053978217KDAUbG93OVvXgs6C"), "brown:op")
        self.assertEqual(scan_code_product_key("0104006396053947217p-30o933ZXHZKjx"), "red:op")
        self.assertEqual(scan_code_product_key("010400639605400521UNIT"), "gold:ssl")
        self.assertEqual(scan_code_product_key("0104006396054067217KDAUbG93OVvXgs6C"), "brown:ssl")
        self.assertEqual(scan_code_product_key("0104006396054036217p-30o933ZXHZKjx"), "red:ssl")
        self.assertEqual(scan_code_product_key("0104006396104441217GREEN"), "green:op")

    def test_desktop_identifies_aggregate_box_product_key(self):
        self.assertEqual(scan_code_product_key("010400639605407421BOX"), "brown:ssl")
        self.assertEqual(scan_code_product_key("010400639605404321BOX"), "red:ssl")
        self.assertEqual(scan_code_product_key("010400639610444821BOX"), "green:op")
        self.assertEqual(scan_code_product_key("010400639610445821BOX"), "green:op")

    def test_aggregate_box_detection_uses_box_gtin_not_next_ai(self):
        cases = [
            ("Chapman Brown OP 20", "010400639605398510BATCH21BOX"),
            ("Chapman RED OP 20", "01040063960539541726062510BATCH"),
            ("Chapman Gold SSL 100`20", "010400639605401221BOX"),
            ("Chapman Brown SSL 100`20", "010400639605407410BATCH21BOX"),
            ("Chapman RED SSL 100 20", "01040063960540431726062510BATCH"),
            ("Chapman Green OP 20", "010400639610444810BATCH21BOX"),
            ("Chapman Green OP 20", "010400639610445821UZ1112042611906223124013040030510ZIG1233389310000"),
        ]

        for product, code in cases:
            with self.subTest(product=product, code=code):
                product_key = product_key_from_name(product)
                self.assertTrue(product_key)
                self.assertEqual(scan_code_product_key(code), product_key)
                self.assertEqual(block_quantity_for_code(code), 50)
                self.assertEqual(scan_metadata_for_code(code)["scan_type"], SCAN_TYPE_AGGREGATE_BOX)
                self.assertFalse(scan_product_mismatch(code, product))
                self.assertFalse(aggregate_product_mismatch(code, product))

    def test_desktop_and_backend_aggregate_box_prefixes_match(self):
        self.assertEqual(AGGREGATE_BOX_PRODUCT_PREFIXES, BACKEND_AGGREGATE_BOX_PRODUCT_PREFIXES)

    def test_desktop_rejects_unit_kiz_for_wrong_chapman_product(self):
        self.assertTrue(
            scan_product_mismatch(
                "0104006396053947217p-30o933ZXHZKjx",
                "Chapman Gold SSL 100`20",
            )
        )
        self.assertTrue(
            scan_product_mismatch(
                "0104006396053978217KDAUbG93OVvXgs6C",
                "Chapman Brown SSL 100`20",
            )
        )
        self.assertTrue(
            scan_product_mismatch(
                "010400639605404321BOX",
                "Chapman RED OP 20",
            )
        )
        self.assertFalse(
            scan_product_mismatch(
                "0104006396053978217KDAUbG93OVvXgs6C",
                "Chapman Brown OP 20",
            )
        )
        self.assertFalse(
            scan_product_mismatch(
                "0104006396104441217GREEN",
                "Chapman Green OP 20",
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
