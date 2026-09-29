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


class BackendSyncTimerTests(unittest.TestCase):
    """Фоновая синхронизация backend-очереди держит одну цепочку отложенных вызовов."""

    def make_app(self):
        class FakeApp(DataLoadingMixin):
            def __init__(self):
                self.backend_sync_running = False
                self.last_sync_result = {"synced": 0, "failed": 0, "remaining": 0}
                self.current_order = None
                self.next_id = 0
                self.pending = {}
                self.cancelled = []
                self.background_jobs = []
                self.stats_updates = 0
                self.blocked_applied = []
                self.cancel_error = None

            def after(self, delay, callback):
                self.next_id += 1
                after_id = f"after#{self.next_id}"
                self.pending[after_id] = (delay, callback)
                return after_id

            def after_cancel(self, after_id):
                self.cancelled.append(after_id)
                if self.cancel_error is not None:
                    raise self.cancel_error
                self.pending.pop(after_id, None)

            def run_background(self, title, work, on_success=None, on_error=None, on_finally=None):
                self.background_jobs.append((work, on_success, on_error, on_finally))

            def update_stats_display(self):
                self.stats_updates += 1

            def apply_backend_blocked_scan_events(self, events):
                self.blocked_applied.append(events)

        return FakeApp()

    @staticmethod
    def finish_job(job, result):
        work, on_success, _on_error, on_finally = job
        with mock.patch(
            "taksklad.app_data_loading.sync_pending_backend_events",
            return_value=result,
        ) as sync:
            produced = work()
        on_success(produced)
        on_finally()
        return sync

    def test_three_manual_calls_and_pass_completion_leave_one_pending_call(self):
        app = self.make_app()
        app.schedule_backend_sync(13000)

        with mock.patch("taksklad.app_data_loading.backend_enabled", return_value=True):
            DataLoadingMixin.sync_backend_events_async(app)
            DataLoadingMixin.sync_backend_events_async(app)
            DataLoadingMixin.sync_backend_events_async(app)

        self.assertEqual(len(app.background_jobs), 1)
        self.assertEqual(len(app.pending), 1)

        self.finish_job(app.background_jobs[0], {"synced": 1, "failed": 0, "remaining": 0, "blocked_events": []})

        self.assertEqual(len(app.pending), 1)
        delay, callback = next(iter(app.pending.values()))
        self.assertEqual(delay, 15000)
        self.assertEqual(callback, app.sync_backend_events_async)
        self.assertFalse(app.backend_sync_running)

    def test_manual_call_runs_pass_at_once_and_leaves_single_call_after_it(self):
        app = self.make_app()
        app.schedule_backend_sync(13000)

        with mock.patch("taksklad.app_data_loading.backend_enabled", return_value=True):
            DataLoadingMixin.sync_backend_events_async(app)

        # проход стартовал сразу, а не отложен
        self.assertEqual(len(app.background_jobs), 1)
        self.assertTrue(app.backend_sync_running)

        self.finish_job(app.background_jobs[0], {"synced": 0, "failed": 0, "remaining": 0, "blocked_events": []})

        self.assertEqual(len(app.pending), 1)
        self.assertEqual(next(iter(app.pending.values()))[0], 15000)

    def test_repeated_cycles_never_accumulate_pending_calls(self):
        app = self.make_app()
        app.schedule_backend_sync(13000)

        with mock.patch("taksklad.app_data_loading.backend_enabled", return_value=True):
            for _ in range(5):
                DataLoadingMixin.sync_backend_events_async(app)
                self.finish_job(
                    app.background_jobs[-1],
                    {"synced": 0, "failed": 0, "remaining": 0, "blocked_events": []},
                )
                self.assertEqual(len(app.pending), 1)

    def test_backend_disabled_still_keeps_single_pending_call(self):
        app = self.make_app()
        app.schedule_backend_sync(13000)

        with mock.patch("taksklad.app_data_loading.backend_enabled", return_value=False):
            DataLoadingMixin.sync_backend_events_async(app)
            DataLoadingMixin.sync_backend_events_async(app)

        self.assertEqual(app.background_jobs, [])
        self.assertEqual(len(app.pending), 1)
        self.assertEqual(next(iter(app.pending.values()))[0], 15000)

    def test_schedule_cancels_previous_call_and_keeps_requested_delay(self):
        app = self.make_app()

        app.schedule_backend_sync(13000)
        first_id = next(iter(app.pending))
        app.schedule_backend_sync()

        self.assertEqual(app.cancelled, [first_id])
        self.assertEqual(len(app.pending), 1)
        self.assertEqual(next(iter(app.pending.values()))[0], 15000)

    def test_schedule_survives_tk_error_when_cancelling_already_fired_call(self):
        import tkinter as tk

        app = self.make_app()
        app.schedule_backend_sync(13000)
        app.cancel_error = tk.TclError("bad id")

        app.schedule_backend_sync()

        self.assertEqual(len(app.cancelled), 1)
        self.assertEqual(app.next_id, 2)

    def test_schedule_survives_tk_error_when_window_is_gone(self):
        import tkinter as tk

        app = self.make_app()

        def broken_after(delay, callback):
            raise tk.TclError("application has been destroyed")

        app.after = broken_after

        app.schedule_backend_sync()

        self.assertIsNone(app.backend_sync_after_id)

    def test_failed_pass_reports_error_and_still_leaves_single_pending_call(self):
        app = self.make_app()
        app.schedule_backend_sync(13000)
        pending_events = [{"id": "e1"}, {"id": "e2"}]

        with mock.patch("taksklad.app_data_loading.backend_enabled", return_value=True):
            DataLoadingMixin.sync_backend_events_async(app)
        self.assertTrue(app.backend_sync_running)

        work, on_success, on_error, on_finally = app.background_jobs[0]
        with mock.patch(
            "taksklad.app_data_loading.sync_pending_backend_events",
            side_effect=RuntimeError("связь прервалась"),
        ):
            with self.assertRaises(RuntimeError) as raised:
                work()
        # run_background при исключении в work зовёт on_error, потом on_finally
        with mock.patch("taksklad.app_data_loading.load_pending_backend_events", return_value=pending_events):
            on_error(raised.exception)
        on_finally()

        self.assertEqual(
            app.last_sync_result["backend"],
            {"enabled": True, "failed": 1, "remaining": 2},
        )
        self.assertEqual(app.stats_updates, 1)
        self.assertFalse(app.backend_sync_running)
        self.assertEqual(len(app.pending), 1)
        self.assertEqual(next(iter(app.pending.values()))[0], 15000)

    def test_background_pass_uses_background_mode(self):
        app = self.make_app()

        with mock.patch("taksklad.app_data_loading.backend_enabled", return_value=True):
            DataLoadingMixin.sync_backend_events_async(app)
        sync = self.finish_job(
            app.background_jobs[0],
            {"synced": 0, "failed": 0, "remaining": 0, "blocked_events": []},
        )

        sync.assert_called_once_with(background=True)

    def test_skipped_tick_keeps_last_sync_result_and_plans_next_tick(self):
        app = self.make_app()
        previous = {"enabled": True, "failed": 1, "remaining": 3}
        app.last_sync_result["backend"] = previous
        app.current_order = {"_backend_order_item_id": "item-1"}
        skipped = {
            "synced": 0,
            "failed": 0,
            "remaining": 0,
            "blocked": 0,
            "blocked_events": [
                {"type": "scan", "payload": {"order_item_id": "item-1", "code": "c"}},
            ],
            "enabled": True,
            "skipped": True,
        }

        with mock.patch("taksklad.app_data_loading.backend_enabled", return_value=True):
            DataLoadingMixin.sync_backend_events_async(app)
        self.finish_job(app.background_jobs[0], skipped)

        self.assertIs(app.last_sync_result["backend"], previous)
        self.assertEqual(app.blocked_applied, [])
        self.assertEqual(app.stats_updates, 0)
        self.assertEqual(len(app.pending), 1)
        self.assertEqual(next(iter(app.pending.values()))[0], 15000)
        self.assertFalse(app.backend_sync_running)

    def test_real_pass_result_still_overwrites_last_sync_result_and_applies_blocked(self):
        app = self.make_app()
        app.current_order = {"_backend_order_item_id": "item-1"}
        blocked_event = {"type": "scan", "payload": {"order_item_id": "item-1", "code": "c"}}
        result = {"synced": 0, "failed": 0, "remaining": 1, "blocked": 1, "blocked_events": [blocked_event]}

        with mock.patch("taksklad.app_data_loading.backend_enabled", return_value=True):
            DataLoadingMixin.sync_backend_events_async(app)
        self.finish_job(app.background_jobs[0], result)

        self.assertIs(app.last_sync_result["backend"], result)
        self.assertEqual(app.blocked_applied, [[blocked_event]])
        self.assertEqual(app.stats_updates, 1)

    def test_all_timer_call_sites_go_through_the_single_chain_helper(self):
        import inspect

        from taksklad import main as main_module

        loading_source = inspect.getsource(DataLoadingMixin.sync_backend_events_async)
        main_source = inspect.getsource(main_module.ScanningApp.__init__)

        self.assertNotIn("self.after(15000, self.sync_backend_events_async)", loading_source)
        self.assertNotIn("after(13000, self.sync_backend_events_async)", main_source)
        self.assertIn("schedule_backend_sync(13000)", main_source)


if __name__ == "__main__":
    unittest.main()
