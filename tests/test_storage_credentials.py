import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from taksklad import storage


def credentials(email, private_key):
    return {
        "type": "service_account",
        "client_email": email,
        "private_key": private_key,
    }


class StorageCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.original_data_file = storage.TAKSKLAD_DATA_FILE
        self.original_backup_limit = storage.APP_DATA_BACKUP_LIMIT

    def tearDown(self):
        storage.TAKSKLAD_DATA_FILE = self.original_data_file
        storage.APP_DATA_BACKUP_LIMIT = self.original_backup_limit

    def test_stored_credentials_take_priority_over_credentials_json(self):
        original_credentials_file = storage.CREDENTIALS_FILE
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                storage.CREDENTIALS_FILE = str(tmp_path / "credentials.json")
                storage.TAKSKLAD_DATA_FILE = str(tmp_path / "TakSklad_data.json")

                file_credentials = credentials("fresh@example.com", "fresh-key")
                stored_credentials = credentials("stale@example.com", "stale-key")

                Path(storage.CREDENTIALS_FILE).write_text(
                    json.dumps(file_credentials),
                    encoding="utf-8",
                )
                Path(storage.TAKSKLAD_DATA_FILE).write_text(
                    json.dumps({"credentials": stored_credentials}),
                    encoding="utf-8",
                )

                self.assertEqual(storage.load_credentials_data(), stored_credentials)
                self.assertTrue(storage.credentials_available())
        finally:
            storage.CREDENTIALS_FILE = original_credentials_file

    def test_stored_credentials_are_used_when_file_is_missing(self):
        original_credentials_file = storage.CREDENTIALS_FILE
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                storage.CREDENTIALS_FILE = str(tmp_path / "credentials.json")
                storage.TAKSKLAD_DATA_FILE = str(tmp_path / "TakSklad_data.json")

                stored_credentials = credentials("stored@example.com", "stored-key")
                Path(storage.TAKSKLAD_DATA_FILE).write_text(
                    json.dumps({"credentials": stored_credentials}),
                    encoding="utf-8",
                )

                self.assertEqual(storage.load_credentials_data(), stored_credentials)
        finally:
            storage.CREDENTIALS_FILE = original_credentials_file

    def test_save_app_data_retries_when_replace_is_temporarily_locked(self):
        original_replace = storage.os.replace
        original_delay = storage.SAVE_RETRY_DELAY_SECONDS
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                storage.TAKSKLAD_DATA_FILE = str(tmp_path / "TakSklad_data.json")
                storage.SAVE_RETRY_DELAY_SECONDS = 0
                calls = []

                def flaky_replace(src, dst):
                    calls.append((src, dst))
                    if len(calls) == 1:
                        raise PermissionError("file is temporarily locked")
                    return original_replace(src, dst)

                storage.os.replace = flaky_replace

                self.assertTrue(storage.save_app_data({"telegram_settings": {"enabled": True}}))
                self.assertEqual(len(calls), 2)
                saved = json.loads(Path(storage.TAKSKLAD_DATA_FILE).read_text(encoding="utf-8"))
                self.assertEqual(saved["telegram_settings"], {"enabled": True})
        finally:
            storage.os.replace = original_replace
            storage.SAVE_RETRY_DELAY_SECONDS = original_delay

    def test_load_app_data_restores_corrupt_main_from_last_good_backup(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            storage.TAKSKLAD_DATA_FILE = str(tmp_path / "TakSklad_data.json")
            valid_backup = {
                "pending_saves": [{"code": "TEST-SECRET-CODE", "client": "SECRET CLIENT"}],
                "pending_prints": [{"address": "SECRET ADDRESS"}],
                "pending_backend_events": [{}, {}],
                "pending_telegram": [],
            }
            Path(storage.TAKSKLAD_DATA_FILE).write_text("{broken-json", encoding="utf-8")
            Path(storage.app_data_backup_path(1)).write_text(json.dumps(valid_backup), encoding="utf-8")

            with self.assertLogs(level="WARNING") as logs:
                data = storage.load_app_data()

            self.assertEqual(len(data["pending_saves"]), 1)
            self.assertEqual(len(data["pending_prints"]), 1)
            self.assertEqual(len(data["pending_backend_events"]), 2)
            restored = json.loads(Path(storage.TAKSKLAD_DATA_FILE).read_text(encoding="utf-8"))
            self.assertEqual(restored["pending_backend_events"], [{}, {}])
            status = storage.get_app_data_recovery_status()
            self.assertEqual(status["status"], "restored")
            self.assertEqual(status["queue_counts"]["pending_saves"], 1)

            log_text = "\n".join(logs.output)
            self.assertIn("before_restore pending_saves=0", log_text)
            self.assertIn("after_restore pending_saves=1", log_text)
            self.assertIn("pending_saves=1", log_text)
            self.assertIn("pending_backend_events=2", log_text)
            self.assertNotIn("TEST-SECRET-CODE", log_text)
            self.assertNotIn("SECRET CLIENT", log_text)
            self.assertNotIn("SECRET ADDRESS", log_text)

    def test_load_app_data_restores_missing_main_from_last_good_backup(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            storage.TAKSKLAD_DATA_FILE = str(tmp_path / "TakSklad_data.json")
            backup_data = {
                "pending_saves": [{"code": "TEST-SECRET-CODE"}],
                "pending_prints": [],
                "pending_backend_events": [{}],
                "pending_telegram": [{}, {}],
            }
            Path(storage.app_data_backup_path(1)).write_text(json.dumps(backup_data), encoding="utf-8")

            with self.assertLogs(level="WARNING") as logs:
                data = storage.load_app_data()

            self.assertEqual(len(data["pending_saves"]), 1)
            self.assertEqual(len(data["pending_backend_events"]), 1)
            self.assertEqual(len(data["pending_telegram"]), 2)
            self.assertTrue(Path(storage.TAKSKLAD_DATA_FILE).exists())
            status = storage.get_app_data_recovery_status()
            self.assertEqual(status["status"], "restored")
            log_text = "\n".join(logs.output)
            self.assertIn("before_restore pending_saves=0", log_text)
            self.assertIn("after_restore pending_saves=1", log_text)
            self.assertNotIn("TEST-SECRET-CODE", log_text)

    def test_load_app_data_restores_non_dict_main_from_last_good_backup(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            storage.TAKSKLAD_DATA_FILE = str(tmp_path / "TakSklad_data.json")
            backup_data = {"pending_saves": [{}, {}], "pending_backend_events": []}
            Path(storage.TAKSKLAD_DATA_FILE).write_text("[]", encoding="utf-8")
            Path(storage.app_data_backup_path(1)).write_text(json.dumps(backup_data), encoding="utf-8")

            data = storage.load_app_data()

            self.assertEqual(len(data["pending_saves"]), 2)
            self.assertEqual(storage.get_app_data_recovery_status()["status"], "restored")

    def test_load_app_data_without_valid_backup_sets_degraded_status(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            storage.TAKSKLAD_DATA_FILE = str(tmp_path / "TakSklad_data.json")
            Path(storage.TAKSKLAD_DATA_FILE).write_text("[]", encoding="utf-8")

            with self.assertLogs(level="ERROR") as logs:
                data = storage.load_app_data()

            self.assertEqual(data["pending_saves"], [])
            status = storage.get_app_data_recovery_status()
            self.assertEqual(status["status"], "degraded")
            self.assertEqual(status["queue_counts"]["pending_saves"], 0)
            self.assertIn("valid last-good backup не найден", "\n".join(logs.output))

    def test_load_app_data_skips_invalid_first_backup_and_uses_next_valid_backup(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            storage.TAKSKLAD_DATA_FILE = str(tmp_path / "TakSklad_data.json")
            Path(storage.TAKSKLAD_DATA_FILE).write_text("{broken-json", encoding="utf-8")
            Path(storage.app_data_backup_path(1)).write_text("{broken-backup", encoding="utf-8")
            Path(storage.app_data_backup_path(2)).write_text(
                json.dumps({"pending_saves": [{}, {}, {}]}),
                encoding="utf-8",
            )

            data = storage.load_app_data()

            self.assertEqual(len(data["pending_saves"]), 3)
            self.assertEqual(storage.get_app_data_recovery_status()["restored_from"], storage.app_data_backup_path(2))

    def test_save_app_data_creates_last_good_backup_before_replace(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            storage.TAKSKLAD_DATA_FILE = str(tmp_path / "TakSklad_data.json")
            previous = {
                "pending_saves": [{"code": "TEST-SECRET-CODE"}],
                "pending_prints": [],
                "pending_backend_events": [{}],
                "telegram_settings": {"enabled": True},
            }
            Path(storage.TAKSKLAD_DATA_FILE).write_text(json.dumps(previous), encoding="utf-8")

            with self.assertLogs(level="INFO") as logs:
                self.assertTrue(storage.save_app_data({"pending_saves": []}))

            backup = json.loads(Path(storage.app_data_backup_path(1)).read_text(encoding="utf-8"))
            current = json.loads(Path(storage.TAKSKLAD_DATA_FILE).read_text(encoding="utf-8"))
            self.assertEqual(backup["pending_saves"], previous["pending_saves"])
            self.assertEqual(backup["pending_backend_events"], previous["pending_backend_events"])
            self.assertEqual(current["pending_saves"], [])
            self.assertTrue(storage.app_data_backup_path(1).endswith("TakSklad_data.json.last_good.1.bak"))

            log_text = "\n".join(logs.output)
            self.assertIn("pending_saves=1", log_text)
            self.assertIn("pending_backend_events=1", log_text)
            self.assertNotIn("TEST-SECRET-CODE", log_text)

            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(Path(storage.TAKSKLAD_DATA_FILE).stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(Path(storage.app_data_backup_path(1)).stat().st_mode), 0o600)

    def test_save_app_data_rotates_last_good_backups(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            storage.TAKSKLAD_DATA_FILE = str(tmp_path / "TakSklad_data.json")
            storage.APP_DATA_BACKUP_LIMIT = 3
            Path(storage.app_data_backup_path(1)).write_text(json.dumps({"marker": "old-1"}), encoding="utf-8")
            Path(storage.app_data_backup_path(2)).write_text(json.dumps({"marker": "old-2"}), encoding="utf-8")
            Path(storage.TAKSKLAD_DATA_FILE).write_text(json.dumps({"marker": "current"}), encoding="utf-8")

            self.assertTrue(storage.save_app_data({"marker": "new"}))

            self.assertEqual(json.loads(Path(storage.app_data_backup_path(1)).read_text(encoding="utf-8"))["marker"], "current")
            self.assertEqual(json.loads(Path(storage.app_data_backup_path(2)).read_text(encoding="utf-8"))["marker"], "old-1")
            self.assertEqual(json.loads(Path(storage.app_data_backup_path(3)).read_text(encoding="utf-8"))["marker"], "old-2")
            self.assertFalse(list(tmp_path.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
