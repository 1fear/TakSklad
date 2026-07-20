import unittest
from unittest import mock

from taksklad.app_data_loading import DataLoadingMixin
from taksklad.app_skladbot import SkladBotActionsMixin


class BackgroundRefreshTests(unittest.TestCase):
    def test_periodic_refresh_is_silent_and_reschedules_once(self):
        class FakeApp(SkladBotActionsMixin):
            update_required = False
            operation_in_progress = False
            refresh_in_progress = False
            current_order = None

            def __init__(self):
                self.refresh_calls = []
                self.after_calls = []

            def refresh_from_sheet(self, **kwargs):
                self.refresh_calls.append(kwargs)

            def after(self, delay, callback):
                self.after_calls.append((delay, callback))

        app = FakeApp()
        SkladBotActionsMixin.run_skladbot_periodic_refresh(app)

        self.assertEqual(app.refresh_calls, [{"background": True}])
        self.assertEqual(len(app.after_calls), 1)

    def test_background_refresh_does_not_touch_buttons_or_schedule_another_refresh(self):
        class FakeStatus:
            def set(self, value):
                self.value = value

        class FakeLabel:
            def config(self, **kwargs):
                self.kwargs = kwargs

        class FakeApp(SkladBotActionsMixin):
            operation_in_progress = False
            refresh_in_progress = False
            current_order = None
            today_orders = []
            last_sync_result = {}
            refresh_btn = object()
            import_btn = object()
            status_var = FakeStatus()
            status_label = FakeLabel()

            def __init__(self):
                self.button_updates = []
                self.after_calls = []
                self.refresh_modes = []

            def ensure_update_allowed(self):
                return True

            def set_refresh_in_progress(self, _message, *, announce=True):
                self.refresh_in_progress = True
                self.refresh_modes.append(announce)

            def clear_refresh_in_progress(self):
                self.refresh_in_progress = False

            def safe_config(self, widget, **kwargs):
                if widget in (self.refresh_btn, self.import_btn):
                    self.button_updates.append((widget, kwargs))

            def run_background(self, _title, worker, *, on_success, on_error, on_finally):
                try:
                    on_success(worker())
                except Exception as exc:  # pragma: no cover - defensive parity with runtime
                    on_error(exc)
                finally:
                    on_finally()

            def apply_loaded_data(self, _result, *, show_empty_warning):
                self.show_empty_warning = show_empty_warning

            def reset_current_selection(self):
                self.current_order = None

            def reconcile_current_order_after_refresh(self):
                return {"status": "merged"}

            def refresh_legal_list(self):
                pass

            def show_error(self, *_args, **_kwargs):
                raise AssertionError("background refresh should succeed")

            def after(self, delay, callback):
                self.after_calls.append((delay, callback))

        app = FakeApp()
        with mock.patch(
            "taksklad.app_data_loading.fetch_sheet_data_with_sync",
            return_value=([], None, set(), {}),
        ):
            DataLoadingMixin.refresh_from_sheet(app, background=True)

        self.assertEqual(app.refresh_modes, [False])
        self.assertEqual(app.button_updates, [])
        self.assertEqual(app.after_calls, [])
        self.assertFalse(app.refresh_in_progress)

    def test_background_refresh_skips_active_order(self):
        class FakeApp(SkladBotActionsMixin):
            update_required = False
            operation_in_progress = False
            refresh_in_progress = False
            current_order = {"id": "active-order"}

            def __init__(self):
                self.refresh_calls = []
                self.after_calls = []

            def refresh_from_sheet(self, **kwargs):
                self.refresh_calls.append(kwargs)

            def after(self, delay, callback):
                self.after_calls.append((delay, callback))

        app = FakeApp()
        SkladBotActionsMixin.run_skladbot_periodic_refresh(app)

        self.assertEqual(app.refresh_calls, [])
        self.assertEqual(len(app.after_calls), 1)


if __name__ == "__main__":
    unittest.main()
