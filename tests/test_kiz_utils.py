import unittest

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


if __name__ == "__main__":
    unittest.main()
