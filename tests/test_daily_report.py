import json
import tempfile
import unittest
from pathlib import Path

from taksklad import reports


class DailyReportTests(unittest.TestCase):
    def test_report_uses_local_scan_backup_and_respects_undo(self):
        original_backup_dir = reports.BACKUP_DIR
        original_reports_dir = reports.REPORTS_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                reports.BACKUP_DIR = str(tmp_path / "scan_backups")
                reports.REPORTS_DIR = str(tmp_path / "reports")
                Path(reports.BACKUP_DIR).mkdir()
                backup_path = Path(reports.BACKUP_DIR) / "scan_backup_24.05.2026.jsonl"
                rows = [
                    {"timestamp": "2026-05-24 10:00:00", "action": "scan", "date": "24.05.2026", "client": "Client", "product": "Chapman Brown OP 20", "payment_type": "Терминал", "code": "CODE-1"},
                    {"timestamp": "2026-05-24 10:01:00", "action": "scan", "date": "24.05.2026", "client": "Client", "product": "Chapman Brown OP 20", "payment_type": "Терминал", "code": "CODE-2"},
                    {"timestamp": "2026-05-24 10:02:00", "action": "undo_scan", "code": "CODE-1"},
                ]
                backup_path.write_text(
                    "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                    encoding="utf-8",
                )

                report = reports.create_day_report_excel(report_date="24.05.2026")

                self.assertEqual(report["source"], "scan_backup")
                self.assertEqual(report["total_report_rows"], 1)
                self.assertTrue(Path(report["filename"]).exists())
        finally:
            reports.BACKUP_DIR = original_backup_dir
            reports.REPORTS_DIR = original_reports_dir

    def test_shift_report_splits_files_by_shipment_date(self):
        original_backup_dir = reports.BACKUP_DIR
        original_reports_dir = reports.REPORTS_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                reports.BACKUP_DIR = str(tmp_path / "scan_backups")
                reports.REPORTS_DIR = str(tmp_path / "reports")
                Path(reports.BACKUP_DIR).mkdir()
                Path(reports.REPORTS_DIR).mkdir()
                backup_path = Path(reports.BACKUP_DIR) / "scan_backup_31.05.2026.jsonl"
                rows = [
                    {"timestamp": "2026-05-31 10:00:00", "action": "scan", "date": "02.06.2026", "client": "A", "product": "Chapman Brown OP 20", "payment_type": "Терминал", "code": "CODE-A"},
                    {"timestamp": "2026-05-31 10:01:00", "action": "scan", "date": "03.06.2026", "client": "B", "product": "Chapman Red SSL 20", "payment_type": "Перечисление", "code": "CODE-B"},
                ]
                backup_path.write_text(
                    "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                    encoding="utf-8",
                )

                result = reports.create_shift_report_excels_by_order_date(scan_date="31.05.2026")

                self.assertEqual(result["total_report_rows"], 2)
                self.assertEqual(
                    [item["shipment_date_display"] for item in result["reports"]],
                    ["02.06.2026", "03.06.2026"],
                )
        finally:
            reports.BACKUP_DIR = original_backup_dir
            reports.REPORTS_DIR = original_reports_dir


if __name__ == "__main__":
    unittest.main()
