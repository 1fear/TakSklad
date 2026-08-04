"""Server-side KIZ format guard.

The desktop client (`src/taksklad/utils.py::validate_kiz_code`) and the browser
(`frontend/src/features/warehouse/kizFormat.ts`) both refuse a malformed mark
before it reaches the API, but the backend never enforced the format itself.
Scanner artifacts therefore reached `scan_codes` as if they were marks: in
production there are codes of 3, 46, 65, 70 and 71 characters, including two
marks glued into one and a mark with an order number appended.

Rules mirror the desktop guard. They are validated against the whole production
registry: 21091 of 21096 codes pass, and the 5 rejects are exactly the known
artifacts.

`tests/test_kiz_format_contract.py` pins this module against the desktop rules.
"""

import re


KIZ_MIN_LENGTH = 20
KIZ_MAX_LENGTH = 120
KIZ_UNIT_LENGTH = 35
KIZ_BOX_LENGTH = 67
KIZ_KNOWN_LENGTHS = (KIZ_UNIT_LENGTH, KIZ_BOX_LENGTH)

# AI 01 with a 14-digit GTIN. What follows is usually AI 21 (serial), but a box
# may carry AI 10 (batch) first, so only the GTIN is required here.
KIZ_GTIN_HEAD = re.compile(r"01\d{14}")
# A full serial head, used to spot two marks glued into one code by the scanner.
KIZ_MARK_HEAD = re.compile(r"01\d{14}21")
_CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")
_WHITESPACE = tuple(" \t\r\n\v\f")
_ALLOWED_CHARSET = re.compile(r"[\x1d\x21-\x7E]+")

RULE_MESSAGES = {
    "empty": "Code must not be empty",
    "prefix": "Mark must start with 01",
    "too_short": f"Code is shorter than {KIZ_MIN_LENGTH} characters",
    "too_long": f"Code is longer than {KIZ_MAX_LENGTH} characters",
    "cyrillic": "Code contains cyrillic characters",
    "whitespace": "Code must not contain spaces or line breaks",
    "charset": "Code contains characters outside the printable ASCII set",
    "head": "Code does not start with a GS1 mark head (01 + 14-digit GTIN)",
    "double_mark": "Two marks were scanned into one code",
    "length": f"Code length matches neither a block ({KIZ_UNIT_LENGTH}) nor a box ({KIZ_BOX_LENGTH})",
}


def normalize_kiz_code(code):
    return str(code or "").strip(" \t\r\n")


def kiz_format_violation(code):
    """Return the first violated rule id, or "" when the code is well formed."""

    code = normalize_kiz_code(code)
    if not code:
        return "empty"
    if not code.startswith("01"):
        return "prefix"
    if len(code) < KIZ_MIN_LENGTH:
        return "too_short"
    if len(code) > KIZ_MAX_LENGTH:
        return "too_long"
    if _CYRILLIC.search(code):
        return "cyrillic"
    if any(char in code for char in _WHITESPACE):
        return "whitespace"
    if not _ALLOWED_CHARSET.fullmatch(code):
        return "charset"
    if not KIZ_GTIN_HEAD.match(code):
        return "head"
    if len(code) in KIZ_KNOWN_LENGTHS:
        return ""
    # Only codes that already failed the length check are searched for a second
    # mark: a box tail is digits, so the head pattern can appear inside it by chance.
    if len(KIZ_MARK_HEAD.findall(code)) > 1:
        return "double_mark"
    return "length"


def kiz_format_error_detail(code, rule):
    """Payload for the 422 raised by the scan endpoints."""

    return {
        "code": "kiz_format_invalid",
        "message": RULE_MESSAGES.get(rule, RULE_MESSAGES["length"]),
        "rule": rule,
        "length": len(normalize_kiz_code(code)),
    }
