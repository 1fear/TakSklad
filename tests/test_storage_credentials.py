import json
import os
import stat
import tempfile
import unittest
import sys
from pathlib import Path
from unittest import mock

from taksklad import storage
from taksklad.secret_store import (
    BACKEND_API_TOKEN_SECRET,
    GEOCODER_API_KEY_SECRET,
    GOOGLE_CREDENTIALS_SECRET,
    TELEGRAM_BOT_TOKEN_SECRET,
    MemorySecretStore,
    SecretStoreError,
    SecretStoreUnavailable,
    get_secret_store,
    reset_secret_store_for_tests,
    set_secret_store_for_tests,
)


def credentials(email, private_key):
    return {
        "type": "service_account",
        "client_email": email,
        "private_key": private_key,
    }


def synthetic_sentinel():
    return "TAKSKLAD" + "_SYNTHETIC_" + "SECRET_SENTINEL_V1"


class StorageCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.original_data_file = storage.TAKSKLAD_DATA_FILE
        self.original_backup_limit = storage.APP_DATA_BACKUP_LIMIT
        self.original_credentials_file = storage.CREDENTIALS_FILE
        self.original_telegram_file = storage.TELEGRAM_SETTINGS_FILE
        self.original_runtime_config_file = storage.RUNTIME_CONFIG_FILE
        self.original_geocoder_file = storage.YANDEX_GEOCODER_KEY_FILE
        self.secret_store = MemorySecretStore()
        set_secret_store_for_tests(self.secret_store)

    def tearDown(self):
        storage.TAKSKLAD_DATA_FILE = self.original_data_file
        storage.APP_DATA_BACKUP_LIMIT = self.original_backup_limit
        storage.CREDENTIALS_FILE = self.original_credentials_file
        storage.TELEGRAM_SETTINGS_FILE = self.original_telegram_file
        storage.RUNTIME_CONFIG_FILE = self.original_runtime_config_file
        storage.YANDEX_GEOCODER_KEY_FILE = self.original_geocoder_file
        reset_secret_store_for_tests()

    def configure_secret_paths(self, tmp_path):
        storage.TAKSKLAD_DATA_FILE = str(tmp_path / "TakSklad_data.json")
        storage.CREDENTIALS_FILE = str(tmp_path / "credentials.json")
        storage.TELEGRAM_SETTINGS_FILE = str(tmp_path / "telegram_settings.json")
        storage.RUNTIME_CONFIG_FILE = str(tmp_path / "runtime.generated.json")
        storage.YANDEX_GEOCODER_KEY_FILE = str(tmp_path / "yandex_geocoder_key.txt")

    def test_secure_store_credentials_are_used_without_plaintext_fallback(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            storage.CREDENTIALS_FILE = str(tmp_path / "credentials.json")
            storage.TAKSKLAD_DATA_FILE = str(tmp_path / "TakSklad_data.json")
            secure_credentials = credentials("secure@example.test", "synthetic-private-key")
            self.secret_store.set_text(
                GOOGLE_CREDENTIALS_SECRET,
                json.dumps(secure_credentials),
            )
            Path(storage.CREDENTIALS_FILE).write_text(
                json.dumps(credentials("legacy@example.test", "legacy-key")),
                encoding="utf-8",
            )

            self.assertEqual(storage.load_credentials_data(), secure_credentials)
            self.assertTrue(storage.credentials_available())

    def test_plaintext_state_credentials_are_not_loaded(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            storage.CREDENTIALS_FILE = str(tmp_path / "credentials.json")
            storage.TAKSKLAD_DATA_FILE = str(tmp_path / "TakSklad_data.json")
            Path(storage.TAKSKLAD_DATA_FILE).write_text(
                json.dumps({"credentials": credentials("legacy@example.test", "legacy-key")}),
                encoding="utf-8",
            )

            self.assertEqual(storage.load_credentials_data(), {})
            self.assertFalse(storage.credentials_available())

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

    def test_secret_migration_verifies_roundtrip_then_scrubs_state_backups_and_files(self):
        sentinel = synthetic_sentinel()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            self.configure_secret_paths(tmp_path)
            state = {
                "credentials": credentials("synthetic@example.test", sentinel + "-google"),
                "telegram_settings": {
                    "enabled": True,
                    "bot_token": sentinel + "-telegram",
                    "chat_ids": ["synthetic-chat"],
                },
                "pending_saves": [{"id": "synthetic-queue-item"}],
            }
            Path(storage.TAKSKLAD_DATA_FILE).write_text(json.dumps(state), encoding="utf-8")
            Path(storage.app_data_backup_path(1)).write_text(json.dumps(state), encoding="utf-8")
            Path(storage.CREDENTIALS_FILE).write_text(
                json.dumps(credentials("lower-priority@example.test", sentinel + "-legacy-google")),
                encoding="utf-8",
            )
            Path(storage.TELEGRAM_SETTINGS_FILE).write_text(
                json.dumps({"enabled": True, "bot_token": sentinel + "-legacy-telegram", "send_reports": True}),
                encoding="utf-8",
            )
            Path(storage.RUNTIME_CONFIG_FILE).write_text(
                json.dumps({
                    "TAKSKLAD_BACKEND_API_TOKEN": sentinel + "-backend",
                    "TAKSKLAD_BACKEND_BASE_URL": "https://synthetic.invalid",
                }),
                encoding="utf-8",
            )
            Path(storage.YANDEX_GEOCODER_KEY_FILE).write_text(sentinel + "-geocoder", encoding="utf-8")

            status = storage.migrate_desktop_secrets(
                self.secret_store,
                allow_volatile_test_store=True,
            )
            current = json.loads(Path(storage.TAKSKLAD_DATA_FILE).read_text(encoding="utf-8"))
            backup = json.loads(Path(storage.app_data_backup_path(1)).read_text(encoding="utf-8"))
            runtime = json.loads(Path(storage.RUNTIME_CONFIG_FILE).read_text(encoding="utf-8"))
            serialized_surfaces = b"\n".join(
                path.read_bytes()
                for path in (Path(storage.TAKSKLAD_DATA_FILE), Path(storage.app_data_backup_path(1)), Path(storage.RUNTIME_CONFIG_FILE))
            )

            self.assertEqual(status["status"], "migrated_restart_required")
            self.assertEqual(status["migrated"], 4)
            self.assertEqual(current["credentials"], {})
            self.assertNotIn("bot_token", current["telegram_settings"])
            self.assertEqual(current["telegram_settings"]["chat_ids"], ["synthetic-chat"])
            self.assertEqual(current["pending_saves"], [{"id": "synthetic-queue-item"}])
            self.assertEqual(backup["credentials"], {})
            self.assertNotIn("bot_token", backup["telegram_settings"])
            self.assertNotIn("TAKSKLAD_BACKEND_API_TOKEN", runtime)
            self.assertEqual(runtime["TAKSKLAD_BACKEND_BASE_URL"], "https://synthetic.invalid")
            self.assertNotIn(sentinel.encode("utf-8"), serialized_surfaces)
            self.assertFalse(Path(storage.CREDENTIALS_FILE).exists())
            self.assertFalse(Path(storage.TELEGRAM_SETTINGS_FILE).exists())
            self.assertFalse(Path(storage.YANDEX_GEOCODER_KEY_FILE).exists())
            self.assertIn(sentinel, self.secret_store.get_text(GOOGLE_CREDENTIALS_SECRET))
            self.assertEqual(self.secret_store.get_text(TELEGRAM_BOT_TOKEN_SECRET), sentinel + "-telegram")
            self.assertEqual(self.secret_store.get_text(BACKEND_API_TOKEN_SECRET), sentinel + "-backend")
            self.assertEqual(self.secret_store.get_text(GEOCODER_API_KEY_SECRET), sentinel + "-geocoder")

    def test_secret_migration_failure_preserves_all_plaintext_sources_byte_for_byte(self):
        class FailingStore(MemorySecretStore):
            def set_text(self, name, value):
                raise RuntimeError("synthetic encrypted write failure")

        sentinel = synthetic_sentinel()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            self.configure_secret_paths(tmp_path)
            originals = {
                Path(storage.TAKSKLAD_DATA_FILE): json.dumps({
                    "credentials": credentials("synthetic@example.test", sentinel + "-google"),
                    "telegram_settings": {"bot_token": sentinel + "-telegram"},
                }).encode("utf-8"),
                Path(storage.app_data_backup_path(1)): json.dumps({
                    "credentials": credentials("synthetic@example.test", sentinel + "-backup")
                }).encode("utf-8"),
                Path(storage.RUNTIME_CONFIG_FILE): json.dumps({
                    "TAKSKLAD_BACKEND_API_TOKEN": sentinel + "-backend"
                }).encode("utf-8"),
                Path(storage.YANDEX_GEOCODER_KEY_FILE): (sentinel + "-geocoder").encode("utf-8"),
            }
            for path, content in originals.items():
                path.write_bytes(content)

            with self.assertRaises(storage.SecretMigrationError):
                storage.migrate_desktop_secrets(
                    FailingStore(),
                    allow_volatile_test_store=True,
                )

            self.assertEqual(storage.get_secret_migration_status()["status"], "migration_failed")
            for path, content in originals.items():
                self.assertEqual(path.read_bytes(), content)

    def test_generic_save_and_backup_never_serialize_secret_fields(self):
        sentinel = synthetic_sentinel()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            self.configure_secret_paths(tmp_path)
            Path(storage.TAKSKLAD_DATA_FILE).write_text(json.dumps({
                "credentials": credentials("synthetic@example.test", sentinel + "-old"),
                "telegram_settings": {"enabled": True, "bot_token": sentinel + "-old-token"},
            }), encoding="utf-8")

            self.assertTrue(storage.save_app_data({
                "credentials": credentials("synthetic@example.test", sentinel + "-new"),
                "telegram_settings": {"enabled": True, "bot_token": sentinel + "-new-token"},
            }))

            for path in (Path(storage.TAKSKLAD_DATA_FILE), Path(storage.app_data_backup_path(1))):
                content = path.read_bytes()
                self.assertNotIn(sentinel.encode("utf-8"), content)
                parsed = json.loads(content.decode("utf-8"))
                self.assertEqual(parsed["credentials"], {})
                self.assertNotIn("bot_token", parsed["telegram_settings"])

    def test_non_windows_provider_requires_explicit_development_selection(self):
        reset_secret_store_for_tests()
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SecretStoreUnavailable):
                get_secret_store()

    def test_explicit_non_windows_environment_provider_is_read_only(self):
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

    def test_frozen_runtime_rejects_environment_and_memory_providers(self):
        reset_secret_store_for_tests()
        for provider in ("environment", "memory"):
            with self.subTest(provider=provider), mock.patch.dict(os.environ, {
                "TAKSKLAD_SECRET_STORE_PROVIDER": provider,
                "TAKSKLAD_SECRET_STORE_MODE": "test",
            }, clear=True), mock.patch.object(sys, "frozen", True, create=True):
                with self.assertRaises(SecretStoreUnavailable):
                    get_secret_store()
            reset_secret_store_for_tests()

    def test_backup_only_credentials_are_migrated_before_backup_is_scrubbed(self):
        sentinel = synthetic_sentinel()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            self.configure_secret_paths(tmp_path)
            Path(storage.TAKSKLAD_DATA_FILE).write_text(
                json.dumps({"credentials": {}, "telegram_settings": {"enabled": True}}),
                encoding="utf-8",
            )
            Path(storage.app_data_backup_path(1)).write_text(
                json.dumps({
                    "credentials": credentials("backup@example.test", sentinel + "-google"),
                    "telegram_settings": {"bot_token": sentinel + "-telegram"},
                }),
                encoding="utf-8",
            )

            status = storage.migrate_desktop_secrets(
                self.secret_store,
                allow_volatile_test_store=True,
            )

            self.assertEqual(status["migrated"], 2)
            self.assertIn(sentinel, self.secret_store.get_text(GOOGLE_CREDENTIALS_SECRET))
            self.assertEqual(
                self.secret_store.get_text(TELEGRAM_BOT_TOKEN_SECRET),
                sentinel + "-telegram",
            )
            self.assertNotIn(
                sentinel.encode("utf-8"),
                Path(storage.app_data_backup_path(1)).read_bytes(),
            )

    def test_backup_only_migration_does_not_mask_last_good_recovery(self):
        sentinel = synthetic_sentinel()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            self.configure_secret_paths(tmp_path)
            Path(storage.app_data_backup_path(1)).write_text(
                json.dumps({
                    "credentials": credentials("backup@example.test", sentinel + "-google"),
                    "print_settings": {"copies": 3},
                    "telegram_settings": {"enabled": True},
                }),
                encoding="utf-8",
            )

            storage.migrate_desktop_secrets(
                self.secret_store,
                allow_volatile_test_store=True,
            )

            self.assertFalse(Path(storage.TAKSKLAD_DATA_FILE).exists())
            recovered = storage.load_app_data()
            self.assertEqual(recovered["print_settings"], {"copies": 3})
            self.assertTrue(recovered["telegram_settings"]["enabled"])
            self.assertEqual(recovered["credentials"], {})

    def test_unavailable_windows_store_fails_closed_even_without_legacy_candidates(self):
        class UnavailableWindowsStore(MemorySecretStore):
            def status(self):
                return {
                    "provider": "windows_dpapi",
                    "available": False,
                    "persistent": True,
                    "state": "failed_closed",
                }

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            self.configure_secret_paths(tmp_path)
            original = b'{"credentials":{},"telegram_settings":{"enabled":true}}'
            Path(storage.TAKSKLAD_DATA_FILE).write_bytes(original)

            with self.assertRaises(storage.SecretMigrationError):
                storage.migrate_desktop_secrets(UnavailableWindowsStore())

            self.assertEqual(Path(storage.TAKSKLAD_DATA_FILE).read_bytes(), original)
            self.assertEqual(storage.get_secret_migration_status()["status"], "migration_failed")

    def test_invalid_nonempty_credentials_fail_closed_and_preserve_every_source(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            self.configure_secret_paths(tmp_path)
            originals = {
                Path(storage.TAKSKLAD_DATA_FILE): b'{"credentials":{"client_email":"missing-key@example.test"}}',
                Path(storage.app_data_backup_path(1)): b'{"credentials":{"private_key":"synthetic-backup-key"}}',
                Path(storage.CREDENTIALS_FILE): b'{"client_email":"missing-key-file@example.test"}',
            }
            for path, content in originals.items():
                path.write_bytes(content)

            with self.assertRaises(storage.SecretMigrationError):
                storage.migrate_desktop_secrets(
                    self.secret_store,
                    allow_volatile_test_store=True,
                )

            self.assertEqual(storage.get_secret_migration_status()["status"], "migration_failed")
            for path, content in originals.items():
                self.assertEqual(path.read_bytes(), content)

    def test_fault_after_plaintext_purge_restores_sources_and_secure_values(self):
        sentinel = synthetic_sentinel()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            self.configure_secret_paths(tmp_path)
            originals = {
                Path(storage.TAKSKLAD_DATA_FILE): json.dumps({
                    "credentials": credentials("synthetic@example.test", sentinel + "-google"),
                    "telegram_settings": {"bot_token": sentinel + "-telegram"},
                }).encode("utf-8"),
                Path(storage.CREDENTIALS_FILE): json.dumps(
                    credentials("file@example.test", sentinel + "-file")
                ).encode("utf-8"),
            }
            for path, content in originals.items():
                path.write_bytes(content)
            self.secret_store.set_text(TELEGRAM_BOT_TOKEN_SECRET, "previous-token")

            def fail_after_purge(stage):
                if stage == "after_plaintext_purge":
                    raise RuntimeError("synthetic post-purge failure")

            with mock.patch.object(storage, "_storage_fault_hook", side_effect=fail_after_purge):
                with self.assertRaises(storage.SecretMigrationError):
                    storage.migrate_desktop_secrets(
                        self.secret_store,
                        allow_volatile_test_store=True,
                    )

            for path, content in originals.items():
                self.assertEqual(path.read_bytes(), content)
            self.assertIsNone(self.secret_store.get_text(GOOGLE_CREDENTIALS_SECRET))
            self.assertEqual(self.secret_store.get_text(TELEGRAM_BOT_TOKEN_SECRET), "previous-token")


if __name__ == "__main__":
    unittest.main()
