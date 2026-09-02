import unittest

from backend.app.kiz_format import kiz_format_violation
from backend.app.scan_quantities import (
    AGGREGATE_BOX_PRODUCT_PREFIXES as BACKEND_AGGREGATE_BOX_PRODUCT_PREFIXES,
    UNIT_PRODUCT_PREFIXES as BACKEND_UNIT_PRODUCT_PREFIXES,
    product_key_from_name as backend_product_key_from_name,
    scan_code_product_key as backend_scan_code_product_key,
    scanned_blocks_for_scans,
)
from backend.app.skladbot_contracts import product_sku_key as backend_product_sku_key
from taksklad.skladbot import product_sku_key as desktop_product_sku_key
from taksklad.scan_quantities import (
    AGGREGATE_BOX_PRODUCT_PREFIXES,
    SCAN_TYPE_AGGREGATE_BOX,
    UNIT_PRODUCT_PREFIXES,
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
        self.assertEqual(scan_code_product_key("0104006396053947217p-30o933ZXHZKjxX"), "red:op")
        self.assertEqual(scan_code_product_key("010400639605400521UNITXXXXXXXXXXXXX"), "gold:ssl")
        self.assertEqual(scan_code_product_key("0104006396054067217KDAUbG93OVvXgs6C"), "brown:ssl")
        self.assertEqual(scan_code_product_key("0104006396054036217p-30o933ZXHZKjxX"), "red:ssl")
        self.assertEqual(scan_code_product_key("0104006396104441217GREENXXXXXXXXXXX"), "green:op")

    def test_desktop_identifies_aggregate_box_product_key(self):
        self.assertEqual(scan_code_product_key("010400639605407421BOXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"), "brown:ssl")
        self.assertEqual(scan_code_product_key("010400639605404321BOXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"), "red:ssl")
        self.assertEqual(scan_code_product_key("010400639610444821BOXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"), "green:op")
        self.assertEqual(scan_code_product_key("010400639610445821BOXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"), "green:op")

    def test_aggregate_box_detection_uses_box_gtin_not_next_ai(self):
        cases = [
            ("Chapman Brown OP 20", "010400639605398510BATCH21BOXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"),
            ("Chapman RED OP 20", "01040063960539541726062510BATCHXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"),
            ("Chapman Gold SSL 100`20", "010400639605401221BOXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"),
            ("Chapman Brown SSL 100`20", "010400639605407410BATCH21BOXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"),
            ("Chapman Brown SSL 100`20", "010400639605407421UZ1112022612417151624013040046310ZIG1231569310000"),
            ("Chapman Brown SSL 100`20", "010400639605407421UZ1112022612416594224013040046310ZIG1231569310000"),
            ("Chapman RED SSL 100 20", "01040063960540431726062510BATCHXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"),
            ("Chapman Green OP 20", "010400639610444810BATCH21BOXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"),
            ("Chapman Green OP 20", "010400639610445821UZ1112042611905354024013040030510ZIG1233389310000"),
            ("Chapman Green OP 20", "010400639610445821UZ1112042611909232924013040030510ZIG1233389310000"),
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
                "0104006396053947217p-30o933ZXHZKjxX",
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
                "010400639605404321BOXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
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
                "0104006396104441217GREENXXXXXXXXXXX",
                "Chapman Green OP 20",
            )
        )

    def test_desktop_rejects_unknown_unit_kiz_for_known_chapman_product(self):
        self.assertTrue(scan_product_mismatch("01000000000000000001XXXXXXXXXXXXXXX", "Chapman Brown OP 20"))
        self.assertFalse(scan_product_mismatch("01000000000000000001XXXXXXXXXXXXXXX", "Other Product"))

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


    # KSSL (King Size Super Slim) приехал поставкой 2026-09-03 с новыми GTIN.
    # Имя товара должно давать отдельный ключ, а не сливаться с SSL 100,
    # иначе SKU-защита пропустит блок KSSL в позицию SSL и наоборот.
    KSSL_BROWN_UNIT = "0104006396104199217BROWNKSSLXXXXXXX"
    KSSL_GREEN_UNIT = "0104006396104229217GREENKSSLXXXXXXX"

    def test_desktop_and_backend_unit_prefixes_match(self):
        self.assertEqual(UNIT_PRODUCT_PREFIXES, BACKEND_UNIT_PRODUCT_PREFIXES)

    def test_kssl_unit_kiz_resolves_to_its_own_product_key(self):
        for code, expected in (
            (self.KSSL_BROWN_UNIT, "brown:kssl"),
            (self.KSSL_GREEN_UNIT, "green:kssl"),
        ):
            with self.subTest(code=code):
                self.assertEqual(scan_code_product_key(code), expected)
                self.assertEqual(backend_scan_code_product_key(code), expected)
                self.assertEqual(block_quantity_for_code(code), 1)

    def test_kssl_names_resolve_the_same_in_all_four_parsers(self):
        cases = (
            ("Chapman Brown KSSL 20", "brown:kssl"),
            ("Chapman Green KSSL 20", "green:kssl"),
            ("Chapman Brown KSSL 20 UZ - KingSize SuperSlim", "brown:kssl"),
            ("chapman green kssl", "green:kssl"),
            ("ChapmanBrownKSSL20", "brown:kssl"),
        )
        for name, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(product_key_from_name(name), expected)
                self.assertEqual(backend_product_key_from_name(name), expected)
                self.assertEqual(desktop_product_sku_key(name), expected)
                self.assertEqual(backend_product_sku_key(name), expected)

    def test_kssl_format_does_not_hijack_existing_names(self):
        cases = (
            ("Chapman Brown SSL 100`20", "brown:ssl"),
            ("Chapman Gold SSL 100`20", "gold:ssl"),
            ("Chapman RED SSL 100 20", "red:ssl"),
            ("Chapman Brown OP 20", "brown:op"),
            ("Chapman Green OP 20 UZ - KingSize", "green:op"),
            ("Chapman Brown SSL 20 UZ - SuperSlim", "brown:ssl"),
        )
        for name, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(product_key_from_name(name), expected)
                self.assertEqual(backend_product_key_from_name(name), expected)
                self.assertEqual(desktop_product_sku_key(name), expected)
                self.assertEqual(backend_product_sku_key(name), expected)

    def test_kssl_unit_kiz_is_guarded_against_ssl_and_op_positions(self):
        self.assertTrue(scan_product_mismatch(self.KSSL_BROWN_UNIT, "Chapman Brown SSL 100`20"))
        self.assertTrue(scan_product_mismatch(self.KSSL_BROWN_UNIT, "Chapman Brown OP 20"))
        self.assertTrue(scan_product_mismatch("0104006396054067217KDAUbG93OVvXgs6C", "Chapman Brown KSSL 20"))
        self.assertTrue(scan_product_mismatch(self.KSSL_GREEN_UNIT, "Chapman Green OP 20"))
        self.assertTrue(scan_product_mismatch("0104006396104441217GREENXXXXXXXXXXX", "Chapman Green KSSL 20"))
        self.assertFalse(scan_product_mismatch(self.KSSL_BROWN_UNIT, "Chapman Brown KSSL 20"))
        self.assertFalse(scan_product_mismatch(self.KSSL_GREEN_UNIT, "Chapman Green KSSL 20"))

    def test_backend_rejects_box_length_code_with_kssl_unit_gtin(self):
        # Штучный GTIN известен, значит код коробочной длины с ним это не короб,
        # а склейка или обрезок: backend отбивает его до подсчёта блоков.
        self.assertEqual(kiz_format_violation(self.KSSL_BROWN_UNIT), "")
        box_length_with_unit_gtin = "010400639610419921UZ1112022525522513824013040046110ZIG1218229310000"
        self.assertEqual(len(box_length_with_unit_gtin), 67)
        self.assertEqual(kiz_format_violation(box_length_with_unit_gtin), "length_for_gtin")

if __name__ == "__main__":
    unittest.main()
