import unittest
from unittest import mock

from taksklad import startup_check, storage
from taksklad.secret_store import (
    BACKEND_API_TOKEN_SECRET,
    MemorySecretStore,
    reset_secret_store_for_tests,
    set_secret_store_for_tests,
)


class StartupCheckTests(unittest.TestCase):
    def tearDown(self):
        reset_secret_store_for_tests()

    def test_startup_self_check_reports_backend_only_contract(self):
        store = MemorySecretStore({BACKEND_API_TOKEN_SECRET: "synthetic-token"})
        set_secret_store_for_tests(store)
        with (
            mock.patch.object(storage, "load_app_data", return_value={"pending_backend_events": [], "pending_prints": [], "pending_telegram": []}),
            mock.patch.object(storage, "get_app_data_recovery_status", return_value={"status": "ok", "restored_from": ""}),
            mock.patch.object(startup_check, "load_telegram_settings", return_value={"enabled": False}),
        ):
            check = startup_check.build_startup_self_check()

        self.assertEqual(check["backend_enabled"], "yes")
        self.assertEqual(check["backend_read_orders"], "yes")
        self.assertEqual(check["backend_only_refresh"], "yes")
        self.assertEqual(check["telegram_desktop_polling"], "no")
        self.assertNotIn("spreadsheet_hash", check)
        self.assertNotIn("credentials", check)

    def test_version_update_status_blocks_below_min_supported(self):
        result = startup_check.build_version_update_status({
            "latest_version": "2.0.40",
            "min_supported_version": "2.0.39",
            "mandatory": True,
            "block_workflow": True,
        }, current_version="2.0.38")
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["blocking"], "yes")

    def test_version_update_status_current_is_non_blocking(self):
        result = startup_check.build_version_update_status({
            "latest_version": "2.0.38",
            "min_supported_version": "2.0.38",
            "block_workflow": True,
        }, current_version="2.0.38")
        self.assertEqual(result["state"], "current")
        self.assertEqual(result["blocking"], "no")

    def test_version_label_contains_current_version(self):
        self.assertIn(startup_check.APP_VERSION, startup_check.format_app_version_label())


if __name__ == "__main__":
    unittest.main()
