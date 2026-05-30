import tempfile
import unittest
from pathlib import Path

from backend.app.excel_importer import excel_file_to_import_payload
from tools.generate_acceptance_excel import DEFAULT_MARKER, save_acceptance_excel


class AcceptanceExcelGeneratorTests(unittest.TestCase):
    def test_generated_acceptance_excel_is_parseable_by_backend_importer(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "acceptance.xlsx"
            save_acceptance_excel(output_path)

            payload = excel_file_to_import_payload(
                output_path,
                file_name=output_path.name,
                source="telegram",
                shipment_date="31.05.2026",
            )

        self.assertEqual(payload["source"], "telegram")
        self.assertEqual(payload["filename"], "acceptance.xlsx")
        self.assertEqual(payload["meta"]["warnings"], [])
        self.assertEqual(payload["meta"]["shipment_date"], "31.05.2026")
        self.assertEqual(len(payload["rows"]), 2)
        self.assertEqual(sum(row["Кол-во блок"] for row in payload["rows"]), 3)
        self.assertEqual(sum(row["Сумма позиции"] for row in payload["rows"]), 720000)
        self.assertEqual({row["Клиент"] for row in payload["rows"]}, {DEFAULT_MARKER})
        self.assertEqual({row["Координаты"] for row in payload["rows"]}, {"41.311081, 69.240562"})


if __name__ == "__main__":
    unittest.main()
