import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


CONFIG_PATH = Path(__file__).resolve().parents[1] / "src" / "taksklad" / "config.py"


def load_config_from_app_dir(app_dir, env=None, executable_name="TakSklad.exe", platform_name=None):
    module_name = "taksklad_config_runtime_test"
    spec = importlib.util.spec_from_file_location(module_name, CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    old_executable = sys.executable
    old_platform = sys.platform
    had_frozen = hasattr(sys, "frozen")
    old_frozen = getattr(sys, "frozen", None)
    try:
        sys.frozen = True
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
    def test_direct_exe_enables_backend_from_runtime_json(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            Path(tmp_dir, ".env.taksklad-vds-2.0.generated.json").write_text(
                json.dumps({"TAKSKLAD_API_TOKEN": "service-token"}),
                encoding="utf-8",
            )

            config = load_config_from_app_dir(tmp_dir)

        self.assertTrue(config.TAKSKLAD_BACKEND_ENABLED)
        self.assertTrue(config.TAKSKLAD_BACKEND_READ_ORDERS_ENABLED)
        self.assertEqual(config.TAKSKLAD_BACKEND_BASE_URL, "https://api.taksklad.uz")
        self.assertEqual(config.TAKSKLAD_BACKEND_API_TOKEN, "service-token")
        self.assertEqual(config.TAKSKLAD_BACKEND_TIMEOUT_SECONDS, 8)

    def test_environment_overrides_runtime_json(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            Path(tmp_dir, ".env.taksklad-vds-2.0.generated.json").write_text(
                json.dumps({"TAKSKLAD_API_TOKEN": "json-token"}),
                encoding="utf-8",
            )

            config = load_config_from_app_dir(
                tmp_dir,
                env={
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

    def test_without_runtime_json_backend_stays_disabled(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = load_config_from_app_dir(tmp_dir)

        self.assertFalse(config.TAKSKLAD_BACKEND_ENABLED)
        self.assertFalse(config.TAKSKLAD_BACKEND_READ_ORDERS_ENABLED)
        self.assertEqual(config.TAKSKLAD_BACKEND_BASE_URL, "")
        self.assertEqual(config.TAKSKLAD_BACKEND_API_TOKEN, "")

    def test_macos_app_uses_parent_folder_as_app_dir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            Path(tmp_dir, ".env.taksklad-vds-2.0.generated.json").write_text(
                json.dumps({"TAKSKLAD_API_TOKEN": "service-token"}),
                encoding="utf-8",
            )

            config = load_config_from_app_dir(
                tmp_dir,
                executable_name="TakSklad.app/Contents/MacOS/TakSklad",
                platform_name="darwin",
            )

        self.assertEqual(config.APP_DIR, tmp_dir)
        self.assertEqual(config.LOG_DIR, str(Path(tmp_dir) / "docs"))
        self.assertTrue(config.TAKSKLAD_BACKEND_ENABLED)


if __name__ == "__main__":
    unittest.main()
