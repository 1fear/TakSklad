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
from backend.app.settings import (
    ConfigurationError,
    load_settings as load_backend_settings,
    validate_backend_settings,
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

    def test_empty_backend_mapping_does_not_inherit_process_environment(self):
        with mock.patch.dict(os.environ, {"TAKSKLAD_API_TOKEN": "inherited-token"}, clear=True):
            settings = load_backend_settings({})

        self.assertEqual(settings.api_token, "")
        self.assertFalse(settings.environment_explicit)

    def test_production_requires_auth_and_independent_session_secret(self):
        with self.assertRaises(ConfigurationError) as missing_auth:
            validate_backend_settings(load_backend_settings({"TAKSKLAD_ENV": "production"}))
        self.assertIn("TAKSKLAD_AUTH_MECHANISM", missing_auth.exception.setting_names)
        self.assertIn("TAKSKLAD_WEB_SESSION_SECRET", missing_auth.exception.setting_names)

        with self.assertRaises(ConfigurationError) as shared_secret:
            validate_backend_settings(load_backend_settings({
                "TAKSKLAD_ENV": "production",
                "TAKSKLAD_API_TOKEN": "synthetic-api-token",
                "TAKSKLAD_WEB_SESSION_SECRET": "synthetic-api-token",
                "TAKSKLAD_LEGACY_AUTH_EXPIRES_AT": "2026-07-17T00:00:00+00:00",
            }))
        self.assertEqual(shared_secret.exception.setting_names, ("TAKSKLAD_WEB_SESSION_SECRET",))

        for weak_secret in ("x", "x" * 64):
            with self.subTest(weak_secret_length=len(weak_secret)):
                with self.assertRaises(ConfigurationError) as weak:
                    validate_backend_settings(load_backend_settings({
                        "TAKSKLAD_ENV": "production",
                        "TAKSKLAD_API_TOKEN": "synthetic-api-token",
                        "TAKSKLAD_WEB_SESSION_SECRET": weak_secret,
                        "TAKSKLAD_LEGACY_AUTH_EXPIRES_AT": "2026-07-17T00:00:00+00:00",
                    }))
                self.assertEqual(weak.exception.setting_names, ("TAKSKLAD_WEB_SESSION_SECRET",))
                self.assertNotIn(weak_secret, str(weak.exception))

        settings = validate_backend_settings(load_backend_settings({
            "TAKSKLAD_ENV": "production",
            "TAKSKLAD_API_TOKEN": "synthetic-api-token",
            "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
            "TAKSKLAD_LEGACY_AUTH_EXPIRES_AT": "2026-07-17T00:00:00+00:00",
        }))
        self.assertTrue(settings.api_auth_enabled)

    def test_anonymous_local_admin_requires_explicit_environment_and_opt_in(self):
        for environment in (None, "local"):
            with self.subTest(environment=environment):
                values = {}
                if environment is not None:
                    values["TAKSKLAD_ENV"] = environment
                with self.assertRaises(ConfigurationError):
                    validate_backend_settings(load_backend_settings(values))

        settings = validate_backend_settings(load_backend_settings({
            "TAKSKLAD_ENV": "local",
            "TAKSKLAD_INSECURE_LOCAL_ANONYMOUS": "true",
        }))
        self.assertTrue(settings.anonymous_local_admin_enabled)

    def test_identity_auth_and_legacy_window_validation_are_fail_closed(self):
        identity_settings = validate_backend_settings(load_backend_settings({
            "TAKSKLAD_ENV": "production",
            "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
            "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
        }))
        self.assertTrue(identity_settings.identity_auth_enabled)

        with self.assertRaises(ConfigurationError) as missing_expiry:
            validate_backend_settings(load_backend_settings({
                "TAKSKLAD_ENV": "production",
                "TAKSKLAD_API_TOKEN": "synthetic-api-token",
                "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
            }))
        self.assertEqual(
            missing_expiry.exception.setting_names,
            ("TAKSKLAD_LEGACY_AUTH_EXPIRES_AT",),
        )

        with self.assertRaises(ConfigurationError) as invalid_policy:
            validate_backend_settings(load_backend_settings({
                "TAKSKLAD_ENV": "production",
                "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
                "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
                "TAKSKLAD_LEGACY_AUTH_MODE": "forever",
                "TAKSKLAD_SERVICE_TOKEN_ROTATION_MAX_OVERLAP_SECONDS": "3601",
            }))
        self.assertEqual(
            invalid_policy.exception.setting_names,
            ("TAKSKLAD_LEGACY_AUTH_MODE", "TAKSKLAD_SERVICE_TOKEN_ROTATION_MAX_OVERLAP_SECONDS"),
        )

        for legacy_mode in ("shadow", "disabled"):
            with self.subTest(legacy_mode=legacy_mode):
                with self.assertRaises(ConfigurationError) as no_identity:
                    validate_backend_settings(load_backend_settings({
                        "TAKSKLAD_ENV": "production",
                        "TAKSKLAD_API_TOKEN": "synthetic-api-token",
                        "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
                        "TAKSKLAD_LEGACY_AUTH_MODE": legacy_mode,
                    }))
                self.assertIn("TAKSKLAD_AUTH_MECHANISM", no_identity.exception.setting_names)
                self.assertIn("TAKSKLAD_IDENTITY_AUTH_ENABLED", no_identity.exception.setting_names)

    def test_partial_web_auth_and_unknown_environment_fail_name_only(self):
        with self.assertRaises(ConfigurationError) as captured:
            validate_backend_settings(load_backend_settings({
                "TAKSKLAD_ENV": "prodution",
                "TAKSKLAD_WEB_LOGIN": "synthetic-login",
                "TAKSKLAD_WEB_SESSION_SECRET": "synthetic-session-secret",
            }))

        self.assertEqual(
            captured.exception.setting_names,
            (
                "TAKSKLAD_AUTH_MECHANISM",
                "TAKSKLAD_ENV",
                "TAKSKLAD_WEB_LOGIN",
                "TAKSKLAD_WEB_PASSWORD_HASH",
                "TAKSKLAD_WEB_SESSION_SECRET",
            ),
        )
        self.assertNotIn("synthetic-login", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
