import tempfile
import unittest
from pathlib import Path

from tools.release_go_no_go import evaluate_acceptance_results, evaluate_file


GO_TEXT = """# TakSklad 2.0 Acceptance Results

## 1. Preflight

- [x] `.venv/bin/python tools/release_preflight.py` вернул `status=ok`.

## 2. Telegram Import

- [x] В Telegram нажата кнопка `Дата отгрузки`.
- [x] Отправлен Excel-файл как документ.

## 3. SkladBot Matching

- [x] Диагностика нашла ровно одно совпадение.

## 4. Windows Desktop Acceptance

- [x] Запуск выполнен из test archive, не из старого ярлыка `1.1.7`.
- [x] Завершение досканированного заказа прошло.

## 5. Cleanup

- [x] Cleanup с `--apply` выполнен.

## 6. Defects / Known Issues

| ID | Сценарий | Симптом | Severity | Решение | Статус |
| --- | --- | --- | --- | --- | --- |
| D-1 | cosmetic | typo | low | later | accepted |

## 7. Go / No-Go

- [x] Telegram import принят.
- [x] SkladBot matching принят.
- [x] Windows desktop acceptance принят.
- [x] Критичных дефектов нет.
- [x] Rollback понятен.
- [x] `version.json` всё ещё не менялся.

Итог:

- [x] GO к подготовке release 2.0.
- [ ] NO-GO, релиз откладывается.
"""


class ReleaseGoNoGoTests(unittest.TestCase):
    def test_go_when_required_checks_are_checked_and_no_critical_defects(self):
        result = evaluate_acceptance_results(GO_TEXT)

        self.assertEqual(result["status"], "go")
        self.assertEqual(result["problems"], [])

    def test_no_go_when_template_is_unchecked(self):
        text = GO_TEXT.replace("[x] Telegram import принят.", "[ ] Telegram import принят.")
        result = evaluate_acceptance_results(text)

        self.assertEqual(result["status"], "no_go")
        self.assertIn("required GO checkbox is not checked: Telegram import принят.", result["problems"])

    def test_no_go_when_acceptance_section_checkbox_is_unchecked(self):
        text = GO_TEXT.replace(
            "[x] Cleanup с `--apply` выполнен.",
            "[ ] Cleanup с `--apply` выполнен.",
        )
        result = evaluate_acceptance_results(text)

        self.assertEqual(result["status"], "no_go")
        self.assertIn(
            "required acceptance checkbox is not checked in 5. Cleanup: Cleanup с `--apply` выполнен.",
            result["problems"],
        )

    def test_no_go_when_required_section_is_missing(self):
        text = GO_TEXT.replace(
            "## 3. SkladBot Matching\n\n- [x] Диагностика нашла ровно одно совпадение.\n\n",
            "",
        )
        result = evaluate_acceptance_results(text)

        self.assertEqual(result["status"], "no_go")
        self.assertIn("required section is missing: 3. SkladBot Matching", result["problems"])

    def test_no_go_when_no_go_line_is_checked(self):
        text = GO_TEXT.replace("[ ] NO-GO, релиз откладывается.", "[x] NO-GO, релиз откладывается.")
        result = evaluate_acceptance_results(text)

        self.assertEqual(result["status"], "no_go")
        self.assertIn("NO-GO line is checked: NO-GO, релиз откладывается.", result["problems"])

    def test_no_go_when_unresolved_critical_defect_exists(self):
        text = GO_TEXT.replace(
            "| D-1 | cosmetic | typo | low | later | accepted |",
            "| D-1 | Windows | print crash | critical | fix needed | open |",
        )
        result = evaluate_acceptance_results(text)

        self.assertEqual(result["status"], "no_go")
        self.assertTrue(any("unresolved critical defect" in item for item in result["problems"]))

    def test_missing_results_file_is_no_go(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "missing.md"
            result = evaluate_file(path)

        self.assertEqual(result["status"], "no_go")
        self.assertIn("acceptance results file not found", result["problems"][0])

    def test_file_gate_requires_checkboxes_from_template(self):
        template = GO_TEXT.replace(
            "- [x] Завершение досканированного заказа прошло.",
            "- [ ] На экране статистики видно `Backend: online, список из VDS`.\n"
            "- [x] Завершение досканированного заказа прошло.",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            result_path = root / "ACCEPTANCE_RESULTS.md"
            template_path = root / "ACCEPTANCE_RESULTS_TEMPLATE.md"
            result_path.write_text(GO_TEXT, encoding="utf-8")
            template_path.write_text(template, encoding="utf-8")

            result = evaluate_file(result_path)

        self.assertEqual(result["status"], "no_go")
        self.assertIn(
            "required acceptance checkbox is missing in 4. Windows Desktop Acceptance: "
            "На экране статистики видно `Backend: online, список из VDS`.",
            result["problems"],
        )


if __name__ == "__main__":
    unittest.main()
