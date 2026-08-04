"""The browser KIZ guard must stay identical to the Windows desktop guard.

The desktop client refuses a malformed KIZ before it ever reaches the backend
(`src/taksklad/utils.py::validate_kiz_code`), the browser mirrors it, and since
the scanner artifacts of 03-04.08.2026 the API enforces it too
(`backend/app/kiz_format.py`).

This test reads the SAME corpus that `frontend/src/__tests__/kizFormat.test.ts`
pins on the TypeScript side and asserts all three implementations agree, so they
cannot drift.
"""

from pathlib import Path
import re
import unittest

from taksklad.config import KIZ_BOX_LENGTH, KIZ_MAX_LENGTH, KIZ_MIN_LENGTH, KIZ_UNIT_LENGTH
from taksklad.utils import normalize_kiz_code, validate_kiz_code


ROOT = Path(__file__).resolve().parents[1]
KIZ_FORMAT_TS = ROOT / "frontend/src/features/warehouse/kizFormat.ts"
KIZ_FORMAT_SPEC = ROOT / "frontend/src/__tests__/kizFormat.test.ts"

# code -> expected rule id ("" means the code is accepted).
# Accepted codes carry a real production shape: a block is 35 characters, a box
# is 67, and both start with AI 01 plus a 14-digit GTIN.
CORPUS = [
    ("0104006396053947217ABCDEF93GHIJKLMN", ""),
    ("010400639605401221UZ1112022525522513824013040046110ZIG1218229310000", ""),
    ("0104006396053947217ABC\x1dDEF93GHIJKLM", ""),
    ("  0104006396053947217ABCDEF93GHIJKLMN  ", ""),
    ("", "empty"),
    ("   ", "empty"),
    ("1234567890123456789012", "prefix"),
    ("010123456789", "too_short"),
    ("01" + "x" * KIZ_MAX_LENGTH, "too_long"),
    ("0104006396053947217ПРИВЕТ", "cyrillic"),
    ("0104006396053947 217ABCDEF", "whitespace"),
    ("0104006396053947217ABCé", "charset"),
    # Scanner artifacts seen in production, see backend/app/kiz_format.py.
    ("010400639605394A217ABCDEF93GHIJKLMN", "head"),
    ("0104006396053947217ABCDEF93GHIJKLMN0104006396053947217ZZZZZZ93QQQQQQQQ", "double_mark"),
    ("0104006396053947217ABCDEF93GHIJKLMNWH-R-214126", "length"),
    ("0104006396053947217ABCDEF", "length"),
]


class KizFormatContractTests(unittest.TestCase):
    def test_desktop_validator_agrees_with_the_browser_corpus(self):
        for code, expected_rule in CORPUS:
            with self.subTest(code=code[:32]):
                is_valid, message, _normalized = validate_kiz_code(code)
                if expected_rule:
                    self.assertFalse(is_valid, f"desktop accepted {code[:32]!r} but browser rejects it")
                    self.assertTrue(message, "desktop rejection must carry an operator message")
                else:
                    self.assertTrue(is_valid, f"desktop rejected {code[:32]!r} but browser accepts it: {message}")

    def test_backend_guard_agrees_with_the_desktop_corpus(self):
        """The API is the last line: a client that skips its guard must still be refused."""
        from backend.app.kiz_format import kiz_format_violation

        for code, expected_rule in CORPUS:
            with self.subTest(code=code[:32]):
                violation = kiz_format_violation(code)
                desktop_valid, _message, _normalized = validate_kiz_code(code)
                self.assertEqual(
                    bool(violation),
                    not desktop_valid,
                    f"backend and desktop disagree on {code[:32]!r}",
                )
                self.assertEqual(violation, expected_rule, f"backend rule drifted for {code[:32]!r}")

    def test_browser_corpus_matches_this_corpus_exactly(self):
        spec = KIZ_FORMAT_SPEC.read_text(encoding="utf-8")
        for code, expected_rule in CORPUS:
            if "repeat(" in code or code == "01" + "x" * KIZ_MAX_LENGTH:
                continue
            literal = (
                code.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\x1d", "\\x1d")
            )
            self.assertIn(
                f'{{ code: "{literal}", rule: "{expected_rule}" }}',
                spec,
                f"corpus entry {code[:32]!r} is missing or classified differently in kizFormat.test.ts",
            )

    def test_browser_guard_pins_the_desktop_bounds_and_charset(self):
        source = KIZ_FORMAT_TS.read_text(encoding="utf-8")

        self.assertIn(f"export const KIZ_MIN_LENGTH = {KIZ_MIN_LENGTH};", source)
        self.assertIn(f"export const KIZ_MAX_LENGTH = {KIZ_MAX_LENGTH};", source)
        self.assertIn(f"export const KIZ_UNIT_LENGTH = {KIZ_UNIT_LENGTH};", source)
        self.assertIn(f"export const KIZ_BOX_LENGTH = {KIZ_BOX_LENGTH};", source)
        self.assertIn('code.startsWith("01")', source)
        self.assertIn("[а-яА-ЯёЁ]", source)
        # GS (\x1d) must stay allowed: real DataMatrix KIZ codes carry it.
        self.assertRegex(source, r"\\x1d\\x21-\\x7E")

    def test_browser_guard_uses_the_desktop_operator_wording(self):
        source = KIZ_FORMAT_TS.read_text(encoding="utf-8")
        desktop_messages = [
            "Код пустой",
            "КИЗ должен начинаться с 01",
            "Код содержит русские буквы! Используйте только латиницу",
            "Код содержит пробелы или переносы",
            "Код содержит недопустимые символы",
            "Код не похож на марку: после 01 ожидается GTIN из 14 цифр",
            "Считаны две марки сразу! Сканируйте по одной",
        ]
        for message in desktop_messages:
            self.assertIn(message, source, f"browser lost the desktop message {message!r}")

        # The browser renders the bounds from the pinned constants, so assert the
        # template form here; kizFormat.test.ts asserts the rendered wording.
        for length_message in (
            "минимум ${KIZ_MIN_LENGTH} символов",
            "максимум ${KIZ_MAX_LENGTH} символов",
        ):
            self.assertIn(length_message, source)

        spec = KIZ_FORMAT_SPEC.read_text(encoding="utf-8")
        self.assertIn(f"минимум {KIZ_MIN_LENGTH} символов", spec)
        self.assertIn(f"максимум {KIZ_MAX_LENGTH} символов", spec)

    def test_desktop_messages_are_still_the_source_of_truth(self):
        """If the desktop wording changes, this test forces the browser to follow."""
        desktop_source = (ROOT / "src/taksklad/utils.py").read_text(encoding="utf-8")
        browser_source = KIZ_FORMAT_TS.read_text(encoding="utf-8")
        quoted = set(re.findall(r'"(Код [^"]+|КИЗ [^"]+)"', desktop_source))
        static_messages = {
            text for text in quoted
            if "{" not in text and "минимум" not in text and "максимум" not in text
        }
        self.assertTrue(static_messages, "expected desktop operator messages to be discoverable")
        for text in sorted(static_messages):
            self.assertIn(text, browser_source, f"desktop message {text!r} has no browser equivalent")

    def test_normalization_matches(self):
        self.assertEqual(
            normalize_kiz_code("  0104006396053947217ABCDEF93GHIJKLMN \t\r\n"),
            "0104006396053947217ABCDEF93GHIJKLMN",
        )


if __name__ == "__main__":
    unittest.main()
