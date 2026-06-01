import unittest

from taksklad import main


class RefreshFallbackTests(unittest.TestCase):
    def test_on_close_has_telegram_lock_helper_imported(self):
        self.assertTrue(callable(main.ScanningApp.on_close.__globals__["telegram_single_listener_lock_enabled"]))

    def test_initial_load_does_not_require_skladbot_number(self):
        original_get_today_orders = main.get_today_orders
        original_get_all_existing_codes = main.get_all_existing_codes
        original_get_pending_codes = main.get_pending_codes
        try:
            calls = []
            sheet = object()
            google_orders = [{"Клиент": "Test Client"}]

            def fake_get_today_orders(apply_skladbot_filter=None, include_rows=False):
                calls.append(apply_skladbot_filter)
                if include_rows:
                    return google_orders, sheet, []
                return google_orders, sheet

            main.get_today_orders = fake_get_today_orders
            main.get_all_existing_codes = lambda sheet, all_rows=None: set()
            main.get_pending_codes = lambda: set()

            orders, _, _ = main.fetch_sheet_data()

            self.assertEqual(orders, google_orders)
            self.assertEqual(calls, [False])
        finally:
            main.get_today_orders = original_get_today_orders
            main.get_all_existing_codes = original_get_all_existing_codes
            main.get_pending_codes = original_get_pending_codes

    def test_returns_google_orders_when_skladbot_sync_fails(self):
        original_get_today_orders = main.get_today_orders
        original_sync_pending_saves = main.sync_pending_saves
        original_sync_skladbot_request_numbers = main.sync_skladbot_request_numbers
        original_get_all_existing_codes = main.get_all_existing_codes
        original_get_pending_codes = main.get_pending_codes
        try:
            calls = []
            sheet = object()
            fallback_orders = [{"Клиент": "Test Client"}]

            def fake_get_today_orders(apply_skladbot_filter=None, include_rows=False):
                calls.append(apply_skladbot_filter)
                if apply_skladbot_filter is False:
                    if include_rows:
                        return fallback_orders, sheet, []
                    return fallback_orders, sheet
                if include_rows:
                    return [], sheet, []
                return [], sheet

            main.get_today_orders = fake_get_today_orders
            main.sync_pending_saves = lambda sheet=None: {"synced": 0, "failed": 0, "remaining": 0}
            main.sync_skladbot_request_numbers = lambda sheet: {
                "enabled": True,
                "updated": 2,
                "matched": 0,
                "not_found": 0,
                "multiple": 0,
                "errors": 1,
                "message": "timeout",
            }
            main.get_all_existing_codes = lambda sheet, all_rows=None: set()
            main.get_pending_codes = lambda: set()

            orders, _, _, sync_result = main.fetch_sheet_data_with_sync()

            self.assertEqual(orders, fallback_orders)
            self.assertEqual(calls, [False])
            self.assertEqual(sync_result["skladbot"]["errors"], 1)
        finally:
            main.get_today_orders = original_get_today_orders
            main.sync_pending_saves = original_sync_pending_saves
            main.sync_skladbot_request_numbers = original_sync_skladbot_request_numbers
            main.get_all_existing_codes = original_get_all_existing_codes
            main.get_pending_codes = original_get_pending_codes

    def test_keeps_google_orders_without_skladbot_number_after_successful_sync(self):
        original_get_today_orders = main.get_today_orders
        original_sync_pending_saves = main.sync_pending_saves
        original_sync_skladbot_request_numbers = main.sync_skladbot_request_numbers
        original_get_all_existing_codes = main.get_all_existing_codes
        original_get_pending_codes = main.get_pending_codes
        try:
            calls = []
            sheet = object()
            google_orders = [
                {"Клиент": "Matched", "Номер заявки SkladBot": "WR-1"},
                {"Клиент": "Not matched", "Номер заявки SkladBot": ""},
            ]

            def fake_get_today_orders(apply_skladbot_filter=None, include_rows=False):
                calls.append(apply_skladbot_filter)
                if apply_skladbot_filter is True:
                    if include_rows:
                        return [google_orders[0]], sheet, []
                    return [google_orders[0]], sheet
                if include_rows:
                    return google_orders, sheet, []
                return google_orders, sheet

            main.get_today_orders = fake_get_today_orders
            main.sync_pending_saves = lambda sheet=None: {"synced": 0, "failed": 0, "remaining": 0}
            main.sync_skladbot_request_numbers = lambda sheet: {
                "enabled": True,
                "updated": 1,
                "matched": 1,
                "not_found": 1,
                "multiple": 0,
                "errors": 0,
                "message": "",
            }
            main.get_all_existing_codes = lambda sheet, all_rows=None: set()
            main.get_pending_codes = lambda: set()

            orders, _, _, sync_result = main.fetch_sheet_data_with_sync()

            self.assertEqual(orders, google_orders)
            self.assertEqual(calls, [False, False])
            self.assertEqual(sync_result["skladbot"]["not_found"], 1)
        finally:
            main.get_today_orders = original_get_today_orders
            main.sync_pending_saves = original_sync_pending_saves
            main.sync_skladbot_request_numbers = original_sync_skladbot_request_numbers
            main.get_all_existing_codes = original_get_all_existing_codes
            main.get_pending_codes = original_get_pending_codes

    def test_can_refresh_without_blocking_on_skladbot_sync(self):
        original_get_today_orders = main.get_today_orders
        original_sync_pending_saves = main.sync_pending_saves
        original_sync_skladbot_request_numbers = main.sync_skladbot_request_numbers
        original_get_all_existing_codes = main.get_all_existing_codes
        original_get_pending_codes = main.get_pending_codes
        try:
            calls = []
            sheet = object()
            google_orders = [{"Клиент": "Visible", "Номер заявки SkladBot": ""}]

            def fail_skladbot_sync(sheet):
                raise AssertionError("SkladBot sync should not run during fast refresh")

            def fake_get_today_orders(apply_skladbot_filter=None, include_rows=False):
                calls.append(apply_skladbot_filter)
                if include_rows:
                    return google_orders, sheet, []
                return google_orders, sheet

            main.get_today_orders = fake_get_today_orders
            main.sync_pending_saves = lambda sheet=None: {"synced": 0, "failed": 0, "remaining": 0}
            main.sync_skladbot_request_numbers = fail_skladbot_sync
            main.get_all_existing_codes = lambda sheet, all_rows=None: set()
            main.get_pending_codes = lambda: set()

            orders, _, _, sync_result = main.fetch_sheet_data_with_sync(sync_skladbot=False)

            self.assertEqual(orders, google_orders)
            self.assertEqual(calls, [False])
            self.assertEqual(sync_result["skladbot"]["enabled"], False)
        finally:
            main.get_today_orders = original_get_today_orders
            main.sync_pending_saves = original_sync_pending_saves
            main.sync_skladbot_request_numbers = original_sync_skladbot_request_numbers
            main.get_all_existing_codes = original_get_all_existing_codes
            main.get_pending_codes = original_get_pending_codes

    def test_backend_refresh_forces_google_and_skladbot_sync_before_loading_orders(self):
        original_backend_read_orders_enabled = main.backend_read_orders_enabled
        original_sync_pending_backend_events = main.sync_pending_backend_events
        original_sync_backend_sources = main.sync_backend_sources
        original_fetch_backend_sheet_data = main.fetch_backend_sheet_data
        original_get_today_orders = main.get_today_orders
        original_get_all_existing_codes = main.get_all_existing_codes
        original_get_pending_codes = main.get_pending_codes
        original_get_pending_backend_codes = main.get_pending_backend_codes
        try:
            calls = []
            sheet = object()
            google_orders = [{"Клиент": "Google Client", "Кол-во блок": 2}]
            backend_orders = [{"Клиент": "Backend Client", "Кол-во блок": 1}]

            def fake_sync_backend_sources(sync_skladbot=True, wait_skladbot=True):
                calls.append((sync_skladbot, wait_skladbot))
                return {
                    "status": "completed",
                    "google_sheets": {
                        "status": "completed",
                        "orders_updated": 0,
                        "items_updated": 1,
                    },
                    "skladbot": {
                        "status": "completed",
                        "updated": 1,
                        "matched": 1,
                        "not_found": 0,
                        "multiple": 0,
                    },
                }

            main.backend_read_orders_enabled = lambda: True
            main.sync_pending_backend_events = lambda: {"enabled": True, "remaining": 0}
            main.sync_backend_sources = fake_sync_backend_sources
            main.fetch_backend_sheet_data = lambda: (backend_orders, None, set())
            main.get_today_orders = lambda apply_skladbot_filter=None, include_rows=False: (
                (google_orders, sheet, []) if include_rows else (google_orders, sheet)
            )
            main.get_all_existing_codes = lambda sheet, all_rows=None: set()
            main.get_pending_codes = lambda: set()
            main.get_pending_backend_codes = lambda: set()

            orders, _, _, sync_result = main.fetch_sheet_data_with_sync(sync_skladbot=True)

            self.assertEqual(orders, google_orders)
            self.assertEqual(calls, [(True, True)])
            self.assertEqual(sync_result["google_sheets"]["items_updated"], 1)
            self.assertEqual(sync_result["skladbot"]["matched"], 1)
            self.assertEqual(sync_result["primary_source"], "google_sheets")
        finally:
            main.backend_read_orders_enabled = original_backend_read_orders_enabled
            main.sync_pending_backend_events = original_sync_pending_backend_events
            main.sync_backend_sources = original_sync_backend_sources
            main.fetch_backend_sheet_data = original_fetch_backend_sheet_data
            main.get_today_orders = original_get_today_orders
            main.get_all_existing_codes = original_get_all_existing_codes
            main.get_pending_codes = original_get_pending_codes
            main.get_pending_backend_codes = original_get_pending_backend_codes

    def test_backend_refresh_falls_back_to_backend_when_google_primary_fails(self):
        original_backend_read_orders_enabled = main.backend_read_orders_enabled
        original_sync_pending_backend_events = main.sync_pending_backend_events
        original_sync_backend_sources = main.sync_backend_sources
        original_fetch_backend_sheet_data = main.fetch_backend_sheet_data
        original_get_today_orders = main.get_today_orders
        original_get_pending_backend_codes = main.get_pending_backend_codes
        try:
            backend_orders = [{"Клиент": "Backend Client", "Кол-во блок": 1}]

            main.backend_read_orders_enabled = lambda: True
            main.sync_pending_backend_events = lambda: {"enabled": True, "remaining": 0}
            main.sync_backend_sources = lambda sync_skladbot=True, wait_skladbot=True: {
                "status": "completed",
                "google_sheets": {"status": "completed"},
                "skladbot": {"status": "skipped"},
            }
            main.get_today_orders = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Google down"))
            main.fetch_backend_sheet_data = lambda: (backend_orders, None, set())
            main.get_pending_backend_codes = lambda: set()

            orders, _, _, sync_result = main.fetch_sheet_data_with_sync(sync_skladbot=False)

            self.assertEqual(orders, backend_orders)
            self.assertEqual(sync_result["primary_source"], "backend_fallback")
        finally:
            main.backend_read_orders_enabled = original_backend_read_orders_enabled
            main.sync_pending_backend_events = original_sync_pending_backend_events
            main.sync_backend_sources = original_sync_backend_sources
            main.fetch_backend_sheet_data = original_fetch_backend_sheet_data
            main.get_today_orders = original_get_today_orders
            main.get_pending_backend_codes = original_get_pending_backend_codes

    def test_refresh_error_message_keeps_cached_orders(self):
        message = main.format_refresh_error_message(
            RuntimeError("Google Sheets временно ограничил запросы"),
            has_cached_orders=True,
        )

        self.assertIn("последним загруженным списком", message)
        self.assertIn("повторите обновление позже", message)


if __name__ == "__main__":
    unittest.main()
