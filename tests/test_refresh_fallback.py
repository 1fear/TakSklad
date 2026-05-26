import unittest

import main


class RefreshFallbackTests(unittest.TestCase):
    def test_initial_load_does_not_require_skladbot_number(self):
        original_get_today_orders = main.get_today_orders
        original_get_all_existing_codes = main.get_all_existing_codes
        original_get_pending_codes = main.get_pending_codes
        try:
            calls = []
            sheet = object()
            google_orders = [{"Клиент": "Test Client"}]

            def fake_get_today_orders(apply_skladbot_filter=None):
                calls.append(apply_skladbot_filter)
                return google_orders, sheet

            main.get_today_orders = fake_get_today_orders
            main.get_all_existing_codes = lambda sheet: set()
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

            def fake_get_today_orders(apply_skladbot_filter=None):
                calls.append(apply_skladbot_filter)
                if apply_skladbot_filter is False:
                    return fallback_orders, sheet
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
            main.get_all_existing_codes = lambda sheet: set()
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

            def fake_get_today_orders(apply_skladbot_filter=None):
                calls.append(apply_skladbot_filter)
                if apply_skladbot_filter is True:
                    return [google_orders[0]], sheet
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
            main.get_all_existing_codes = lambda sheet: set()
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

            def fake_get_today_orders(apply_skladbot_filter=None):
                calls.append(apply_skladbot_filter)
                return google_orders, sheet

            main.get_today_orders = fake_get_today_orders
            main.sync_pending_saves = lambda sheet=None: {"synced": 0, "failed": 0, "remaining": 0}
            main.sync_skladbot_request_numbers = fail_skladbot_sync
            main.get_all_existing_codes = lambda sheet: set()
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


if __name__ == "__main__":
    unittest.main()
