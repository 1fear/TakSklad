import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from taksklad import backend_client
from taksklad.secret_store import (
    BACKEND_API_TOKEN_SECRET,
    MemorySecretStore,
    SecretStoreUnavailable,
    SecretStoreError,
    reset_secret_store_for_tests,
    set_secret_store_for_tests,
)


CONFIG_PATH = Path(__file__).resolve().parents[1] / "src" / "taksklad" / "config.py"


def load_config_from_app_dir(
    app_dir,
    env=None,
    executable_name="TakSklad.exe",
    platform_name=None,
    frozen=True,
):
    module_name = "taksklad_config_runtime_test"
    spec = importlib.util.spec_from_file_location(module_name, CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    old_executable = sys.executable
    old_platform = sys.platform
    had_frozen = hasattr(sys, "frozen")
    old_frozen = getattr(sys, "frozen", None)
    try:
        sys.frozen = frozen
        sys.executable = str(Path(app_dir) / executable_name)
        if platform_name:
            sys.platform = platform_name
        with mock.patch.dict(os.environ, env or {}, clear=True):
            spec.loader.exec_module(module)
        return module
    finally:
        sys.executable = old_executable
        sys.platform = old_platform
        if had_frozen:
            sys.frozen = old_frozen
        else:
            delattr(sys, "frozen")


class BackendRuntimeConfigTests(unittest.TestCase):
    def tearDown(self):
        reset_secret_store_for_tests()

    def test_runtime_json_token_is_ignored_when_secure_store_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            Path(tmp_dir, ".env.taksklad-vds-2.0.generated.json").write_text(
                json.dumps({"TAKSKLAD_API_TOKEN": "service-token"}),
                encoding="utf-8",
            )

            set_secret_store_for_tests(MemorySecretStore())
            config = load_config_from_app_dir(tmp_dir)

        self.assertFalse(config.TAKSKLAD_BACKEND_ENABLED)
        self.assertFalse(config.TAKSKLAD_BACKEND_READ_ORDERS_ENABLED)
        self.assertEqual(config.TAKSKLAD_BACKEND_BASE_URL, "")
        self.assertEqual(config.TAKSKLAD_BACKEND_API_TOKEN, "")
        self.assertEqual(config.TAKSKLAD_BACKEND_TIMEOUT_SECONDS, 8)

    def test_environment_overrides_runtime_json(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            Path(tmp_dir, ".env.taksklad-vds-2.0.generated.json").write_text(
                json.dumps({"TAKSKLAD_API_TOKEN": "json-token"}),
                encoding="utf-8",
            )

            config = load_config_from_app_dir(
                tmp_dir,
                frozen=False,
                env={
                    "TAKSKLAD_SECRET_STORE_PROVIDER": "environment",
                    "TAKSKLAD_SECRET_STORE_MODE": "test",
                    "TAKSKLAD_BACKEND_ENABLED": "0",
                    "TAKSKLAD_BACKEND_API_TOKEN": "env-token",
                    "TAKSKLAD_BACKEND_BASE_URL": "https://example.test/api/",
                    "TAKSKLAD_BACKEND_TIMEOUT_SECONDS": "12",
                },
            )

        self.assertFalse(config.TAKSKLAD_BACKEND_ENABLED)
        self.assertEqual(config.TAKSKLAD_BACKEND_API_TOKEN, "env-token")
        self.assertEqual(config.TAKSKLAD_BACKEND_BASE_URL, "https://example.test/api")
        self.assertEqual(config.TAKSKLAD_BACKEND_TIMEOUT_SECONDS, 12)

    def test_frozen_runtime_rejects_explicit_environment_provider(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(SecretStoreUnavailable):
                load_config_from_app_dir(
                    tmp_dir,
                    env={
                        "TAKSKLAD_SECRET_STORE_PROVIDER": "environment",
                        "TAKSKLAD_SECRET_STORE_MODE": "test",
                        "TAKSKLAD_BACKEND_API_TOKEN": "synthetic-token",
                    },
                )

    def test_without_runtime_json_backend_stays_disabled(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            set_secret_store_for_tests(MemorySecretStore())
            config = load_config_from_app_dir(tmp_dir)

        self.assertFalse(config.TAKSKLAD_BACKEND_ENABLED)
        self.assertFalse(config.TAKSKLAD_BACKEND_READ_ORDERS_ENABLED)
        self.assertEqual(config.TAKSKLAD_BACKEND_BASE_URL, "")
        self.assertEqual(config.TAKSKLAD_BACKEND_API_TOKEN, "")

    def test_frozen_macos_without_explicit_provider_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            Path(tmp_dir, ".env.taksklad-vds-2.0.generated.json").write_text(
                json.dumps({"TAKSKLAD_API_TOKEN": "service-token"}),
                encoding="utf-8",
            )

            with self.assertRaises(SecretStoreUnavailable):
                load_config_from_app_dir(
                    tmp_dir,
                    executable_name="TakSklad.app/Contents/MacOS/TakSklad",
                    platform_name="darwin",
                )

    def test_macos_app_with_explicit_test_provider_uses_parent_folder(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            set_secret_store_for_tests(MemorySecretStore({BACKEND_API_TOKEN_SECRET: "synthetic-token"}))
            config = load_config_from_app_dir(
                tmp_dir,
                executable_name="TakSklad.app/Contents/MacOS/TakSklad",
                platform_name="darwin",
            )

        self.assertEqual(config.APP_DIR, tmp_dir)
        self.assertEqual(config.LOG_DIR, str(Path(tmp_dir) / "docs"))
        self.assertTrue(config.TAKSKLAD_BACKEND_ENABLED)

    def test_backend_headers_fail_closed_after_secure_store_error(self):
        class FailingStore(MemorySecretStore):
            def get_text(self, name):
                raise SecretStoreError("synthetic access denial")

        set_secret_store_for_tests(FailingStore())

        headers = backend_client.make_backend_headers()

        self.assertNotIn("Authorization", headers)


if __name__ == "__main__":
    unittest.main()
