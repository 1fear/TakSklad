import importlib
import unittest
from pathlib import Path

from taksklad.main import ScanningApp


REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = REPO_ROOT / "src" / "taksklad" / "main.py"

EXTRACTED_MODULES = [
    "taksklad.app_data_loading",
    "taksklad.app_finish",
    "taksklad.app_layout",
    "taksklad.app_order_display",
    "taksklad.app_returns",
    "taksklad.app_runtime",
    "taksklad.app_scanning",
    "taksklad.backend_flow",
    "taksklad.desktop_refresh_service",
    "taksklad.desktop_scan_rules",
    "taksklad.order_list_models",
    "taksklad.order_list_widgets",
]

FORBIDDEN_MAIN_METHODS = [
    "_build_ui",
    "apply_backend_blocked_scan_events",
    "finish_legal_entity",
    "load_current_product",
    "next_product",
    "on_scan",
    "refresh_from_sheet",
    "refresh_legal_list",
    "reset_current_selection",
    "select_legal_entity",
    "show_error",
    "show_error_toast",
    "show_returns_window",
    "undo_last_scan",
    "update_product_photo",
    "validate_code",
]


class CodeOrganizationTests(unittest.TestCase):
    def test_main_py_stays_small(self):
        line_count = len(MAIN_PATH.read_text(encoding="utf-8").splitlines())

        self.assertLessEqual(line_count, 500)

    def test_scanning_app_does_not_own_extracted_workflows(self):
        direct_methods = set(ScanningApp.__dict__)

        for method_name in FORBIDDEN_MAIN_METHODS:
            self.assertNotIn(method_name, direct_methods)

    def test_extracted_modules_import_without_runtime_services(self):
        for module_name in EXTRACTED_MODULES:
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                self.assertIsNotNone(module)


if __name__ == "__main__":
    unittest.main()
