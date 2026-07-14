import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from taksklad import single_instance, startup_check, storage
from taksklad.secret_store import (
    BACKEND_API_TOKEN_SECRET,
    GOOGLE_CREDENTIALS_SECRET,
    TELEGRAM_BOT_TOKEN_SECRET,
    MemorySecretStore,
    SecretStoreError,
    reset_secret_store_for_tests,
    set_secret_store_for_tests,
)


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
        self.original_backend_only_refresh = startup_check.TAKSKLAD_BACKEND_ONLY_REFRESH
        self.original_backend_emergency_google_fallback = startup_check.TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED
        self.original_backend_url = startup_check.TAKSKLAD_BACKEND_BASE_URL
        self.original_telegram_desktop_polling = startup_check.TELEGRAM_DESKTOP_POLLING_ENABLED
        self.original_geocoder_loader = startup_check.load_yandex_geocoder_key
        self.secret_store = MemorySecretStore()
        set_secret_store_for_tests(self.secret_store)

    def tearDown(self):
        storage.TAKSKLAD_DATA_FILE = self.original_data_file
        storage.CREDENTIALS_FILE = self.original_credentials_file
        startup_check.TAKSKLAD_BACKEND_ENABLED = self.original_backend_enabled
        startup_check.TAKSKLAD_BACKEND_READ_ORDERS_ENABLED = self.original_backend_read
        startup_check.TAKSKLAD_BACKEND_ONLY_REFRESH = self.original_backend_only_refresh
        startup_check.TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED = self.original_backend_emergency_google_fallback
        startup_check.TAKSKLAD_BACKEND_BASE_URL = self.original_backend_url
        startup_check.TELEGRAM_DESKTOP_POLLING_ENABLED = self.original_telegram_desktop_polling
        startup_check.load_yandex_geocoder_key = self.original_geocoder_loader
        reset_secret_store_for_tests()

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
            self.secret_store.set_text(GOOGLE_CREDENTIALS_SECRET, json.dumps(credentials()))
            self.secret_store.set_text(TELEGRAM_BOT_TOKEN_SECRET, "telegram-token")
            self.secret_store.set_text(BACKEND_API_TOKEN_SECRET, "backend-token")
            startup_check.TAKSKLAD_BACKEND_ENABLED = True
            startup_check.TAKSKLAD_BACKEND_READ_ORDERS_ENABLED = True
            startup_check.TAKSKLAD_BACKEND_ONLY_REFRESH = True
            startup_check.TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED = False
            startup_check.TAKSKLAD_BACKEND_BASE_URL = "https://api.taksklad.uz/path"
            startup_check.TELEGRAM_DESKTOP_POLLING_ENABLED = False
            startup_check.load_yandex_geocoder_key = lambda: "geocoder-key"

            check = startup_check.build_startup_self_check()
            formatted = startup_check.format_startup_self_check(check)

            self.assertEqual(check["build_label"], "MVP 2.0")
            self.assertRegex(check["workstation_id"], r"^[a-f0-9]{12}$")
            self.assertEqual(check["version_status"], "checking")
            self.assertEqual(check["app_data_status"], "ok")
            self.assertEqual(check["app_data_restored"], "no")
            self.assertEqual(check["credentials"], "secure_store")
            self.assertEqual(check["telegram_enabled"], "yes")
            self.assertEqual(check["telegram_token"], "yes")
            self.assertEqual(check["telegram_chats"], "2")
            self.assertEqual(check["telegram_desktop_polling"], "no")
            self.assertEqual(check["backend_only_refresh"], "yes")
            self.assertEqual(check["backend_emergency_google_fallback"], "no")
            self.assertEqual(check["backend_origin"], "https://api.taksklad.uz")
            self.assertEqual(check["backend_token"], "yes")
            self.assertEqual(check["geocoder_key"], "yes")
            self.assertEqual(check["pending_backend_events"], "2")
            self.assertIn("telegram_desktop_polling=no", formatted)
            self.assertIn("backend_only_refresh=yes", formatted)
            self.assertIn("backend_emergency_google_fallback=no", formatted)
            self.assertIn("app_data_status=ok", formatted)
            self.assertNotIn("telegram-token", formatted)
            self.assertNotIn("backend-token", formatted)
            self.assertNotIn("geocoder-key", formatted)
            self.assertNotIn("secret-code", formatted)

    def test_secret_store_failure_is_not_reported_as_configured(self):
        with mock.patch.object(
            startup_check,
            "load_secret",
            side_effect=SecretStoreError("synthetic access denial"),
        ):
            self.assertFalse(startup_check.secret_available(BACKEND_API_TOKEN_SECRET))

    def test_version_update_status_current_manifest_is_non_blocking(self):
        status = startup_check.build_version_update_status(
            {
                "latest_version": startup_check.APP_VERSION,
                "min_supported_version": startup_check.APP_VERSION,
                "mandatory": True,
                "block_workflow": True,
                "package_type": "onefile_exe",
            }
        )
        label = startup_check.format_version_update_status_label(status)

        self.assertEqual(status["state"], "current")
        self.assertEqual(status["blocking"], "no")
        self.assertIn("актуальная", label)
        self.assertIn(startup_check.APP_VERSION, label)
        self.assertIn("ПК", label)

    def test_version_update_status_blocks_below_min_supported(self):
        status = startup_check.build_version_update_status(
            {
                "latest_version": "2.0.25",
                "min_supported_version": "2.0.25",
                "mandatory": True,
                "block_workflow": True,
                "package_type": "onefile_exe",
            },
            current_version="2.0.24",
        )
        label = startup_check.format_version_update_status_label(status)

        self.assertEqual(status["state"], "blocked")
        self.assertEqual(status["blocking"], "yes")
        self.assertEqual(status["below_min_version"], "yes")
        self.assertIn("заблокирована", label)
        self.assertIn("2.0.25", label)

    def test_version_update_status_outdated_without_workflow_block_is_non_blocking(self):
        status = startup_check.build_version_update_status(
            {
                "latest_version": "2.0.33",
                "min_supported_version": startup_check.APP_VERSION,
                "mandatory": False,
                "block_workflow": False,
                "package_type": "onefile_exe",
            }
        )
        label = startup_check.format_version_update_status_label(status)

        self.assertEqual(status["state"], "outdated")
        self.assertEqual(status["blocking"], "no")
        self.assertIn("Доступно обновление 2.0.33", label)

    def test_version_update_status_mandatory_without_block_workflow_is_non_blocking(self):
        status = startup_check.build_version_update_status(
            {
                "latest_version": "2.0.33",
                "min_supported_version": startup_check.APP_VERSION,
                "mandatory": True,
                "block_workflow": False,
                "package_type": "onefile_exe",
            }
        )

        self.assertEqual(status["state"], "outdated")
        self.assertEqual(status["mandatory"], "yes")
        self.assertEqual(status["block_workflow"], "no")
        self.assertEqual(status["blocking"], "no")

    def test_version_update_status_unavailable_uses_error_class_only(self):
        error = RuntimeError("token=secret-token Authorization: Bearer secret")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            storage.TAKSKLAD_DATA_FILE = str(tmp_path / "TakSklad_data.json")
            storage.CREDENTIALS_FILE = str(tmp_path / "credentials.json")
            Path(storage.TAKSKLAD_DATA_FILE).write_text("{}", encoding="utf-8")

            status = startup_check.build_version_update_status(error=error)
            check = startup_check.build_startup_self_check(status)
            label = startup_check.format_version_update_status_label(status)
            formatted = startup_check.format_startup_self_check(check)

        self.assertEqual(status["state"], "unavailable")
        self.assertEqual(status["error_class"], "RuntimeError")
        self.assertIn("RuntimeError", label)
        self.assertIn("version_error_class=RuntimeError", formatted)
        self.assertNotIn("secret-token", label)
        self.assertNotIn("Authorization", label)
        self.assertNotIn("secret-token", formatted)
        self.assertNotIn("Authorization", formatted)

    def test_app_version_label_contains_mvp_marker(self):
        label = startup_check.format_app_version_label()

        self.assertIn("Версия:", label)
        self.assertIn(startup_check.APP_VERSION, label)
        self.assertIn("MVP 2.0", label)

    def test_credentials_status_does_not_fall_back_to_plaintext_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            storage.TAKSKLAD_DATA_FILE = str(tmp_path / "TakSklad_data.json")
            storage.CREDENTIALS_FILE = str(tmp_path / "credentials.json")
            Path(storage.TAKSKLAD_DATA_FILE).write_text("{}", encoding="utf-8")
            Path(storage.CREDENTIALS_FILE).write_text(json.dumps(credentials()), encoding="utf-8")

            self.assertEqual(startup_check.credentials_status(storage.load_app_data()), "missing")

    def test_startup_self_check_reports_degraded_app_data_without_payload_values(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            storage.TAKSKLAD_DATA_FILE = str(tmp_path / "TakSklad_data.json")
            storage.CREDENTIALS_FILE = str(tmp_path / "credentials.json")
            Path(storage.TAKSKLAD_DATA_FILE).write_text("{broken-json token-secret", encoding="utf-8")

            check = startup_check.build_startup_self_check()
            formatted = startup_check.format_startup_self_check(check)

            self.assertEqual(check["app_data_status"], "degraded")
            self.assertEqual(check["app_data_restored"], "no")
            self.assertIn("app_data_status=degraded", formatted)
            self.assertNotIn("token-secret", formatted)


class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_is_rejected_with_operator_safe_message(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            first = single_instance.acquire_single_instance_lock(
                app_dir=tmp_dir,
                now=1_000,
                process_running_func=lambda pid: True,
            )
            second = single_instance.acquire_single_instance_lock(
                app_dir=tmp_dir,
                now=1_010,
                process_running_func=lambda pid: True,
            )

            self.assertTrue(first.acquired)
            self.assertFalse(second.acquired)
            self.assertEqual(second.reason, "already_running")
            self.assertIn("уже запущен", second.message)
            self.assertIn("локальные очереди сканов", second.message)
            self.assertNotIn(tmp_dir, second.message)

            single_instance.release_single_instance_lock(first.lock)

    def test_dead_pid_lock_is_recovered(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_path = Path(single_instance.single_instance_lock_path(tmp_dir))
            lock_path.write_text(
                json.dumps({"owner_id": "old", "pid": 999999, "updated_ts": 1_000}),
                encoding="utf-8",
            )

            result = single_instance.acquire_single_instance_lock(
                app_dir=tmp_dir,
                now=1_010,
                process_running_func=lambda pid: False,
            )

            self.assertTrue(result.acquired)
            self.assertTrue(result.recovered)
            self.assertEqual(result.reason, "acquired_after_stale_recovery")
            single_instance.release_single_instance_lock(result.lock)

    def test_live_pid_lock_is_not_recovered_even_when_old(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_path = Path(single_instance.single_instance_lock_path(tmp_dir))
            lock_path.write_text(
                json.dumps({"owner_id": "old", "pid": 123, "updated_ts": 1_000}),
                encoding="utf-8",
            )

            result = single_instance.acquire_single_instance_lock(
                app_dir=tmp_dir,
                now=1_000 + single_instance.SINGLE_INSTANCE_LOCK_STALE_SECONDS + 1,
                process_running_func=lambda pid: True,
            )

            self.assertFalse(result.acquired)
            self.assertFalse(result.recovered)
            self.assertEqual(result.existing["pid"], 123)

    def test_no_pid_lock_is_recovered_by_ttl(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_path = Path(single_instance.single_instance_lock_path(tmp_dir))
            lock_path.write_text(
                json.dumps({"owner_id": "old", "updated_ts": 1_000}),
                encoding="utf-8",
            )

            result = single_instance.acquire_single_instance_lock(
                app_dir=tmp_dir,
                now=1_000 + single_instance.SINGLE_INSTANCE_LOCK_STALE_SECONDS + 1,
                process_running_func=lambda pid: True,
            )

            self.assertTrue(result.acquired)
            self.assertTrue(result.recovered)
            single_instance.release_single_instance_lock(result.lock)

    def test_release_removes_only_owned_lock(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = single_instance.acquire_single_instance_lock(app_dir=tmp_dir, now=1_000)
            lock_path = Path(result.lock.path)

            self.assertTrue(single_instance.release_single_instance_lock(result.lock))
            self.assertFalse(lock_path.exists())

            other = single_instance.acquire_single_instance_lock(app_dir=tmp_dir, now=1_100)
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            payload["owner_id"] = "other-owner"
            lock_path.write_text(json.dumps(payload), encoding="utf-8")

            self.assertFalse(single_instance.release_single_instance_lock(other.lock))
            self.assertTrue(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
