import json
import tempfile
import unittest
from pathlib import Path

from taksklad import startup_check, storage


def credentials():
    return {
        "type": "service_account",
        "client_email": "service-account@example.com",
        "private_key": "private-key",
    }


class StartupCheckTests(unittest.TestCase):
    def setUp(self):
        self.original_data_file = storage.TAKSKLAD_DATA_FILE
        self.original_credentials_file = storage.CREDENTIALS_FILE
        self.original_backend_enabled = startup_check.TAKSKLAD_BACKEND_ENABLED
        self.original_backend_read = startup_check.TAKSKLAD_BACKEND_READ_ORDERS_ENABLED
        self.original_backend_url = startup_check.TAKSKLAD_BACKEND_BASE_URL
        self.original_backend_token = startup_check.TAKSKLAD_BACKEND_API_TOKEN
        self.original_geocoder_loader = startup_check.load_yandex_geocoder_key

    def tearDown(self):
        storage.TAKSKLAD_DATA_FILE = self.original_data_file
        storage.CREDENTIALS_FILE = self.original_credentials_file
        startup_check.TAKSKLAD_BACKEND_ENABLED = self.original_backend_enabled
        startup_check.TAKSKLAD_BACKEND_READ_ORDERS_ENABLED = self.original_backend_read
        startup_check.TAKSKLAD_BACKEND_BASE_URL = self.original_backend_url
        startup_check.TAKSKLAD_BACKEND_API_TOKEN = self.original_backend_token
        startup_check.load_yandex_geocoder_key = self.original_geocoder_loader

    def test_build_startup_self_check_redacts_runtime_secrets(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            storage.TAKSKLAD_DATA_FILE = str(tmp_path / "TakSklad_data.json")
            storage.CREDENTIALS_FILE = str(tmp_path / "credentials.json")
            Path(storage.TAKSKLAD_DATA_FILE).write_text(
                json.dumps(
                    {
                        "credentials": credentials(),
                        "telegram_settings": {
                            "enabled": True,
                            "bot_token": "telegram-token",
                            "chat_ids": ["111", "222"],
                        },
                        "pending_saves": [{"code": "secret-code"}],
                        "pending_prints": [{}],
                        "pending_backend_events": [{}, {}],
                        "pending_telegram": [],
                    }
                ),
                encoding="utf-8",
            )
            startup_check.TAKSKLAD_BACKEND_ENABLED = True
            startup_check.TAKSKLAD_BACKEND_READ_ORDERS_ENABLED = True
            startup_check.TAKSKLAD_BACKEND_BASE_URL = "https://api.taksklad.uz/path"
            startup_check.TAKSKLAD_BACKEND_API_TOKEN = "backend-token"
            startup_check.load_yandex_geocoder_key = lambda: "geocoder-key"

            check = startup_check.build_startup_self_check()
            formatted = startup_check.format_startup_self_check(check)

            self.assertEqual(check["build_label"], "MVP 2.0")
            self.assertEqual(check["credentials"], "stored")
            self.assertEqual(check["telegram_enabled"], "yes")
            self.assertEqual(check["telegram_token"], "yes")
            self.assertEqual(check["telegram_chats"], "2")
            self.assertEqual(check["backend_origin"], "https://api.taksklad.uz")
            self.assertEqual(check["backend_token"], "yes")
            self.assertEqual(check["geocoder_key"], "yes")
            self.assertEqual(check["pending_backend_events"], "2")
            self.assertNotIn("telegram-token", formatted)
            self.assertNotIn("backend-token", formatted)
            self.assertNotIn("geocoder-key", formatted)
            self.assertNotIn("secret-code", formatted)

    def test_app_version_label_contains_mvp_marker(self):
        label = startup_check.format_app_version_label()

        self.assertIn("Версия:", label)
        self.assertIn(startup_check.APP_VERSION, label)
        self.assertIn("MVP 2.0", label)

    def test_credentials_status_falls_back_to_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            storage.TAKSKLAD_DATA_FILE = str(tmp_path / "TakSklad_data.json")
            storage.CREDENTIALS_FILE = str(tmp_path / "credentials.json")
            Path(storage.TAKSKLAD_DATA_FILE).write_text("{}", encoding="utf-8")
            Path(storage.CREDENTIALS_FILE).write_text(json.dumps(credentials()), encoding="utf-8")

            self.assertEqual(startup_check.credentials_status(storage.load_app_data()), "file")


if __name__ == "__main__":
    unittest.main()
