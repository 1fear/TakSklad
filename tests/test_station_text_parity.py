"""Operator-facing texts of the station logic must be the desktop's words

Every string literal in `frontend/src/station/logic/*.ts` that holds a Cyrillic word is read as an operator-facing
message. Its fixed parts (the text between `${...}` placeholders and `\\n` breaks) must occur verbatim in a file under
`src/taksklad/`, except the approved deviations named below
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGIC = ROOT / "frontend" / "src" / "station" / "logic"
DESKTOP = ROOT / "src" / "taksklad"

# the station is a page, not the program: the last two lines of the SKU mismatch message name the site and F5
APPROVED_DEVIATIONS = {
    "Версия сайта: ",
    "Если SKU на блоке верный, обновите страницу (F5).",
}

BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
LINE_COMMENT = re.compile(r"//[^\n]*")
# a regex literal can hold quote marks, so it is cut before the string literals are read
REGEX_LITERAL = re.compile(r"(?<=[(,=:])\s*/(?![/*])(?:[^/\\\n\[]|\\.|\[(?:[^\]\\\n]|\\.)*\])+/[a-z]*")
# a string literal on one line: "double quoted" or `template`
STRING_LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"|`((?:[^`\\\n]|\\.)*)`')
PLACEHOLDER = re.compile(r"\$\{[^{}]*\}")
# two Cyrillic letters in a row: a word, unlike the single "ё" in a replaceAll() call
CYRILLIC_WORD = re.compile(r"[А-Яа-яЁё]{2,}")


def code_without_comments(source):
    return REGEX_LITERAL.sub("", LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", source)))


def fixed_parts(source):
    """The fixed parts of every Cyrillic string literal, one list per literal; a placeholder is code, searched in turn"""
    for match in STRING_LITERAL.finditer(source):
        text = match.group(1) if match.group(1) is not None else match.group(2)
        for placeholder in PLACEHOLDER.findall(text):
            yield from fixed_parts(placeholder[2:-1])
        parts = [part for part in re.split(r"\$\{[^{}]*\}|\\n", text) if part.strip()]
        if CYRILLIC_WORD.search("".join(parts)):
            yield parts


class StationTextParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.desktop = "\n".join(path.read_text(encoding="utf-8") for path in sorted(DESKTOP.rglob("*.py")))
        cls.found = [
            (path.name, parts)
            for path in sorted(LOGIC.glob("*.ts"))
            for parts in fixed_parts(code_without_comments(path.read_text(encoding="utf-8")))
        ]

    def test_extraction_reads_every_logic_file(self):
        # a broken extraction would check nothing and pass, so it is pinned by what it must find
        self.assertGreater(len(self.found), 40)
        self.assertEqual({name for name, _parts in self.found}, {path.name for path in LOGIC.glob("*.ts")})

    def test_every_fixed_part_occurs_in_the_desktop_sources(self):
        missing = [
            f"{name}: {part!r}"
            for name, parts in self.found
            for part in parts
            if part not in self.desktop and part not in APPROVED_DEVIATIONS
        ]
        self.assertEqual(missing, [])

    def test_the_approved_deviations_are_real(self):
        # an allowance for a text that is not in the logic, or that the desktop has after all, must go
        messages = {part for _name, parts in self.found for part in parts}
        self.assertEqual(APPROVED_DEVIATIONS - messages, set())
        for part in APPROVED_DEVIATIONS:
            self.assertNotIn(part, self.desktop)


if __name__ == "__main__":
    unittest.main()
