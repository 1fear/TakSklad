import tempfile
import unittest
from pathlib import Path

from backend.app.excel_importer import excel_file_to_import_payload
from tools.generate_acceptance_excel import DEFAULT_MARKER, save_acceptance_excel
from tools.prepare_acceptance_kit import (
    MANIFEST_NAME,
    README_NAME,
    RESULT_FILE_NAME,
    RESULT_TEMPLATE_NAME,
    prepare_acceptance_kit,
)


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
            result_template_text = (output_dir / RESULT_TEMPLATE_NAME).read_text(encoding="utf-8")
            result_file_text = (output_dir / RESULT_FILE_NAME).read_text(encoding="utf-8")

        self.assertIn('"marker": "ACCEPTANCE TELEGRAM 20260531"', manifest_text)
        self.assertIn(f'"result_template": "{RESULT_TEMPLATE_NAME}"', manifest_text)
        self.assertIn(f'"result_file": "{RESULT_FILE_NAME}"', manifest_text)
        self.assertIn('"local_preflight"', manifest_text)
        self.assertIn('"planned_blocks": 3', manifest_text)
        self.assertIn('"contains_secrets": false', manifest_text)
        self.assertIn("SHA-256 Excel", readme_text)
        self.assertIn("WIN-KIZ-ACCEPT-001", readme_text)
        self.assertIn("windows_backend_acceptance.ps1", readme_text)
        self.assertIn("wait_acceptance_marker.sh", readme_text)
        self.assertIn('"telegram_wait"', manifest_text)
        self.assertIn('"windows_wait"', manifest_text)
        self.assertIn('"windows_build_test_archive"', manifest_text)
        self.assertIn('"windows_launch_source_auto"', manifest_text)
        self.assertIn("build_windows_test_archive.ps1", readme_text)
        self.assertIn(RESULT_TEMPLATE_NAME, readme_text)
        self.assertIn("release_preflight.py", readme_text)
        self.assertIn("-UsePython", readme_text)
        self.assertIn("api.taksklad.uz", readme_text)
        self.assertIn("acceptance_status.sh", readme_text)
        self.assertIn("acceptance_status.sh --require-go", readme_text)
        self.assertIn('"vds_status"', manifest_text)
        self.assertIn('"telegram_status"', manifest_text)
        self.assertIn('"windows_status"', manifest_text)
        self.assertIn("GO к подготовке release 2.0", result_template_text)
        self.assertIn("SkladBot Matching", result_template_text)
        self.assertIn("WIN-KIZ-ACCEPT-001", result_template_text)
        self.assertIn("NO-GO, релиз откладывается.", result_file_text)
        self.assertIn("Файл создан автоматически как стартовый NO-GO", result_file_text)

        forbidden_fragments = [
            "TELEGRAM_BOT_TOKEN=",
            "SKLADBOT_API_TOKEN=",
            "GOOGLE_PRIVATE_KEY",
        ]
        for text in (manifest_text, readme_text, result_template_text, result_file_text):
            for fragment in forbidden_fragments:
                self.assertNotIn(fragment, text)

    def test_acceptance_kit_preserves_existing_result_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            prepare_acceptance_kit(output_dir)
            result_path = output_dir / RESULT_FILE_NAME
            result_path.write_text("manual acceptance notes\n", encoding="utf-8")

            prepare_acceptance_kit(output_dir)

            self.assertEqual(result_path.read_text(encoding="utf-8"), "manual acceptance notes\n")

    def test_acceptance_kit_checksum_is_stable_on_regeneration(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            prepare_acceptance_kit(output_dir)
            first_manifest = (output_dir / MANIFEST_NAME).read_text(encoding="utf-8")

            prepare_acceptance_kit(output_dir)
            second_manifest = (output_dir / MANIFEST_NAME).read_text(encoding="utf-8")

        self.assertEqual(first_manifest, second_manifest)

    def test_acceptance_excel_is_byte_stable_between_generations(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            first_path = Path(tmp_dir) / "first.xlsx"
            second_path = Path(tmp_dir) / "second.xlsx"

            save_acceptance_excel(first_path)
            save_acceptance_excel(second_path)

            self.assertEqual(first_path.read_bytes(), second_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
