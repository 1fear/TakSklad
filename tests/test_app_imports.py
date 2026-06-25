import unittest

from taksklad.app_imports import apply_backend_import_preview, source_filename_for_records


class AppImportsTests(unittest.TestCase):
    def test_backend_import_preview_filters_new_duplicate_and_invalid_rows(self):
        parsed = {
            "records": [
                {"Клиент": "Client A", "Товары": "Product A", "Кол-во ШТ": 10, "Кол-во блок": 1},
                {"Клиент": "Client B", "Товары": "Product B", "Кол-во ШТ": 20, "Кол-во блок": 2},
                {"Клиент": "Client C", "Товары": "Product C", "Кол-во ШТ": 30, "Кол-во блок": 3},
            ],
            "errors": [],
        }
        preview = {
            "duplicate_row_numbers": [2],
            "invalid_row_numbers": [3],
            "errors": ["row 3: missing required fields: payment_type"],
        }

        result = apply_backend_import_preview(parsed, preview)

        self.assertEqual(result["new_records"], [parsed["records"][0]])
        self.assertEqual(result["duplicate_records"], [parsed["records"][1]])
        self.assertEqual(result["backend_invalid_rows_count"], 1)
        self.assertEqual(result["clients_count"], 1)
        self.assertEqual(result["products_count"], 1)
        self.assertEqual(result["quantity_count"], 10)
        self.assertEqual(result["blocks_count"], 1)
        self.assertTrue(result["backend_import"])
        self.assertEqual(result["errors"], ["backend preview: row 3: missing required fields: payment_type"])

    def test_source_filename_for_records_uses_first_five_sorted_sources(self):
        records = [
            {"Источник файла": "b.xlsx"},
            {"Источник файла": "a.xlsx"},
            {"Источник файла": "c.xlsx"},
            {"Источник файла": ""},
            {"Источник файла": "f.xlsx"},
            {"Источник файла": "e.xlsx"},
            {"Источник файла": "d.xlsx"},
        ]

        self.assertEqual(source_filename_for_records(records), "a.xlsx, b.xlsx, c.xlsx, d.xlsx, e.xlsx")


if __name__ == "__main__":
    unittest.main()
