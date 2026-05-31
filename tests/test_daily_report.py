import json
import tempfile
import unittest
from pathlib import Path

from taksklad import main, reports


class DailyReportTests(unittest.TestCase):
    def test_report_uses_scan_backup_and_respects_undo(self):
        original_backup_dir = main.BACKUP_DIR
        original_reports_dir = main.REPORTS_DIR
        original_load_pending_saves = main.load_pending_saves
        original_reports_backup_dir = reports.BACKUP_DIR
        original_reports_reports_dir = reports.REPORTS_DIR
        original_reports_load_pending_saves = reports.load_pending_saves
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                main.BACKUP_DIR = str(tmp_path / "scan_backups")
                main.REPORTS_DIR = str(tmp_path / "reports")
                main.load_pending_saves = lambda: []
                reports.BACKUP_DIR = main.BACKUP_DIR
                reports.REPORTS_DIR = main.REPORTS_DIR
                reports.load_pending_saves = main.load_pending_saves
                Path(main.BACKUP_DIR).mkdir()

                backup_path = Path(main.BACKUP_DIR) / "scan_backup_24.05.2026.jsonl"
                rows = [
                    {
                        "timestamp": "2026-05-24 10:00:00",
                        "action": "scan",
                        "date": "24.05.2026",
                        "client": "ИП Даврон",
                        "representative": "ТП1",
                        "address": "Ташкент",
                        "product": "Chapman Brown OP 20",
                        "payment_type": "Терминал",
                        "code": "CODE-1",
                        "codes": ["CODE-1"],
                    },
                    {
                        "timestamp": "2026-05-24 10:01:00",
                        "action": "scan",
                        "date": "24.05.2026",
                        "client": "ИП Даврон",
                        "representative": "ТП1",
                        "address": "Ташкент",
                        "product": "Chapman Brown OP 20",
                        "payment_type": "Терминал",
                        "code": "CODE-2",
                        "codes": ["CODE-1", "CODE-2"],
                    },
                    {
                        "timestamp": "2026-05-24 10:02:00",
                        "action": "undo_scan",
                        "code": "CODE-1",
                    },
                    {
                        "timestamp": "2026-05-24 10:03:00",
                        "action": "position_saved",
                        "date": "24.05.2026",
                        "client": "ИП Даврон",
                        "representative": "ТП1",
                        "address": "Ташкент",
                        "product": "Chapman Brown OP 20",
                        "payment_type": "Терминал",
                        "codes": ["CODE-2", "CODE-3"],
                    },
                ]
                with backup_path.open("w", encoding="utf-8") as backup_file:
                    for row in rows:
                        backup_file.write(json.dumps(row, ensure_ascii=False) + "\n")

                report = main.create_day_report_excel(report_date="24.05.2026")

                self.assertFalse(report["empty"])
                self.assertEqual(report["total_report_rows"], 2)
                self.assertEqual(report["terminal_count"], 2)
                self.assertEqual(report["source"], "scan_backup")
                self.assertTrue(Path(report["filename"]).exists())
        finally:
            main.BACKUP_DIR = original_backup_dir
            main.REPORTS_DIR = original_reports_dir
            main.load_pending_saves = original_load_pending_saves
            reports.BACKUP_DIR = original_reports_backup_dir
            reports.REPORTS_DIR = original_reports_reports_dir
            reports.load_pending_saves = original_reports_load_pending_saves

    def test_shift_report_splits_kiz_files_by_shipment_date_with_part_number(self):
        original_backup_dir = reports.BACKUP_DIR
        original_reports_dir = reports.REPORTS_DIR
        original_load_pending_saves = reports.load_pending_saves
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                reports.BACKUP_DIR = str(tmp_path / "scan_backups")
                reports.REPORTS_DIR = str(tmp_path / "reports")
                reports.load_pending_saves = lambda: []
                Path(reports.BACKUP_DIR).mkdir()
                Path(reports.REPORTS_DIR).mkdir()

                backup_path = Path(reports.BACKUP_DIR) / "scan_backup_31.05.2026.jsonl"
                rows = [
                    {
                        "timestamp": "2026-05-31 10:00:00",
                        "action": "scan",
                        "date": "02.06.2026",
                        "client": "Client A",
                        "product": "Chapman Brown OP 20",
                        "payment_type": "Терминал",
                        "code": "CODE-A",
                    },
                    {
                        "timestamp": "2026-05-31 10:01:00",
                        "action": "scan",
                        "date": "03.06.2026",
                        "client": "Client B",
                        "product": "Chapman Red SSL 20",
                        "payment_type": "Перечисление",
                        "code": "CODE-B",
                    },
                ]
                with backup_path.open("w", encoding="utf-8") as backup_file:
                    for row in rows:
                        backup_file.write(json.dumps(row, ensure_ascii=False) + "\n")

                result = reports.create_shift_report_excels_by_order_date(scan_date="31.05.2026")

                self.assertFalse(result["empty"])
                self.assertEqual(result["total_report_rows"], 2)
                self.assertEqual(len(result["reports"]), 2)
                self.assertEqual([item["shipment_date_display"] for item in result["reports"]], ["02.06.2026", "03.06.2026"])
                self.assertEqual([item["part_number"] for item in result["reports"]], [1, 1])
                self.assertTrue(Path(result["reports"][0]["filename"]).name.endswith("_ч1.xlsx"))
                self.assertTrue(Path(result["reports"][0]["filename"]).exists())
                self.assertTrue(Path(result["reports"][1]["filename"]).exists())

                second = reports.create_shift_report_excels_by_order_date(scan_date="31.05.2026")

                self.assertEqual([item["part_number"] for item in second["reports"]], [1, 1])
                self.assertEqual([item["already_exists"] for item in second["reports"]], [True, True])

                next_day_backup_path = Path(reports.BACKUP_DIR) / "scan_backup_01.06.2026.jsonl"
                with next_day_backup_path.open("w", encoding="utf-8") as backup_file:
                    backup_file.write(json.dumps({
                        "timestamp": "2026-06-01 09:00:00",
                        "action": "scan",
                        "date": "02.06.2026",
                        "client": "Client A",
                        "product": "Chapman Brown OP 20",
                        "payment_type": "Терминал",
                        "code": "CODE-C",
                    }, ensure_ascii=False) + "\n")

                next_day_result = reports.create_shift_report_excels_by_order_date(scan_date="01.06.2026")

                self.assertEqual(len(next_day_result["reports"]), 1)
                self.assertEqual(next_day_result["reports"][0]["shipment_date_display"], "02.06.2026")
                self.assertEqual(next_day_result["reports"][0]["part_number"], 2)
                self.assertFalse(next_day_result["reports"][0]["already_exists"])
                self.assertTrue(Path(next_day_result["reports"][0]["filename"]).name.endswith("_ч2.xlsx"))
        finally:
            reports.BACKUP_DIR = original_backup_dir
            reports.REPORTS_DIR = original_reports_dir
            reports.load_pending_saves = original_load_pending_saves


if __name__ == "__main__":
    unittest.main()
