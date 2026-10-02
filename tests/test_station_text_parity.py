"""Operator-facing texts of the station logic must be the desktop's words

Every string literal in `frontend/src/station/logic/*.ts` that holds a Cyrillic word is read as an operator-facing
message: "double quoted", 'single quoted' and `template` literals, a template may span lines. Its fixed parts (the text
between `${...}` placeholders and `\\n` or real line breaks) must occur verbatim in a file under `src/taksklad/`,
except the approved deviations named below. The guard fails closed: a Cyrillic word that is left in the code once
comments, regex literals and every recognized literal are cut out is a message in a form the extraction does not read
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
# a string literal: "double quoted" or 'single quoted' on one line, `template` possibly over several lines
STRING_LITERAL = re.compile(r'''"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)'|`((?:[^`\\]|\\.)*)`''', re.S)
PLACEHOLDER = re.compile(r"\$\{[^{}]*\}")
# two Cyrillic letters in a row: a word, unlike the single "ё" in a replaceAll() call
CYRILLIC_WORD = re.compile(r"[А-Яа-яЁё]{2,}")


def code_without_comments(source):
    return REGEX_LITERAL.sub("", LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", source)))


def literal_text(match):
    """The text between the quotes of a STRING_LITERAL match, whichever of the three forms it is"""
    return next(group for group in match.groups() if group is not None)


def fixed_parts(source):
    """The fixed parts of every Cyrillic string literal, one list per literal; a placeholder is code, searched in turn

    A part is cut at a placeholder, at the two characters `\\n` and at a real line break (a template over several
    lines), and keeps its own indentation: the indentation of a multi-line template would show in the message
    """
    for match in STRING_LITERAL.finditer(source):
        text = literal_text(match)
        for placeholder in PLACEHOLDER.findall(text):
            yield from fixed_parts(placeholder[2:-1])
        parts = [part for part in re.split(r"\$\{[^{}]*\}|\\n|\n", text) if part.strip()]
        if CYRILLIC_WORD.search("".join(parts)):
            yield parts


def unread_cyrillic(source):
    """Lines of the code that still hold a Cyrillic word once every recognized literal is cut out (placeholders too)

    The extraction reads only the literal forms above, so a message in any other form (a template nested in a
    placeholder, a word outside quotes) is not checked against the desktop: it is reported here instead
    """
    left = []
    for match in STRING_LITERAL.finditer(source):
        for placeholder in PLACEHOLDER.findall(literal_text(match)):
            left.extend(unread_cyrillic(placeholder[2:-1]))
    left.extend(line.strip() for line in STRING_LITERAL.sub("", source).splitlines() if CYRILLIC_WORD.search(line))
    return left


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

    def test_no_cyrillic_word_is_left_outside_the_recognized_literals(self):
        # fail closed: a message in a form the extraction does not read would pass unchecked, so it fails here instead
        left = [
            f"{path.name}: {fragment!r}"
            for path in sorted(LOGIC.glob("*.ts"))
            for fragment in unread_cyrillic(code_without_comments(path.read_text(encoding="utf-8")))
        ]
        self.assertEqual(left, [])


# a desktop text in miniature for the guard's own tests: three messages on three lines
FAKE_DESKTOP = "Сначала выберите заказ\nПлан выполнен!\nКод принят"


def unfound(source):
    """Fixed parts of an inline source that the fake desktop does not hold: the same test the real check applies"""
    return [
        part
        for parts in fixed_parts(code_without_comments(source))
        for part in parts
        if part not in FAKE_DESKTOP and part not in APPROVED_DEVIATIONS
    ]


class StationTextGuardTests(unittest.TestCase):
    """The guard itself, on small inline sources: a guard that reads nothing passes everything, so each form is pinned"""

    def test_a_typo_in_double_quotes_is_reported(self):
        self.assertEqual(unfound('const m = "Сначала выберете заказ";'), ["Сначала выберете заказ"])
        self.assertEqual(unfound('const m = "Сначала выберите заказ";'), [])

    def test_a_typo_in_single_quotes_is_reported(self):
        self.assertEqual(unfound("const m = 'Сначала выберете заказ';"), ["Сначала выберете заказ"])
        self.assertEqual(unfound("const m = 'Сначала выберите заказ';"), [])

    def test_a_typo_in_a_multiline_template_is_reported(self):
        self.assertEqual(unfound("const m = `План выполнен!\nСначала выберете заказ`;"), ["Сначала выберете заказ"])
        # a real line break splits the parts like \n does: the two lines are found on two lines of the desktop text
        self.assertEqual(unfound("const m = `План выполнен!\nСначала выберите заказ`;"), [])

    def test_placeholders_and_line_breaks_still_split_the_fixed_parts(self):
        source = r"const m = `План ${plan} выполнен!\nСначала выберите заказ`;"
        self.assertEqual(list(fixed_parts(code_without_comments(source))), [["План ", " выполнен!", "Сначала выберите заказ"]])
        self.assertEqual(unfound(source), [])
        self.assertEqual(unfound(r"const m = `План ${plan} выполнено!\nКод принят`;"), [" выполнено!"])
        self.assertEqual(unfound(r'const m = "План выполнен!\nКод принят";'), [])
        self.assertEqual(unfound(r'const m = "План выполнен!\nКод принято";'), ["Код принято"])

    def test_a_string_inside_a_placeholder_is_read_too(self):
        # "принят" is in the desktop text, "не принят" is not
        self.assertEqual(unfound("const m = `Код ${ok ? 'принят' : 'не принят'}`;"), ["не принят"])

    def test_quotes_inside_other_quotes_do_not_split_a_literal(self):
        def parts(source):
            return list(fixed_parts(code_without_comments(source)))

        self.assertEqual(parts("""f("Нажмите 'ЗАВЕРШИТЬ ЗАКАЗ'");"""), [["Нажмите 'ЗАВЕРШИТЬ ЗАКАЗ'"]])
        self.assertEqual(parts("""f('Сказано "да" тут');"""), [['Сказано "да" тут']])
        self.assertEqual(parts("""f("Код принят", 'План выполнен!');"""), [["Код принят"], ["План выполнен!"]])

    def test_a_word_in_an_unrecognized_form_trips_the_fail_closed_check(self):
        self.assertEqual(unread_cyrillic(code_without_comments("const привет = 1;")), ["const привет = 1;"])
        # a template nested in a placeholder is not a form the extraction reads: its text is left over
        nested = 'const m = `a ${ok ? `вот` : ""}`;'
        self.assertEqual(unread_cyrillic(code_without_comments(nested)), ["const m = вот;"])

    def test_every_recognized_form_leaves_nothing_behind(self):
        source = (
            "// комментарий\n"
            "/* и блок */\n"
            "const a = f(\"да\", 'нет', `да\nнет ${x ? \"ок\" : 'ах'}`);\n"
            'const r = s.replace(/привет/g, "");\n'
        )
        self.assertEqual(unread_cyrillic(code_without_comments(source)), [])


if __name__ == "__main__":
    unittest.main()
