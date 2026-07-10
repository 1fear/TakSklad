import unittest
from decimal import Decimal

from pydantic import ValidationError

from backend.app.schemas import ImportCreate


class BackendInputSafetyTests(unittest.TestCase):
    def test_import_dto_rejects_nonfinite_decimal(self):
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(value=str(value)), self.assertRaises(ValidationError):
                ImportCreate(rows=[{"Кол-во ШТ": value}])

    def test_import_dto_rejects_unknown_field(self):
        with self.assertRaises(ValidationError):
            ImportCreate(rows=[{"unexpected": "synthetic"}])


if __name__ == "__main__":
    unittest.main()
