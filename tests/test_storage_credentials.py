import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from taksklad import storage
from taksklad.secret_store import (
    BACKEND_API_TOKEN_SECRET,
    MemorySecretStore,
    SecretStoreError,
    SecretStoreUnavailable,
    get_secret_store,
    reset_secret_store_for_tests,
    set_secret_store_for_tests,
)


class StorageSafetyTests(unittest.TestCase):
    def setUp(self):
        self.original_data_file = storage.TAKSKLAD_DATA_FILE
        self.original_runtime_config_file = storage.RUNTIME_CONFIG_FILE
        self.original_telegram_file = storage.TELEGRAM_SETTINGS_FILE
        self.original_geocoder_file = storage.YANDEX_GEOCODER_KEY_FILE
        self.store = MemorySecretStore()
        set_secret_store_for_tests(self.store)

    def tearDown(self):
        storage.TAKSKLAD_DATA_FILE = self.original_data_file
        storage.RUNTIME_CONFIG_FILE = self.original_runtime_config_file
        storage.TELEGRAM_SETTINGS_FILE = self.original_telegram_file
        storage.YANDEX_GEOCODER_KEY_FILE = self.original_geocoder_file
        reset_secret_store_for_tests()

    def configure_paths(self, root):
        storage.TAKSKLAD_DATA_FILE = str(root / "TakSklad_data.json")
        storage.RUNTIME_CONFIG_FILE = str(root / "runtime.json")
        storage.TELEGRAM_SETTINGS_FILE = str(root / "telegram.json")
        storage.YANDEX_GEOCODER_KEY_FILE = str(root / "geocoder.txt")

    def test_save_retries_when_replace_is_temporarily_locked(self):
        original_replace = storage.os.replace
        original_delay = storage.SAVE_RETRY_DELAY_SECONDS
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                storage.TAKSKLAD_DATA_FILE = str(Path(tmp_dir) / "TakSklad_data.json")
                storage.SAVE_RETRY_DELAY_SECONDS = 0
                calls = []

                def flaky_replace(src, dst):
                    calls.append((src, dst))
                    if len(calls) == 1:
                        raise PermissionError("locked")
                    return original_replace(src, dst)

                storage.os.replace = flaky_replace
                self.assertTrue(storage.save_app_data({"telegram_settings": {"enabled": True}}))
                self.assertEqual(len(calls), 2)
        finally:
            storage.os.replace = original_replace
            storage.SAVE_RETRY_DELAY_SECONDS = original_delay

    def test_corrupt_main_restores_last_good_backup(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            storage.TAKSKLAD_DATA_FILE = str(Path(tmp_dir) / "TakSklad_data.json")
            Path(storage.TAKSKLAD_DATA_FILE).write_text("{broken", encoding="utf-8")
            Path(storage.app_data_backup_path(1)).write_text(
                json.dumps({"pending_backend_events": [{"id": "event-1"}]}),
                encoding="utf-8",
            )
            loaded = storage.load_app_data()
        self.assertEqual(loaded["pending_backend_events"], [{"id": "event-1"}])

    def test_secret_migration_moves_backend_token_and_scrubs_runtime_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.configure_paths(root)
            Path(storage.RUNTIME_CONFIG_FILE).write_text(
                json.dumps({"TAKSKLAD_BACKEND_API_TOKEN": "synthetic-token", "TAKSKLAD_BACKEND_BASE_URL": "https://example.test"}),
                encoding="utf-8",
            )
            result = storage.migrate_desktop_secrets(self.store, allow_volatile_test_store=True)
            runtime = json.loads(Path(storage.RUNTIME_CONFIG_FILE).read_text(encoding="utf-8"))

        self.assertEqual(result["migrated"], 1)
        self.assertEqual(self.store.get_text(BACKEND_API_TOKEN_SECRET), "synthetic-token")
        self.assertNotIn("TAKSKLAD_BACKEND_API_TOKEN", runtime)

    def test_existing_secure_backend_token_is_authoritative_and_legacy_sources_are_purged(self):
        scoped = "tks." + "a" * 32 + "." + "b" * 43
        legacy = "synthetic-old-legacy-token"
        self.store.set_text(BACKEND_API_TOKEN_SECRET, scoped)
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.configure_paths(root)
            state = {
                "backend_api_token": legacy,
                "TAKSKLAD_BACKEND_API_TOKEN": legacy,
                "orders": [],
            }
            Path(storage.TAKSKLAD_DATA_FILE).write_text(json.dumps(state), encoding="utf-8")
            Path(storage.RUNTIME_CONFIG_FILE).write_text(
                json.dumps({"TAKSKLAD_API_TOKEN": legacy, "safe": "kept"}),
                encoding="utf-8",
            )
            backup_paths = []
            for index in range(1, storage.APP_DATA_BACKUP_LIMIT + 1):
                path = Path(storage.app_data_backup_path(index))
                path.write_text(json.dumps({"TAKSKLAD_API_TOKEN": legacy, "index": index}), encoding="utf-8")
                backup_paths.append(path)

            result = storage.migrate_desktop_secrets(
                self.store,
                allow_volatile_test_store=True,
            )
            sanitized = [json.loads(Path(storage.TAKSKLAD_DATA_FILE).read_text(encoding="utf-8"))]
            sanitized += [json.loads(path.read_text(encoding="utf-8")) for path in backup_paths]
            runtime = json.loads(Path(storage.RUNTIME_CONFIG_FILE).read_text(encoding="utf-8"))

        self.assertEqual(self.store.get_text(BACKEND_API_TOKEN_SECRET), scoped)
        self.assertFalse(result["restart_required"])
        self.assertEqual(result["migrated"], 0)
        self.assertEqual(runtime, {"safe": "kept"})
        for payload in sanitized:
            rendered = json.dumps(payload)
            self.assertNotIn(legacy, rendered)
            self.assertNotIn("backend_api_token", rendered.casefold())

    def test_cleanup_failure_never_replaces_authoritative_secure_backend_token(self):
        scoped = "tks." + "c" * 32 + "." + "d" * 43
        legacy = "synthetic-old-legacy-token"
        self.store.set_text(BACKEND_API_TOKEN_SECRET, scoped)
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.configure_paths(root)
            Path(storage.RUNTIME_CONFIG_FILE).write_text(
                json.dumps({"TAKSKLAD_BACKEND_API_TOKEN": legacy}),
                encoding="utf-8",
            )

            def fail_cleanup(stage):
                if stage == "after_runtime_sanitize":
                    raise OSError("synthetic cleanup failure")

            with mock.patch.object(storage, "_storage_fault_hook", side_effect=fail_cleanup):
                with self.assertRaises(storage.SecretMigrationError) as captured:
                    storage.migrate_desktop_secrets(
                        self.store,
                        allow_volatile_test_store=True,
                    )
            restored_runtime = json.loads(Path(storage.RUNTIME_CONFIG_FILE).read_text(encoding="utf-8"))

        self.assertEqual(self.store.get_text(BACKEND_API_TOKEN_SECRET), scoped)
        self.assertEqual(restored_runtime["TAKSKLAD_BACKEND_API_TOKEN"], legacy)
        self.assertNotIn(scoped, str(captured.exception))

    def test_repeated_migration_never_overwrites_existing_secure_backend_token(self):
        scoped = "tks." + "e" * 32 + "." + "f" * 43
        legacy = "synthetic-old-legacy-token"
        self.store.set_text(BACKEND_API_TOKEN_SECRET, scoped)
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.configure_paths(root)
            Path(storage.RUNTIME_CONFIG_FILE).write_text(
                json.dumps({"TAKSKLAD_BACKEND_API_TOKEN": legacy}), encoding="utf-8"
            )
            first = storage.migrate_desktop_secrets(self.store, allow_volatile_test_store=True)
            second = storage.migrate_desktop_secrets(self.store, allow_volatile_test_store=True)

        self.assertEqual(self.store.get_text(BACKEND_API_TOKEN_SECRET), scoped)
        self.assertFalse(first["restart_required"])
        self.assertFalse(second["restart_required"])

    def test_non_windows_provider_requires_explicit_selection(self):
        reset_secret_store_for_tests()
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SecretStoreUnavailable):
                get_secret_store()

    def test_environment_provider_is_read_only(self):
        reset_secret_store_for_tests()
        with mock.patch.dict(os.environ, {
            "TAKSKLAD_SECRET_STORE_PROVIDER": "environment",
            "TAKSKLAD_SECRET_STORE_MODE": "test",
            "TAKSKLAD_BACKEND_API_TOKEN": "synthetic-env-token",
        }, clear=True):
            store = get_secret_store()
            self.assertEqual(store.get_text(BACKEND_API_TOKEN_SECRET), "synthetic-env-token")
            with self.assertRaises(SecretStoreError):
                store.set_text(BACKEND_API_TOKEN_SECRET, "replacement")


if __name__ == "__main__":
    unittest.main()
