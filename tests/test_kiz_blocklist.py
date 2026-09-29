"""Blocked KIZ codes must be refused identically by the desktop and the backend.

Written with unittest on purpose: CI runs `python -m unittest discover -s tests`,
so a pytest-style module here would silently never run.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from backend.app.kiz_blocklist import (
    BLOCKED_KIZ_CODES as BACKEND_BLOCKED_KIZ_CODES,
    blocked_kiz_reason as backend_blocked_reason,
    is_blocked_kiz as backend_is_blocked,
)
from taksklad.kiz_blocklist import (
    BLOCKED_KIZ_CODES,
    blocked_kiz_reason as desktop_blocked_reason,
    is_blocked_kiz as desktop_is_blocked,
)

# Every entry has to be covered: a second blocked code used to add no coverage
# at all, because the assertions only ever looked at the first key.
BLOCKED_CODE = next(iter(BLOCKED_KIZ_CODES))
# The blocklist compares whole codes, so an env entry must be the exact mark.
ENV_BLOCKED_FIRST = "0100000000000000000extraXXXXXXXXXXX"
ENV_BLOCKED_SECOND = "0100000000000000000secondXXXXXXXXXX"
ENV_ALLOWED = "0100000000000000000thirdXXXXXXXXXXX"


class KizBlocklistTests(unittest.TestCase):
    def test_blocked_code_is_rejected_on_desktop_and_backend(self):
        for code in BLOCKED_KIZ_CODES:
            with self.subTest(code=code):
                self.assertTrue(desktop_is_blocked(code))
                self.assertTrue(backend_is_blocked(code))

    def test_blocked_code_reason_is_identical_in_both_contours(self):
        for code in BLOCKED_KIZ_CODES:
            with self.subTest(code=code):
                self.assertEqual(desktop_blocked_reason(code), backend_blocked_reason(code))
                self.assertTrue(desktop_blocked_reason(code))

    def test_both_contours_block_the_same_set_of_codes(self):
        self.assertEqual(BLOCKED_KIZ_CODES, BACKEND_BLOCKED_KIZ_CODES)

    def test_surrounding_whitespace_does_not_bypass_block(self):
        for code in BLOCKED_KIZ_CODES:
            with self.subTest(code=code):
                self.assertTrue(desktop_is_blocked(f"  {code}\r\n"))
                self.assertTrue(backend_is_blocked(f"\t{code} "))

    def test_regular_code_stays_allowed(self):
        self.assertEqual(desktop_blocked_reason("0104006396053947217other-code-tailX"), "")
        self.assertEqual(backend_blocked_reason("0104006396053947217other-code-tailX"), "")
        self.assertEqual(desktop_blocked_reason(""), "")
        self.assertEqual(backend_blocked_reason(None), "")

    def test_code_with_a_comma_cannot_be_expressed_in_the_env_list(self):
        # The env list is comma-separated, so a mark carrying a comma is cut in
        # two and silently stops being blocked: such codes belong in the dict.
        comma_code = "0100000000000000000with,commaXXXXX"
        environ = {"TAKSKLAD_BLOCKED_KIZ_CODES": comma_code}
        self.assertFalse(backend_is_blocked(comma_code, environ=environ))

    def test_backend_env_can_block_additional_codes_without_release(self):
        environ = {"TAKSKLAD_BLOCKED_KIZ_CODES": f"{ENV_BLOCKED_FIRST}, {ENV_BLOCKED_SECOND}"}
        self.assertTrue(backend_is_blocked(ENV_BLOCKED_FIRST, environ=environ))
        self.assertTrue(backend_is_blocked(ENV_BLOCKED_SECOND, environ=environ))
        self.assertFalse(backend_is_blocked(ENV_ALLOWED, environ=environ))


if __name__ == "__main__":
    unittest.main()
