import unittest

from backend.app.skladbot_contracts import parse_int


class SkladBotContractsParseIntTests(unittest.TestCase):
    """parse_int разбирает числа из ответа СкладБота, то есть из внешнего источника.

    Любое необработанное исключение здесь обрывает не одну заявку, а весь цикл
    воркера, поэтому функция обязана возвращать 0 на любом непригодном значении
    """

    def test_finite_values_are_parsed(self):
        self.assertEqual(parse_int("60"), 60)
        self.assertEqual(parse_int("10,9"), 10)
        self.assertEqual(parse_int(" 1 234 "), 1234)
        self.assertEqual(parse_int(""), 0)
        self.assertEqual(parse_int(None), 0)
        self.assertEqual(parse_int("мусор"), 0)

    def test_not_a_number_returns_zero(self):
        # float("nan") проходит, а int() от него бросает ValueError
        self.assertEqual(parse_int("nan"), 0)

    def test_infinite_values_return_zero_instead_of_raising(self):
        # float() отдаёт бесконечность и на слове, и на переполненной экспоненте,
        # а int() от бесконечности бросает OverflowError, который не является
        # подклассом ValueError и раньше уходил из функции наверх
        for value in ("inf", "-inf", "Infinity", "1e400", "-1e400", "9" * 400):
            with self.subTest(value=value):
                self.assertEqual(parse_int(value), 0)


if __name__ == "__main__":
    unittest.main()
