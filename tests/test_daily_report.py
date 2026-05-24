import json
import tempfile
import unittest
from pathlib import Path

import main


class DailyReportTests(unittest.TestCase):
    def test_report_uses_scan_backup_and_respects_undo(self):
        original_backup_dir = main.BACKUP_DIR
        original_reports_dir = main.REPORTS_DIR
        original_load_pending_saves = main.load_pending_saves
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                main.BACKUP_DIR = str(tmp_path / "scan_backups")
                main.REPORTS_DIR = str(tmp_path / "reports")
                main.load_pending_saves = lambda: []
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


if __name__ == "__main__":
    unittest.main()
