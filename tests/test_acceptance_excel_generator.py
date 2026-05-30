import tempfile
import unittest
from pathlib import Path

from backend.app.excel_importer import excel_file_to_import_payload
from tools.generate_acceptance_excel import DEFAULT_MARKER, save_acceptance_excel
from tools.prepare_acceptance_kit import MANIFEST_NAME, README_NAME, prepare_acceptance_kit


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

    def test_acceptance_kit_contains_manifest_and_readme_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = prepare_acceptance_kit(Path(tmp_dir))
            manifest_text = (output_dir / MANIFEST_NAME).read_text(encoding="utf-8")
            readme_text = (output_dir / README_NAME).read_text(encoding="utf-8")

        self.assertIn('"marker": "ACCEPTANCE TELEGRAM 20260531"', manifest_text)
        self.assertIn('"planned_blocks": 3', manifest_text)
        self.assertIn('"contains_secrets": false', manifest_text)
        self.assertIn("SHA-256 Excel", readme_text)
        self.assertIn("WIN-KIZ-ACCEPT-001", readme_text)
        self.assertIn("windows_backend_acceptance.ps1", readme_text)

        forbidden_fragments = [
            "TELEGRAM_BOT_TOKEN=",
            "SKLADBOT_API_TOKEN=",
            "GOOGLE_PRIVATE_KEY",
        ]
        for text in (manifest_text, readme_text):
            for fragment in forbidden_fragments:
                self.assertNotIn(fragment, text)

    def test_acceptance_kit_checksum_is_stable_on_regeneration(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            prepare_acceptance_kit(output_dir)
            first_manifest = (output_dir / MANIFEST_NAME).read_text(encoding="utf-8")

            prepare_acceptance_kit(output_dir)
            second_manifest = (output_dir / MANIFEST_NAME).read_text(encoding="utf-8")

        self.assertEqual(first_manifest, second_manifest)


if __name__ == "__main__":
    unittest.main()
