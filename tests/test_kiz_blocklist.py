import os
import sys

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


def test_blocked_code_is_rejected_on_desktop_and_backend():
    assert desktop_is_blocked(BLOCKED_CODE)
    assert backend_is_blocked(BLOCKED_CODE)


def test_blocked_code_reason_is_identical_in_both_contours():
    assert desktop_blocked_reason(BLOCKED_CODE) == backend_blocked_reason(BLOCKED_CODE)
    assert desktop_blocked_reason(BLOCKED_CODE)


def test_surrounding_whitespace_does_not_bypass_block():
    assert desktop_is_blocked(f"  {BLOCKED_CODE}\r\n")
    assert backend_is_blocked(f"\t{BLOCKED_CODE} ")


def test_regular_code_stays_allowed():
    assert desktop_blocked_reason("0104006396053947217other-code-tailX") == ""
    assert backend_blocked_reason("0104006396053947217other-code-tailX") == ""
    assert desktop_blocked_reason("") == ""
    assert backend_blocked_reason(None) == ""


def test_backend_env_can_block_additional_codes_without_release():
    environ = {"TAKSKLAD_BLOCKED_KIZ_CODES": "0100000000000000000extra, 0100000000000000000second"}
    assert backend_is_blocked("0100000000000000000extraXXXXXXXXXXX", environ=environ)
    assert backend_is_blocked("0100000000000000000secondXXXXXXXXXX", environ=environ)
    assert not backend_is_blocked("0100000000000000000thirdXXXXXXXXXXX", environ=environ)
