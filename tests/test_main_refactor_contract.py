import inspect
import unittest

from taksklad import main as main_module
from taksklad.main import ScanningApp


PUBLIC_MAIN_HELPERS = [
    "backend_blocked_scan_events_for_item",
    "backend_sync_group_blocker",
    "backend_sync_item_blocker",
    "build_product_result",
    "complete_backend_orders_or_raise",
    "find_code_owner_in_orders",
    "format_backend_blocked_scan_message",
    "format_duplicate_scan_message",
    "format_print_failure_after_backend_complete",
    "format_scan_product_mismatch_message",
    "group_finish_blocker",
    "is_terminal_scan_state",
]


class MainRefactorContractTests(unittest.TestCase):
    def test_public_main_helper_imports_stay_compatible(self):
        for name in PUBLIC_MAIN_HELPERS:
            self.assertTrue(callable(getattr(main_module, name)))

    def test_scan_rejects_wrong_sku_before_backup_and_backend_queue(self):
        source = inspect.getsource(ScanningApp.on_scan)

        self.assertIn("scan_product_mismatch", source)
        self.assertLess(source.index("scan_product_mismatch"), source.index("write_scan_backup"))
        self.assertLess(source.index("scan_product_mismatch"), source.index("queue_backend_scan"))

    def test_finish_prints_before_backend_complete(self):
        source = inspect.getsource(ScanningApp.finish_legal_entity)
        print_call = "print_summary(address, summary_products, print_settings=selected_print_settings)"

        self.assertIn("add_pending_print", source)
        self.assertIn(print_call, source)
        self.assertIn("remove_pending_print", source)
        self.assertLess(source.index("add_pending_print"), source.index(print_call))
        self.assertLess(source.index(print_call), source.index("remove_pending_print"))
        self.assertLess(source.index(print_call), source.index("complete_backend_orders_or_raise"))
        self.assertNotIn("archive_order_group_to_gsheet", source)

    def test_return_flow_is_backend_only(self):
        source = inspect.getsource(ScanningApp.mark_return_for_display)

        self.assertIn("backend_configured()", source)
        self.assertIn("mark_order_returned", source)
        self.assertNotIn("mark_return_order_in_gsheet", source)

    def test_product_photo_contract_stays_on_scan_screen(self):
        build_source = inspect.getsource(ScanningApp._build_ui)
        load_source = inspect.getsource(ScanningApp.load_current_product)
        reset_source = inspect.getsource(ScanningApp.reset_current_selection)

        self.assertIn("self.product_photo_canvas", build_source)
        self.assertIn("self.product_photo_gtin_label", build_source)
        self.assertIn("self.product_photo_caption_label", build_source)
        self.assertIn("self.update_product_photo(product_text)", load_source)
        self.assertIn("self.update_product_photo(\"\")", reset_source)

    def test_errors_use_non_blocking_toast_contract(self):
        show_error_source = inspect.getsource(ScanningApp.show_error)
        toast_source = inspect.getsource(ScanningApp.show_error_toast)
        status_source = inspect.getsource(ScanningApp.show_status_notice)

        self.assertIn("show_error_toast", show_error_source)
        self.assertNotIn("messagebox.showerror", show_error_source)
        self.assertIn("error_toast", toast_source)
        self.assertIn("STATUS_NOTICE_TIMEOUT_MS", status_source)


if __name__ == "__main__":
    unittest.main()
