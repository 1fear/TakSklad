import json
import tempfile
import unittest
from pathlib import Path

import storage


def credentials(email, private_key):
    return {
        "type": "service_account",
        "client_email": email,
        "private_key": private_key,
    }


class StorageCredentialsTests(unittest.TestCase):
    def test_credentials_json_takes_priority_over_stored_credentials(self):
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

                self.assertEqual(storage.load_credentials_data(), file_credentials)
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


if __name__ == "__main__":
    unittest.main()
