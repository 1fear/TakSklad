import importlib
import inspect
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

# Разбор чисел из внешних источников в проекте не централизован: одна и та же
# функция скопирована по модулям. Пока копии живут отдельно, контракт проверяется
# сразу по всем, иначе дыра чинится в одной и остаётся в остальных
PARSE_INT_IMPLEMENTATIONS = (
    ("backend.app.excel_importer", "parse_int"),
    ("backend.app.imports_service", "parse_int"),
    ("backend.app.kiz_reports_service", "parse_int"),
    ("backend.app.orders_service", "parse_int"),
    ("backend.app.reports_service", "parse_int"),
    ("backend.app.scan_quantities", "parse_int"),
    ("backend.app.settings", "parse_int"),
    ("backend.app.skladbot_contracts", "parse_int"),
    ("backend.app.smartup_auto_import", "parse_int"),
    ("backend.app.telegram_common", "parse_int"),
    ("backend.app.telegram_manual_support", "parse_int"),
    ("taksklad.scan_quantities", "parse_int"),
    ("taksklad.utils", "parse_int_value"),
)

PARSE_MONEY_IMPLEMENTATIONS = (
    ("backend.app.excel_importer", "parse_money"),
    ("backend.app.imports_service", "parse_money"),
    ("backend.app.smartup_auto_import", "parse_money"),
)

SCANNED_PACKAGES = (
    (Path("backend") / "app", "backend.app"),
    (Path("src") / "taksklad", "taksklad"),
)

DEFINITION_RE = re.compile(r"^def (parse_int|parse_int_value|parse_money)\(", re.MULTILINE)

# float() отдаёт бесконечность на всех этих формах, а int() от бесконечности
# бросает OverflowError, который не является подклассом ValueError. Целым числом
# ни одна из форм не является, поэтому здесь ответ обязан быть нулём у всех копий
OVERFLOWING_FLOAT_TEXTS = ("inf", "-inf", "Infinity", "1e400", "-1e400")

# Длинная строка цифр отдельно: через float() она тоже даёт бесконечность, но для
# int() это законный литерал, и копии, разбирающие строку напрямую, обязаны вернуть
# само число. Общее требование к ней только одно, не бросать
LONG_DIGIT_TEXT = "9" * 400

HOSTILE_TEXTS = OVERFLOWING_FLOAT_TEXTS + (
    LONG_DIGIT_TEXT,
    "nan",
    "-nan",
    "мусор",
    "",
    "   ",
    "1/2",
    "0x10",
)


def call(function, value):
    """Вызвать копию с её собственной сигнатурой.

    У большинства копий один параметр, у settings.parse_int и
    smartup_auto_import.parse_int второй параметр это значение по умолчанию
    """
    parameters = inspect.signature(function).parameters
    required = [
        name
        for name, parameter in parameters.items()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(required) > 1:
        return function(value, 0)
    return function(value)


class ParseIntOverflowGuardTests(unittest.TestCase):
    """Число из внешнего источника не имеет права уронить разбор.

    Значения приходят из ответов СкладБота и Smartup, из ячеек Excel и из полей
    payload, то есть из мест, которые проект не контролирует. Необработанное
    исключение здесь обрывает не одну строку и не одну заявку, а весь цикл
    воркера или весь разбор файла, поэтому контракт такой: вернуть значение по
    умолчанию, но не бросить
    """

    def implementations(self):
        for module_name, function_name in PARSE_INT_IMPLEMENTATIONS:
            module = importlib.import_module(module_name)
            yield module_name, function_name, getattr(module, function_name)

    def test_every_implementation_survives_hostile_text(self):
        for module_name, function_name, function in self.implementations():
            for value in HOSTILE_TEXTS:
                with self.subTest(module=module_name, function=function_name, value=value[:16]):
                    try:
                        result = call(function, value)
                    except Exception as exc:  # noqa: BLE001 - суть проверки в том, что его нет
                        self.fail(f"{module_name}.{function_name}({value[:16]!r}) бросил {exc!r}")
                    self.assertIsInstance(result, int)

    def test_infinite_text_falls_back_to_zero(self):
        for module_name, function_name, function in self.implementations():
            for value in OVERFLOWING_FLOAT_TEXTS:
                with self.subTest(module=module_name, function=function_name, value=value):
                    self.assertEqual(call(function, value), 0)

    def test_plain_number_is_still_parsed(self):
        for module_name, function_name, function in self.implementations():
            with self.subTest(module=module_name, function=function_name):
                self.assertEqual(call(function, "10"), 10)

    def test_none_and_empty_give_zero(self):
        for module_name, function_name, function in self.implementations():
            for value in (None, ""):
                with self.subTest(module=module_name, function=function_name, value=value):
                    self.assertEqual(call(function, value), 0)

    def test_implementation_list_covers_every_copy_in_the_tree(self):
        """Появилась новая копия, значит она обязана попасть под этот контракт.

        Без этой проверки список выше тихо устаревает, и следующая копия
        приезжает в проект без защиты, как это уже произошло с шестью
        """
        found = set()
        for relative_dir, package in SCANNED_PACKAGES:
            directory = REPO_ROOT / relative_dir
            for source in sorted(directory.glob("*.py")):
                for match in DEFINITION_RE.finditer(source.read_text(encoding="utf-8")):
                    found.add((f"{package}.{source.stem}", match.group(1)))

        declared = set(PARSE_INT_IMPLEMENTATIONS) | set(PARSE_MONEY_IMPLEMENTATIONS)
        self.assertEqual(
            found,
            declared,
            "списки реализаций разошлись с деревом: "
            f"не покрыто {sorted(found - declared)}, лишнее {sorted(declared - found)}",
        )


class ParseMoneyOverflowGuardTests(unittest.TestCase):
    """Сумма приходит из ячейки Excel или из поля Smartup, то есть снаружи.

    Разбор суммы устроен иначе, чем разбор количества: текст вида «240 000 сум»
    это штатный случай, из него намеренно выскребаются цифры. Поэтому здесь
    проверяется не «ноль на всё непонятное», а две вещи: функция не бросает
    ни на чём, и реальные формы суммы разбираются как раньше
    """

    def implementations(self):
        for module_name, function_name in PARSE_MONEY_IMPLEMENTATIONS:
            module = importlib.import_module(module_name)
            yield module_name, function_name, getattr(module, function_name)

    def test_nothing_raises(self):
        values = (
            float("inf"),
            float("-inf"),
            float("nan"),
            "inf",
            "-inf",
            "Infinity",
            "1e400",
            "-1e400",
            LONG_DIGIT_TEXT,
            LONG_DIGIT_TEXT + " сум",
            "мусор",
            "",
            "   ",
            None,
            0,
            -5,
            240000,
            240000.7,
        )
        for module_name, function_name, function in self.implementations():
            for value in values:
                label = str(value)[:16]
                with self.subTest(module=module_name, value=label):
                    try:
                        result = function(value)
                    except Exception as exc:  # noqa: BLE001 - суть проверки в том, что его нет
                        self.fail(f"{module_name}.{function_name}({label!r}) бросил {exc!r}")
                    self.assertIsInstance(result, int)

    def test_non_finite_number_is_not_a_sum(self):
        # Бесконечность и nan приходят и готовым float, и текстом: ни то ни другое
        # не является суммой, поэтому ноль, как и для любого нечислового значения
        for module_name, function_name, function in self.implementations():
            for value in (float("inf"), float("-inf"), float("nan"), "inf", "-inf", "Infinity", LONG_DIGIT_TEXT):
                with self.subTest(module=module_name, value=str(value)[:16]):
                    self.assertEqual(function(value), 0)

    def test_real_money_shapes_are_unchanged(self):
        expected = {
            "240 000": 240000,
            "240000,00": 240000,
            "240 000 сум": 240000,
            "12abc34": 1234,
            "0": 0,
            "": 0,
            "мусор": 0,
        }
        for module_name, function_name, function in self.implementations():
            for value, want in expected.items():
                with self.subTest(module=module_name, value=value):
                    self.assertEqual(function(value), want)

    def test_numeric_input_is_truncated_not_rounded(self):
        for module_name, function_name, function in self.implementations():
            with self.subTest(module=module_name):
                self.assertEqual(function(240000), 240000)
                self.assertEqual(function(240000.7), 240000)
                self.assertEqual(function(-5), -5)


if __name__ == "__main__":
    unittest.main()
