import unittest

from taksklad.backend_client import backend_order_to_rows
from taksklad.backend_flow import unsaved_backend_scan_codes
from taksklad.desktop_scan_rules import scanned_codes_for_order
from taksklad.utils import split_codes, validate_kiz_code


class KizUtilsTests(unittest.TestCase):
    def test_validate_accepts_gs1_group_separator(self):
        # Real block shape: 35 characters, AI 01 plus a 14-digit GTIN.
        code = "0104006396053947217ABC\x1dDEF93GHIJKLM"

        is_valid, message, normalized = validate_kiz_code(code)

        self.assertTrue(is_valid, message)
        self.assertEqual(normalized, code)

    def test_validate_rejects_real_line_breaks(self):
        is_valid, message, _ = validate_kiz_code("01012345678901234567\nABC123")

        self.assertFalse(is_valid)
        self.assertIn("переносы", message)

    def test_split_codes_does_not_split_on_comma_inside_kiz(self):
        first = "01012345678901234567ABC,DEFXXXXXXXX"
        second = "01012345678901234567XYZXXXXXXXXXXXX"

        self.assertEqual(split_codes(f"{first}\n{second}"), [first, second])

    def test_split_codes_keeps_gs1_group_separator_inside_kiz(self):
        # 05.10.2026: str.splitlines() считает GS (\x1d) переносом строки, код
        # позиции терял хвост и становился короче 35
        inner_gs = "0104006396053947217ABC\x1dDEF93GHIJKLM"
        trailing_gs = "010400639605394721ABCDEFG93HIJKLMN\x1d"
        plain = "0104006396053947217XYZWXYZ93XXXXXXX"
        for code in (inner_gs, trailing_gs, plain):
            self.assertTrue(validate_kiz_code(code)[0], code)

        self.assertEqual(
            split_codes(f"{inner_gs}\n{trailing_gs}\r\n{plain}"),
            [inner_gs, trailing_gs, plain],
        )

    def test_backend_code_with_gs_is_not_resent_as_unsaved(self):
        # Код с GS, уже записанный сервером, после загрузки позиции считался
        # несохранённым в обрезанном виде, уходил новым сканом, и сервер вечно
        # отвечал 422 «Code length matches neither a block nor a box»
        trailing_gs = "010400639605394721ABCDEFG93HIJKLMN\x1d"
        rows = backend_order_to_rows({
            "id": "order-1",
            "status": "active",
            "items": [{"id": "item-1", "product": "Chapman", "scan_codes": [trailing_gs]}],
        })

        self.assertEqual(scanned_codes_for_order(rows[0]), [trailing_gs])
        self.assertEqual(unsaved_backend_scan_codes(rows[0], scanned_codes_for_order(rows[0])), [])


if __name__ == "__main__":
    unittest.main()
