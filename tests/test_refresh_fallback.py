import unittest

from taksklad import main
from taksklad import app_data_loading
from taksklad import desktop_refresh_service as refresh_service


class DummyStatusVar:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value


class DummyStatusLabel:
    def __init__(self):
        self.configs = []

    def config(self, **kwargs):
        self.configs.append(kwargs)


class FakeRefreshApp(app_data_loading.DataLoadingMixin):
    def __init__(self):
        self.current_order = {"ID заказа": "order-1", "Товары": "Chapman Brown OP 20"}
        self.today_orders = [self.current_order]
        self.sheet = None
        self.all_existing_codes = set()
        self.last_sync_result = {}
        self.operation_in_progress = False
        self.refresh_in_progress = False
        self.refresh_btn = object()
        self.import_btn = object()
        self.status_var = DummyStatusVar()
        self.status_label = DummyStatusLabel()
        self.reset_calls = 0
        self.refresh_list_calls = 0
        self.config_calls = []
        self.errors = []
        self.refresh_messages = []
        self.after_calls = []

    def ensure_update_allowed(self):
        return True

    def show_busy_error(self):
        self.errors.append("busy")

    def show_refresh_busy_error(self):
        self.errors.append("refresh_busy")

    def set_refresh_in_progress(self, message):
        self.refresh_in_progress = True
        self.refresh_messages.append(message)

    def clear_refresh_in_progress(self):
        self.refresh_in_progress = False

    def safe_config(self, widget, **kwargs):
        self.config_calls.append((widget, kwargs))

    def apply_loaded_data(self, result, show_empty_warning):
        self.today_orders, self.sheet, self.all_existing_codes, self.last_sync_result = result

    def reset_current_selection(self):
        self.reset_calls += 1
        self.current_order = None

    def refresh_legal_list(self):
        self.refresh_list_calls += 1

    def show_error(self, message, popup=False):
        self.errors.append((message, popup))

    def after(self, delay, callback):
        self.after_calls.append((delay, callback))

    def sync_skladbot_async(self):
        self.after_calls.append(("sync_skladbot_async", None))

    def run_background(self, _title, work, on_success=None, on_error=None, on_finally=None):
        try:
            result = work()
        except Exception as exc:
            if on_error:
                on_error(exc)
        else:
            if on_success:
                on_success(result)
        finally:
            if on_finally:
                on_finally()


class RefreshFallbackTests(unittest.TestCase):
    def test_on_close_has_telegram_lock_helper_imported(self):
        self.assertTrue(callable(main.ScanningApp.on_close.__globals__["telegram_single_listener_lock_enabled"]))

    def test_initial_load_does_not_require_skladbot_number(self):
        original_get_today_orders = refresh_service.get_today_orders
        original_get_all_existing_codes = refresh_service.get_all_existing_codes
        original_get_pending_codes = refresh_service.get_pending_codes
        original_get_pending_backend_codes = refresh_service.get_pending_backend_codes
        try:
            calls = []
            sheet = object()
            google_orders = [{"Клиент": "Test Client"}]

            def fake_get_today_orders(apply_skladbot_filter=None, include_rows=False):
                calls.append(apply_skladbot_filter)
                if include_rows:
                    return google_orders, sheet, []
                return google_orders, sheet

            refresh_service.get_today_orders = fake_get_today_orders
            refresh_service.get_all_existing_codes = lambda sheet, all_rows=None: set()
            refresh_service.get_pending_codes = lambda: set()
            refresh_service.get_pending_backend_codes = lambda: set()

            orders, _, _ = main.fetch_sheet_data()

            self.assertEqual(orders, google_orders)
            self.assertEqual(calls, [False])
        finally:
            refresh_service.get_today_orders = original_get_today_orders
            refresh_service.get_all_existing_codes = original_get_all_existing_codes
            refresh_service.get_pending_codes = original_get_pending_codes
            refresh_service.get_pending_backend_codes = original_get_pending_backend_codes

    def test_refresh_exposes_pending_backend_codes_as_known_duplicates(self):
        original_backend_read_orders_enabled = refresh_service.backend_read_orders_enabled
        original_get_today_orders = refresh_service.get_today_orders
        original_get_all_existing_codes = refresh_service.get_all_existing_codes
        original_get_pending_codes = refresh_service.get_pending_codes
        original_get_pending_backend_codes = refresh_service.get_pending_backend_codes
        try:
            sheet = object()
            google_orders = [{"Клиент": "Test Client"}]

            refresh_service.backend_read_orders_enabled = lambda: False
            refresh_service.get_today_orders = lambda apply_skladbot_filter=None, include_rows=False: (
                (google_orders, sheet, []) if include_rows else (google_orders, sheet)
            )
            refresh_service.get_all_existing_codes = lambda sheet, all_rows=None: {"01000000000000000001"}
            refresh_service.get_pending_codes = lambda: {"01000000000000000002"}
            refresh_service.get_pending_backend_codes = lambda: {"01000000000000000003"}

            _orders, _sheet, all_existing_codes = main.fetch_sheet_data()

            self.assertEqual(
                all_existing_codes,
                {
                    "01000000000000000001",
                    "01000000000000000002",
                    "01000000000000000003",
                },
            )
        finally:
            refresh_service.backend_read_orders_enabled = original_backend_read_orders_enabled
            refresh_service.get_today_orders = original_get_today_orders
            refresh_service.get_all_existing_codes = original_get_all_existing_codes
            refresh_service.get_pending_codes = original_get_pending_codes
            refresh_service.get_pending_backend_codes = original_get_pending_backend_codes

    def test_returns_google_orders_when_skladbot_sync_fails(self):
        original_get_today_orders = refresh_service.get_today_orders
        original_sync_pending_saves = refresh_service.sync_pending_saves
        original_sync_skladbot_request_numbers = refresh_service.sync_skladbot_request_numbers
        original_get_all_existing_codes = refresh_service.get_all_existing_codes
        original_get_pending_codes = refresh_service.get_pending_codes
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

            refresh_service.get_today_orders = fake_get_today_orders
            refresh_service.sync_pending_saves = lambda sheet=None: {"synced": 0, "failed": 0, "remaining": 0}
            refresh_service.sync_skladbot_request_numbers = lambda sheet: {
                "enabled": True,
                "updated": 2,
                "matched": 0,
                "not_found": 0,
                "multiple": 0,
                "errors": 1,
                "message": "timeout",
            }
            refresh_service.get_all_existing_codes = lambda sheet, all_rows=None: set()
            refresh_service.get_pending_codes = lambda: set()

            orders, _, _, sync_result = main.fetch_sheet_data_with_sync()

            self.assertEqual(orders, fallback_orders)
            self.assertEqual(calls, [False])
            self.assertEqual(sync_result["skladbot"]["errors"], 1)
        finally:
            refresh_service.get_today_orders = original_get_today_orders
            refresh_service.sync_pending_saves = original_sync_pending_saves
            refresh_service.sync_skladbot_request_numbers = original_sync_skladbot_request_numbers
            refresh_service.get_all_existing_codes = original_get_all_existing_codes
            refresh_service.get_pending_codes = original_get_pending_codes

    def test_keeps_google_orders_without_skladbot_number_after_successful_sync(self):
        original_get_today_orders = refresh_service.get_today_orders
        original_sync_pending_saves = refresh_service.sync_pending_saves
        original_sync_skladbot_request_numbers = refresh_service.sync_skladbot_request_numbers
        original_get_all_existing_codes = refresh_service.get_all_existing_codes
        original_get_pending_codes = refresh_service.get_pending_codes
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

            refresh_service.get_today_orders = fake_get_today_orders
            refresh_service.sync_pending_saves = lambda sheet=None: {"synced": 0, "failed": 0, "remaining": 0}
            refresh_service.sync_skladbot_request_numbers = lambda sheet: {
                "enabled": True,
                "updated": 1,
                "matched": 1,
                "not_found": 1,
                "multiple": 0,
                "errors": 0,
                "message": "",
            }
            refresh_service.get_all_existing_codes = lambda sheet, all_rows=None: set()
            refresh_service.get_pending_codes = lambda: set()

            orders, _, _, sync_result = main.fetch_sheet_data_with_sync()

            self.assertEqual(orders, google_orders)
            self.assertEqual(calls, [False, False])
            self.assertEqual(sync_result["skladbot"]["not_found"], 1)
        finally:
            refresh_service.get_today_orders = original_get_today_orders
            refresh_service.sync_pending_saves = original_sync_pending_saves
            refresh_service.sync_skladbot_request_numbers = original_sync_skladbot_request_numbers
            refresh_service.get_all_existing_codes = original_get_all_existing_codes
            refresh_service.get_pending_codes = original_get_pending_codes

    def test_can_refresh_without_blocking_on_skladbot_sync(self):
        original_get_today_orders = refresh_service.get_today_orders
        original_sync_pending_saves = refresh_service.sync_pending_saves
        original_sync_skladbot_request_numbers = refresh_service.sync_skladbot_request_numbers
        original_get_all_existing_codes = refresh_service.get_all_existing_codes
        original_get_pending_codes = refresh_service.get_pending_codes
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

            refresh_service.get_today_orders = fake_get_today_orders
            refresh_service.sync_pending_saves = lambda sheet=None: {"synced": 0, "failed": 0, "remaining": 0}
            refresh_service.sync_skladbot_request_numbers = fail_skladbot_sync
            refresh_service.get_all_existing_codes = lambda sheet, all_rows=None: set()
            refresh_service.get_pending_codes = lambda: set()

            orders, _, _, sync_result = main.fetch_sheet_data_with_sync(sync_skladbot=False)

            self.assertEqual(orders, google_orders)
            self.assertEqual(calls, [False])
            self.assertEqual(sync_result["skladbot"]["enabled"], False)
        finally:
            refresh_service.get_today_orders = original_get_today_orders
            refresh_service.sync_pending_saves = original_sync_pending_saves
            refresh_service.sync_skladbot_request_numbers = original_sync_skladbot_request_numbers
            refresh_service.get_all_existing_codes = original_get_all_existing_codes
            refresh_service.get_pending_codes = original_get_pending_codes

    def test_backend_refresh_loads_backend_orders_as_primary_source(self):
        original_backend_read_orders_enabled = refresh_service.backend_read_orders_enabled
        original_sync_pending_backend_events = refresh_service.sync_pending_backend_events
        original_sync_backend_sources = refresh_service.sync_backend_sources
        original_fetch_backend_sheet_data = refresh_service.fetch_backend_sheet_data
        original_get_today_orders = refresh_service.get_today_orders
        original_get_all_existing_codes = refresh_service.get_all_existing_codes
        original_get_pending_codes = refresh_service.get_pending_codes
        original_get_pending_backend_codes = refresh_service.get_pending_backend_codes
        try:
            calls = []
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

            refresh_service.backend_read_orders_enabled = lambda: True
            refresh_service.sync_pending_backend_events = lambda: {"enabled": True, "remaining": 0}
            refresh_service.sync_backend_sources = fake_sync_backend_sources
            refresh_service.fetch_backend_sheet_data = lambda: (backend_orders, None, set())
            refresh_service.get_today_orders = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Google should not be primary in backend mode"))
            refresh_service.get_all_existing_codes = lambda sheet, all_rows=None: set()
            refresh_service.get_pending_codes = lambda: set()
            refresh_service.get_pending_backend_codes = lambda: set()

            orders, _, _, sync_result = main.fetch_sheet_data_with_sync(sync_skladbot=True)

            self.assertEqual(orders, backend_orders)
            self.assertEqual(calls, [(True, False)])
            self.assertEqual(sync_result["google_sheets"]["items_updated"], 1)
            self.assertEqual(sync_result["skladbot"]["matched"], 1)
            self.assertEqual(sync_result["primary_source"], "backend")
        finally:
            refresh_service.backend_read_orders_enabled = original_backend_read_orders_enabled
            refresh_service.sync_pending_backend_events = original_sync_pending_backend_events
            refresh_service.sync_backend_sources = original_sync_backend_sources
            refresh_service.fetch_backend_sheet_data = original_fetch_backend_sheet_data
            refresh_service.get_today_orders = original_get_today_orders
            refresh_service.get_all_existing_codes = original_get_all_existing_codes
            refresh_service.get_pending_codes = original_get_pending_codes
            refresh_service.get_pending_backend_codes = original_get_pending_backend_codes

    def test_backend_refresh_falls_back_to_google_when_backend_primary_fails(self):
        original_backend_read_orders_enabled = refresh_service.backend_read_orders_enabled
        original_sync_pending_backend_events = refresh_service.sync_pending_backend_events
        original_sync_backend_sources = refresh_service.sync_backend_sources
        original_fetch_backend_sheet_data = refresh_service.fetch_backend_sheet_data
        original_get_today_orders = refresh_service.get_today_orders
        original_get_all_existing_codes = refresh_service.get_all_existing_codes
        original_get_pending_codes = refresh_service.get_pending_codes
        original_get_pending_backend_codes = refresh_service.get_pending_backend_codes
        try:
            google_orders = [{"Клиент": "Google Client", "Кол-во блок": 1}]
            sheet = object()

            refresh_service.backend_read_orders_enabled = lambda: True
            refresh_service.sync_pending_backend_events = lambda: {"enabled": True, "remaining": 0}
            refresh_service.sync_backend_sources = lambda sync_skladbot=True, wait_skladbot=True: {
                "status": "completed",
                "google_sheets": {"status": "completed"},
                "skladbot": {"status": "skipped"},
            }
            refresh_service.get_today_orders = lambda apply_skladbot_filter=None, include_rows=False: (
                (google_orders, sheet, []) if include_rows else (google_orders, sheet)
            )
            refresh_service.get_all_existing_codes = lambda sheet, all_rows=None: set()
            refresh_service.get_pending_codes = lambda: set()
            refresh_service.fetch_backend_sheet_data = lambda: (_ for _ in ()).throw(RuntimeError("Backend down"))
            refresh_service.get_pending_backend_codes = lambda: set()

            orders, _, _, sync_result = main.fetch_sheet_data_with_sync(sync_skladbot=False)

            self.assertEqual(orders, google_orders)
            self.assertEqual(sync_result["primary_source"], "google_fallback")
        finally:
            refresh_service.backend_read_orders_enabled = original_backend_read_orders_enabled
            refresh_service.sync_pending_backend_events = original_sync_pending_backend_events
            refresh_service.sync_backend_sources = original_sync_backend_sources
            refresh_service.fetch_backend_sheet_data = original_fetch_backend_sheet_data
            refresh_service.get_today_orders = original_get_today_orders
            refresh_service.get_all_existing_codes = original_get_all_existing_codes
            refresh_service.get_pending_codes = original_get_pending_codes
            refresh_service.get_pending_backend_codes = original_get_pending_backend_codes

    def test_refresh_from_sheet_preserves_current_position(self):
        original_fetch_sheet_data_with_sync = app_data_loading.fetch_sheet_data_with_sync
        original_backend_read_orders_enabled = app_data_loading.backend_read_orders_enabled
        try:
            loaded_orders = [{"ID заказа": "order-1", "Товары": "Chapman Brown OP 20"}]
            app_data_loading.fetch_sheet_data_with_sync = lambda sync_skladbot=True: (
                loaded_orders,
                None,
                {"01012345678901234567ABC"},
                {
                    "backend": {"enabled": True, "remaining": 0},
                    "google_sheets": {"orders_updated": 0, "items_updated": 0},
                    "skladbot": {"enabled": False},
                    "primary_source": "backend",
                },
            )
            app_data_loading.backend_read_orders_enabled = lambda: True
            app = FakeRefreshApp()
            original_order = app.current_order

            app.refresh_from_sheet(initial=False)

            self.assertIs(app.current_order, original_order)
            self.assertEqual(app.reset_calls, 0)
            self.assertEqual(app.refresh_list_calls, 1)
            self.assertIn("сканирование доступно", app.refresh_messages[0])
            self.assertIn("текущая позиция сохранена", app.status_var.value)
            self.assertFalse(app.refresh_in_progress)
        finally:
            app_data_loading.fetch_sheet_data_with_sync = original_fetch_sheet_data_with_sync
            app_data_loading.backend_read_orders_enabled = original_backend_read_orders_enabled

    def test_refresh_error_message_keeps_cached_orders(self):
        message = main.format_refresh_error_message(
            RuntimeError("Google Sheets временно ограничил запросы"),
            has_cached_orders=True,
        )

        self.assertIn("последним загруженным списком", message)
        self.assertIn("повторите обновление позже", message)


if __name__ == "__main__":
    unittest.main()
