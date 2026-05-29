import json
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
    def test_stored_credentials_take_priority_over_credentials_json(self):
        original_credentials_file = storage.CREDENTIALS_FILE
        original_data_file = storage.TAKSKLAD_DATA_FILE
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
            storage.TAKSKLAD_DATA_FILE = original_data_file

    def test_stored_credentials_are_used_when_file_is_missing(self):
        original_credentials_file = storage.CREDENTIALS_FILE
        original_data_file = storage.TAKSKLAD_DATA_FILE
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
            storage.TAKSKLAD_DATA_FILE = original_data_file

    def test_save_app_data_retries_when_replace_is_temporarily_locked(self):
        original_data_file = storage.TAKSKLAD_DATA_FILE
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
            storage.TAKSKLAD_DATA_FILE = original_data_file
            storage.os.replace = original_replace
            storage.SAVE_RETRY_DELAY_SECONDS = original_delay


if __name__ == "__main__":
    unittest.main()
