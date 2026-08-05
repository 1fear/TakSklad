"""Blocked KIZ codes must be refused identically by the desktop and the backend.

Written with unittest on purpose: CI runs `python -m unittest discover -s tests`,
so a pytest-style module here would silently never run.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from backend.app.kiz_blocklist import (
    blocked_kiz_reason as backend_blocked_reason,
    is_blocked_kiz as backend_is_blocked,
)
from taksklad.kiz_blocklist import (
    BLOCKED_KIZ_CODES,
    blocked_kiz_reason as desktop_blocked_reason,
    is_blocked_kiz as desktop_is_blocked,
)

BLOCKED_CODE = next(iter(BLOCKED_KIZ_CODES))
# The blocklist compares whole codes, so an env entry must be the exact mark.
ENV_BLOCKED_FIRST = "0100000000000000000extraXXXXXXXXXXX"
ENV_BLOCKED_SECOND = "0100000000000000000secondXXXXXXXXXX"
ENV_ALLOWED = "0100000000000000000thirdXXXXXXXXXXX"


class KizBlocklistTests(unittest.TestCase):
    def test_blocked_code_is_rejected_on_desktop_and_backend(self):
        self.assertTrue(desktop_is_blocked(BLOCKED_CODE))
        self.assertTrue(backend_is_blocked(BLOCKED_CODE))

    def test_blocked_code_reason_is_identical_in_both_contours(self):
        self.assertEqual(desktop_blocked_reason(BLOCKED_CODE), backend_blocked_reason(BLOCKED_CODE))
        self.assertTrue(desktop_blocked_reason(BLOCKED_CODE))

    def test_surrounding_whitespace_does_not_bypass_block(self):
        self.assertTrue(desktop_is_blocked(f"  {BLOCKED_CODE}\r\n"))
        self.assertTrue(backend_is_blocked(f"\t{BLOCKED_CODE} "))

    def test_regular_code_stays_allowed(self):
        self.assertEqual(desktop_blocked_reason("0104006396053947217other-code-tailX"), "")
        self.assertEqual(backend_blocked_reason("0104006396053947217other-code-tailX"), "")
        self.assertEqual(desktop_blocked_reason(""), "")
        self.assertEqual(backend_blocked_reason(None), "")

    def test_backend_env_can_block_additional_codes_without_release(self):
        environ = {"TAKSKLAD_BLOCKED_KIZ_CODES": f"{ENV_BLOCKED_FIRST}, {ENV_BLOCKED_SECOND}"}
        self.assertTrue(backend_is_blocked(ENV_BLOCKED_FIRST, environ=environ))
        self.assertTrue(backend_is_blocked(ENV_BLOCKED_SECOND, environ=environ))
        self.assertFalse(backend_is_blocked(ENV_ALLOWED, environ=environ))


if __name__ == "__main__":
    unittest.main()
