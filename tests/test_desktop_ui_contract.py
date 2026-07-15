import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from taksklad import app_day_end, app_returns, app_runtime, app_telegram
from taksklad import main as main_module
from taksklad.config import (
    ACCENT,
    BG_MAIN,
    DANGER,
    ERROR_FG,
    FG_MUTED,
    FG_TEXT,
    WARNING,
)
from taksklad.config import SKLADBOT_REQUEST_NUMBER_COLUMN
from taksklad.app_day_end import build_backend_status
from taksklad import backend_client
from taksklad.main import (
    ScanningApp,
    backend_blocked_scan_events_for_item,
    format_backend_blocked_scan_message,
    backend_sync_group_blocker,
    backend_sync_item_blocker,
    complete_backend_orders_or_raise,
    format_print_failure_after_backend_complete,
    find_code_owner_in_orders,
    format_duplicate_scan_message,
    format_scan_product_mismatch_message,
    first_incomplete_order_index,
    group_finish_blocker,
    is_terminal_scan_state,
)
from taksklad.desktop_scan_rules import scan_sku_guard_status
from taksklad.startup_check import format_app_version_label
from taksklad.ui_widgets import AppButton, fade_hex


class DesktopUiContractTests(unittest.TestCase):
    def make_runtime_app_for_close(self, lock_owned_until=0):
        class FakeRuntimeApp:
            current_order = None
            scanned_codes = []
            saved_codes_count = 0
            telegram_lock_owner_id = "desktop-test"
            telegram_lock_owned_until = lock_owned_until

            def __init__(self):
                self.destroyed = False

            def destroy(self):
                self.destroyed = True

        return FakeRuntimeApp()

    def test_desktop_telegram_polling_is_disabled_by_config_even_when_bot_is_configured(self):
        settings = {
            "enabled": True,
            "bot_token": "telegram-token",
            "chat_ids": ["123"],
        }
        with mock.patch("taksklad.app_telegram.TELEGRAM_DESKTOP_POLLING_ENABLED", False):
            self.assertFalse(app_telegram.desktop_telegram_polling_enabled(settings))

    def test_desktop_telegram_polling_can_still_be_enabled_as_legacy_fallback(self):
        settings = {
            "enabled": True,
            "bot_token": "telegram-token",
            "chat_ids": ["123"],
        }
        with mock.patch("taksklad.app_telegram.TELEGRAM_DESKTOP_POLLING_ENABLED", True):
            self.assertTrue(app_telegram.desktop_telegram_polling_enabled(settings))

    def test_disabled_desktop_telegram_polling_does_not_touch_lock_or_state_path(self):
        class FakeTelegramApp:
            telegram_poll_running = False

            def __init__(self):
                self.after_calls = []

            def after(self, delay, callback):
                self.after_calls.append((delay, callback))

            def poll_telegram_bot_async(self):
                self.after_calls.append(("rescheduled", None))

            def ensure_telegram_poll_lock(self, settings):
                raise AssertionError("desktop polling should not acquire Google lock")

            def process_telegram_updates(self, settings):
                raise AssertionError("desktop polling should not process updates")

        app = FakeTelegramApp()
        settings = {
            "enabled": True,
            "bot_token": "telegram-token",
            "chat_ids": ["123"],
        }

        with (
            mock.patch("taksklad.app_telegram.TELEGRAM_DESKTOP_POLLING_ENABLED", False),
            mock.patch("taksklad.app_telegram.load_telegram_settings", return_value=settings),
        ):
            app_telegram.TelegramActionsMixin.poll_telegram_bot_async(app)

        self.assertEqual(len(app.after_calls), 1)
        self.assertEqual(app.after_calls[0][0], 15000)

    def test_close_does_not_release_telegram_lock_when_desktop_never_owned_it(self):
        app = self.make_runtime_app_for_close(lock_owned_until=0)

        with (
            mock.patch("taksklad.app_runtime.telegram_single_listener_lock_enabled", return_value=True),
            mock.patch("taksklad.app_runtime.release_telegram_poll_lock") as release_lock,
        ):
            app_runtime.AppRuntimeMixin.on_close(app)

        release_lock.assert_not_called()
        self.assertTrue(app.destroyed)

    def test_close_releases_telegram_lock_when_desktop_owns_it(self):
        app = self.make_runtime_app_for_close(lock_owned_until=10**12)

        with (
            mock.patch("taksklad.app_runtime.telegram_single_listener_lock_enabled", return_value=True),
            mock.patch("taksklad.app_runtime.release_telegram_poll_lock") as release_lock,
        ):
            app_runtime.AppRuntimeMixin.on_close(app)

        release_lock.assert_called_once_with("desktop-test")
        self.assertTrue(app.destroyed)

    def test_close_releases_single_instance_lock_when_owned(self):
        app = self.make_runtime_app_for_close(lock_owned_until=0)
        lock = object()
        app.single_instance_lock = lock

        with mock.patch("taksklad.app_runtime.release_single_instance_lock") as release_lock:
            app_runtime.AppRuntimeMixin.on_close(app)

        release_lock.assert_called_once_with(lock)
        self.assertIsNone(app.single_instance_lock)
        self.assertTrue(app.destroyed)

    def test_close_keeps_single_instance_lock_when_user_cancels_unsaved_close(self):
        app = self.make_runtime_app_for_close(lock_owned_until=0)
        app.current_order = {"id": "order-1"}
        app.scanned_codes = ["code-1"]
        app.saved_codes_count = 0
        app.single_instance_lock = object()

        with (
            mock.patch("taksklad.app_runtime.messagebox.askyesno", return_value=False),
            mock.patch("taksklad.app_runtime.release_single_instance_lock") as release_lock,
        ):
            app_runtime.AppRuntimeMixin.on_close(app)

        release_lock.assert_not_called()
        self.assertIsNotNone(app.single_instance_lock)
        self.assertFalse(app.destroyed)

    def test_run_app_acquires_single_instance_before_local_writes(self):
        source = inspect.getsource(main_module.run_app)

        self.assertLess(
            source.index("acquire_single_instance_lock"),
            source.index("migrate_legacy_json_files_to_app_data"),
        )
        self.assertIn("show_startup_error_message(\"TakSklad уже запущен\"", source)
        self.assertIn("return 2", source)

    def test_run_app_denied_second_instance_stops_before_startup_writes(self):
        denied = SimpleNamespace(
            acquired=False,
            message="TakSklad уже запущен",
            reason="already_running",
            lock=None,
        )

        with (
            mock.patch("taksklad.main.acquire_single_instance_lock", return_value=denied),
            mock.patch("taksklad.main.show_startup_error_message") as show_startup_error,
            mock.patch("taksklad.main.maybe_rename_windows_executable") as rename,
            mock.patch("taksklad.main.ensure_windows_desktop_shortcut") as shortcut,
            mock.patch("taksklad.main.migrate_legacy_json_files_to_app_data") as migrate,
            mock.patch("taksklad.main.log_startup_self_check") as self_check,
            mock.patch("taksklad.main.ScanningApp") as scanning_app,
        ):
            result = main_module.run_app()

        self.assertEqual(result, 2)
        show_startup_error.assert_called_once_with("TakSklad уже запущен", "TakSklad уже запущен")
        rename.assert_not_called()
        shortcut.assert_not_called()
        migrate.assert_not_called()
        self_check.assert_not_called()
        scanning_app.assert_not_called()

    def test_sync_queue_retry_shows_blocker_without_sync_calls(self):
        fake = SimpleNamespace(
            build_sync_queue_window_summary=lambda: {
                "retry_enabled": False,
                "retry_blocker": "Backend не настроен",
            },
            show_warning=mock.Mock(),
        )

        result = app_runtime.AppRuntimeMixin.retry_sync_queues_from_window(fake)

        self.assertFalse(result)
        fake.show_warning.assert_called_once_with("Backend не настроен")

    def test_sync_queue_retry_calls_existing_sync_methods(self):
        class FakeWindow:
            def __init__(self):
                self.destroyed = False

            def destroy(self):
                self.destroyed = True

        fake = SimpleNamespace(
            build_sync_queue_window_summary=lambda: {
                "retry_enabled": True,
                "queues": {
                    "google_saves": {"count": 1},
                    "backend_scans": {"count": 1},
                    "backend_completes": {"count": 1},
                    "telegram": {"count": 1},
                    "prints": {"count": 1},
                },
            },
            show_info=mock.Mock(),
            refresh_from_sheet=mock.Mock(),
            sync_backend_events_async=mock.Mock(),
            sync_pending_telegram_async=mock.Mock(),
            check_pending_prints=mock.Mock(),
        )
        window = FakeWindow()

        result = app_runtime.AppRuntimeMixin.retry_sync_queues_from_window(fake, window)

        self.assertTrue(result)
        fake.show_info.assert_called_once()
        fake.refresh_from_sheet.assert_called_once()
        fake.sync_backend_events_async.assert_called_once()
        fake.sync_pending_telegram_async.assert_called_once()
        fake.check_pending_prints.assert_called_once()
        self.assertTrue(window.destroyed)

    def test_diagnostic_bundle_failure_is_operator_safe(self):
        fake = SimpleNamespace(show_error=mock.Mock())

        with (
            mock.patch("taksklad.app_runtime.write_diagnostic_bundle", side_effect=RuntimeError("token=SECRET")),
            mock.patch("taksklad.app_runtime.logging.exception") as log_exception,
        ):
            result = app_runtime.AppRuntimeMixin.create_diagnostic_bundle_for_support(fake)

        self.assertEqual(result, "")
        log_exception.assert_called_once_with("Не удалось создать диагностический пакет")
        fake.show_error.assert_called_once()
        self.assertIn("Подробности записаны в лог", fake.show_error.call_args.args[0])
        self.assertNotIn("SECRET", fake.show_error.call_args.args[0])

    def test_diagnostic_bundle_button_exists_on_warehouse_screen(self):
        build_source = inspect.getsource(ScanningApp._build_ui)

        self.assertIn("self.diagnostics_btn", build_source)
        self.assertIn("command=self.create_diagnostic_bundle_for_support", build_source)

    def test_main_warehouse_screen_uses_2_0_labels(self):
        source = inspect.getsource(ScanningApp._build_ui)

        expected_labels = [
            "Заказы для КИЗов",
            "ВОЗВРАТЫ",
            "ТЕКУЩАЯ ПОЗИЦИЯ",
            "Партия не выбрана",
            "СКАНИРОВАНИЕ КОДА",
            "ЗАВЕРШИТЬ ЗАКАЗ",
            "ЗАКРЫТЬ СМЕНУ",
        ]
        for label in expected_labels:
            self.assertIn(label, source)
        self.assertIn("OrderCardList", source)
        self.assertIn("PlaceholderEntry", source)
        self.assertIn("order_list_counter_label", source)
        self.assertNotIn("legal_listbox", source)
        self.assertNotIn("tk.Listbox", source)
        self.assertIn("version_status_label", source)
        self.assertIn("format_version_update_status_label", source)
        self.assertIn("build_version_update_status", source)
        self.assertIn("MVP 2.0", format_app_version_label())

    def test_scan_screen_shows_product_photo_for_current_position(self):
        build_source = inspect.getsource(ScanningApp._build_ui)
        load_source = inspect.getsource(ScanningApp.load_current_product)
        reset_source = inspect.getsource(ScanningApp.reset_current_selection)

        self.assertIn("self.product_photo_canvas", build_source)
        self.assertIn("self.product_photo_gtin_label", build_source)
        self.assertIn("self.product_photo_caption_label", build_source)
        self.assertIn("self.update_product_photo(product_text)", load_source)
        self.assertIn("self.update_product_photo(\"\")", reset_source)

    def test_scan_screen_typography_stays_compact(self):
        module_source = inspect.getsource(main_module)
        build_source = inspect.getsource(ScanningApp._build_ui)

        self.assertIn("PRIMARY_LABEL_FONT = (UI_FONT, 15", module_source)
        self.assertIn("PRODUCT_LABEL_FONT = (UI_FONT, 15", module_source)
        self.assertNotIn("PRIMARY_LABEL_FONT = (UI_FONT, 18", module_source)
        self.assertNotIn("PRODUCT_LABEL_FONT = (UI_FONT, 17", module_source)
        self.assertIn("fg=FG_MUTED,\n            font=BODY_FONT", build_source)

    def test_main_warehouse_screen_does_not_show_legacy_admin_buttons(self):
        source = inspect.getsource(ScanningApp._build_ui)

        legacy_labels = [
            "Заказы на сегодня",
            "ИМПОРТ EXCEL",
            "ТОВАРЫ",
            "КОНТРОЛЬ",
            "ЗАВЕРШИТЬ ДЕНЬ",
        ]
        for label in legacy_labels:
            self.assertNotIn(label, source)

    def test_print_is_not_visible_on_main_screen_before_order_finish(self):
        build_source = inspect.getsource(ScanningApp._build_ui)
        finish_source = inspect.getsource(ScanningApp.finish_legal_entity)

        self.assertNotIn("ПЕЧАТАТЬ", build_source)
        self.assertNotIn("Печать сводного листа", build_source)
        self.assertIn("self.confirm_print_settings()", finish_source)
        self.assertIn("print_summary(address, summary_products, print_settings=selected_print_settings)", finish_source)

    def test_final_position_guides_warehouse_to_finish_order(self):
        load_source = inspect.getsource(ScanningApp.load_current_product)
        scan_source = inspect.getsource(ScanningApp.on_scan)
        finish_source = inspect.getsource(ScanningApp.finish_legal_entity)
        next_source = inspect.getsource(ScanningApp.next_product)

        self.assertIn("self.finish_btn.config(state=\"disabled\")", load_source)
        self.assertIn("Заказ выполнен! Нажмите 'ЗАВЕРШИТЬ ЗАКАЗ'", scan_source)
        self.assertIn("self.current_product_idx >= len(self.current_legal_entity_orders) - 1", scan_source)
        self.assertIn("self.current_product_idx == len(self.current_legal_entity_orders) - 1", finish_source)
        self.assertIn("self.next_product(finish_after_save=True)", finish_source)
        self.assertIn("finish_legal_entity(from_next_product=True)", next_source)
        self.assertIn("Все позиции сохранены", next_source)

    def test_next_product_hard_error_keeps_final_position_actions_consistent(self):
        class FakeVar:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class FakeWidget:
            def __init__(self):
                self.options = {}

            def config(self, **kwargs):
                self.options.update(kwargs)

        def run_background(_title, work, on_success=None, on_error=None):
            try:
                result = work()
            except Exception as exc:
                if on_error:
                    on_error(exc)
            else:
                if on_success:
                    on_success(result)

        first_code = "01040063960540670001"
        second_code = "01040063960540670002"
        order = {
            "Кол-во блок": 2,
            "Товары": "Chapman Brown SSL",
            "Отсканированные коды": "",
        }
        fake = SimpleNamespace(
            ensure_update_allowed=lambda: True,
            operation_in_progress=False,
            current_order=order,
            current_product_idx=0,
            current_legal_entity_orders=[order],
            current_legal_entity_products=[],
            completed_orders=[],
            scanned_codes=[first_code, second_code],
            sheet=object(),
            product_catalog={},
            next_product_btn=FakeWidget(),
            finish_btn=FakeWidget(),
            status_var=FakeVar(),
            status_label=FakeWidget(),
            safe_config=lambda widget, **kwargs: widget.config(**kwargs),
            set_busy=lambda message: setattr(fake, "operation_in_progress", True) or fake.status_var.set(message),
            clear_busy=lambda: setattr(fake, "operation_in_progress", False),
            show_critical_error=mock.Mock(),
            run_background=run_background,
        )

        with (
            mock.patch("taksklad.app_scanning.order_uses_backend_scan_path", return_value=False),
            mock.patch("taksklad.app_scanning.update_scanned_codes_to_gsheet", return_value=(False, "fatal")),
            mock.patch("taksklad.app_scanning.is_retryable_save_error", return_value=False),
        ):
            ScanningApp.next_product(fake, finish_after_save=True)

        self.assertIs(fake.current_order, order)
        self.assertEqual(fake.current_product_idx, 0)
        self.assertEqual(fake.current_legal_entity_products, [])
        self.assertEqual(fake.completed_orders, [])
        self.assertFalse(fake.operation_in_progress)
        self.assertEqual(fake.next_product_btn.options["state"], "disabled")
        self.assertEqual(fake.finish_btn.options["state"], "normal")
        fake.show_critical_error.assert_called_once()

    def test_scan_rejects_wrong_sku_before_local_backup_and_backend_queue(self):
        source = inspect.getsource(ScanningApp.on_scan)

        self.assertIn("scan_product_mismatch", source)
        self.assertLess(source.index("scan_product_mismatch"), source.index("write_scan_backup"))
        self.assertLess(source.index("scan_product_mismatch"), source.index("queue_backend_scan"))
        self.assertIn("format_scan_product_mismatch_message", source)

    def test_scan_rejects_duplicates_before_local_backup_and_backend_queue(self):
        source = inspect.getsource(ScanningApp.on_scan)

        backup_idx = source.index("write_scan_backup")
        backend_queue_idx = source.index("queue_backend_scan")
        duplicate_guards = [
            "if code in self.scanned_codes:",
            "if code in self.all_existing_codes:",
            "duplicate_in_completed_orders = any(",
        ]

        for guard in duplicate_guards:
            self.assertIn(guard, source)
            self.assertLess(source.index(guard), backup_idx)
            self.assertLess(source.index(guard), backend_queue_idx)

        self.assertIn("format_duplicate_scan_message", source)
        self.assertIn("log_duplicate_code_async", source)

    def test_scan_entry_starts_disabled_until_order_selected(self):
        source = inspect.getsource(ScanningApp._build_ui)

        self.assertIn("self.scan_guard_label", source)
        self.assertIn('state="disabled"', source)

    def test_scan_sku_guard_status_covers_active_unknown_and_unavailable(self):
        active = scan_sku_guard_status({"Товары": "Chapman Brown OP 20"})
        unknown = scan_sku_guard_status({"Товары": "Unmapped Product"})
        unavailable = scan_sku_guard_status(None)

        self.assertEqual(active["state"], "active")
        self.assertIn("активна", active["message"])
        self.assertEqual(unknown["state"], "unknown")
        self.assertIn("не активна", unknown["message"])
        self.assertEqual(unavailable["state"], "unavailable")
        self.assertIn("недоступна", unavailable["message"])

    def test_scan_without_order_does_not_write_backup_or_backend_queue(self):
        class FakeWidget:
            def __init__(self, value=""):
                self.value = value
                self.deleted = False
                self.focused = False

            def get(self):
                return self.value

            def delete(self, *_args):
                self.value = ""
                self.deleted = True

            def focus_set(self):
                self.focused = True

        fake = SimpleNamespace(
            ensure_update_allowed=lambda: True,
            operation_in_progress=False,
            current_order=None,
            scanned_codes=[],
            all_existing_codes=set(),
            scan_entry=FakeWidget("0104006396053978-TEST-NO-ORDER"),
            show_error=mock.Mock(),
            show_busy_error=mock.Mock(),
            bell=mock.Mock(),
        )

        with (
            mock.patch("taksklad.app_scanning.write_scan_backup") as write_backup,
            mock.patch("taksklad.app_scanning.queue_backend_scan") as queue_scan,
            mock.patch("taksklad.app_scanning.add_pending_save") as add_pending,
        ):
            ScanningApp.on_scan(fake)

        write_backup.assert_not_called()
        queue_scan.assert_not_called()
        add_pending.assert_not_called()
        self.assertEqual(fake.scanned_codes, [])
        self.assertEqual(fake.all_existing_codes, set())
        self.assertEqual(fake.scan_feedback_state, "rejected")
        self.assertIn("Сначала выберите заказ", fake.last_scan_feedback_message)
        self.assertTrue(fake.scan_entry.deleted)
        self.assertTrue(fake.scan_entry.focused)
        fake.bell.assert_called_once()

    def test_scan_accept_sets_feedback_state_and_queues_once(self):
        class FakeVar:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class FakeWidget:
            def __init__(self, value=""):
                self.value = value
                self.options = {}
                self.deleted = False
                self.focused = False

            def get(self):
                return self.value

            def delete(self, *_args):
                self.value = ""
                self.deleted = True

            def config(self, **kwargs):
                self.options.update(kwargs)

            def focus_set(self):
                self.focused = True

        code = "0104006396104441-TEST-GREEN"
        order = {
            "Кол-во блок": 1,
            "Товары": "Chapman Green OP 20",
            "_backend_order_item_id": "item-green",
        }
        fake = SimpleNamespace(
            ensure_update_allowed=lambda: True,
            operation_in_progress=False,
            current_order=order,
            current_product_idx=0,
            current_legal_entity_orders=[order],
            scanned_codes=[],
            all_existing_codes=set(),
            today_orders=[],
            completed_orders=[],
            scan_entry=FakeWidget(code),
            progress_label=FakeWidget(),
            last_code_label=FakeWidget(),
            status_var=FakeVar(),
            status_label=FakeWidget(),
            next_product_btn=FakeWidget(),
            finish_btn=FakeWidget(),
            show_error=mock.Mock(),
            show_busy_error=mock.Mock(),
            log_duplicate_code_async=mock.Mock(),
            bell=mock.Mock(),
        )

        with (
            mock.patch("taksklad.app_scanning.write_scan_backup", return_value=True) as write_backup,
            mock.patch("taksklad.app_scanning.queue_backend_scan") as queue_scan,
        ):
            ScanningApp.on_scan(fake)

        write_backup.assert_called_once()
        queue_scan.assert_called_once_with(order, code)
        self.assertEqual(fake.scan_feedback_state, "accepted")
        self.assertIn("Отсканирован код", fake.last_scan_feedback_message)
        self.assertEqual(fake.scanned_codes, [code])
        self.assertEqual(fake.progress_label.options["text"], "1 / 1")
        self.assertTrue(fake.scan_entry.deleted)
        self.assertTrue(fake.scan_entry.focused)
        fake.bell.assert_not_called()

    def test_scan_wrong_sku_rejects_without_queue_and_keeps_focus(self):
        class FakeWidget:
            def __init__(self, value=""):
                self.value = value
                self.deleted = False
                self.focused = False

            def get(self):
                return self.value

            def delete(self, *_args):
                self.value = ""
                self.deleted = True

            def focus_set(self):
                self.focused = True

        code = "0104006396054067-TEST-BROWN-SSL"
        order = {"Кол-во блок": 1, "Товары": "Chapman RED SSL 100`20"}
        fake = SimpleNamespace(
            ensure_update_allowed=lambda: True,
            operation_in_progress=False,
            current_order=order,
            scanned_codes=[],
            all_existing_codes=set(),
            today_orders=[],
            completed_orders=[],
            scan_entry=FakeWidget(code),
            show_error=mock.Mock(),
            show_busy_error=mock.Mock(),
            log_duplicate_code_async=mock.Mock(),
            bell=mock.Mock(),
        )

        with (
            mock.patch("taksklad.app_scanning.write_scan_backup") as write_backup,
            mock.patch("taksklad.app_scanning.queue_backend_scan") as queue_scan,
        ):
            ScanningApp.on_scan(fake)

        write_backup.assert_not_called()
        queue_scan.assert_not_called()
        self.assertEqual(fake.scan_feedback_state, "rejected")
        self.assertIn("КИЗ не соответствует товару", fake.last_scan_feedback_message)
        self.assertEqual(fake.scanned_codes, [])
        self.assertTrue(fake.scan_entry.deleted)
        self.assertTrue(fake.scan_entry.focused)
        fake.bell.assert_called_once()

    def test_scan_allows_backend_released_kiz_from_stale_duplicate_cache(self):
        class FakeVar:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class FakeWidget:
            def __init__(self, value=""):
                self.value = value
                self.options = {}
                self.deleted = False
                self.focused = False

            def get(self):
                return self.value

            def delete(self, *_args):
                self.deleted = True

            def config(self, **kwargs):
                self.options.update(kwargs)

            def focus_set(self):
                self.focused = True

        code = "0104006396053978-TEST-BROWN-RETURN"
        order = {
            "Кол-во блок": 1,
            "Товары": "Chapman Brown OP 20",
            "_backend_order_item_id": "item-brown",
        }
        stale_owner_without_return_status = {
            "Клиент": "Returned Client",
            "Дата отгрузки": "15.07.2026",
            "Товары": "Chapman RED OP 20",
            "Отсканированные коды": code,
        }
        scenarios = (
            ({code}, []),
            (set(), []),
            ({code}, [stale_owner_without_return_status]),
        )
        for initial_codes, today_orders in scenarios:
            with self.subTest(initial_codes=initial_codes, today_orders=today_orders):
                fake = SimpleNamespace(
                    ensure_update_allowed=lambda: True,
                    operation_in_progress=False,
                    current_order=order,
                    current_product_idx=0,
                    current_legal_entity_orders=[order],
                    scanned_codes=[],
                    all_existing_codes=set(initial_codes),
                    today_orders=today_orders,
                    completed_orders=[{"Коды": [code]}],
                    scan_entry=FakeWidget(code),
                    progress_label=FakeWidget(),
                    last_code_label=FakeWidget(),
                    status_var=FakeVar(),
                    status_label=FakeWidget(),
                    next_product_btn=FakeWidget(),
                    finish_btn=FakeWidget(),
                    show_error=mock.Mock(),
                    show_busy_error=mock.Mock(),
                    log_duplicate_code_async=mock.Mock(),
                )

                with (
                    mock.patch(
                        "taksklad.app_scanning.backend_duplicate_scan_reuse_status",
                        return_value={
                            "checked": True,
                            "available": True,
                            "latest_movement_type": "return",
                            "reason": "latest movement is return",
                        },
                    ) as reuse_status,
                    mock.patch("taksklad.app_scanning.write_scan_backup", return_value=True) as write_backup,
                    mock.patch("taksklad.app_scanning.queue_backend_scan") as queue_scan,
                ):
                    ScanningApp.on_scan(fake)

                reuse_status.assert_called_once_with(order, code)
                write_backup.assert_called_once()
                queue_scan.assert_called_once_with(order, code)
                fake.show_error.assert_not_called()
                fake.log_duplicate_code_async.assert_not_called()
                self.assertEqual(fake.scanned_codes, [code])
                self.assertIn(code, fake.all_existing_codes)
                self.assertTrue(fake.scan_entry.deleted)
                self.assertTrue(fake.scan_entry.focused)

    def test_scan_keeps_completed_order_duplicate_when_backend_does_not_release(self):
        class FakeWidget:
            def __init__(self, value=""):
                self.value = value
                self.deleted = False

            def get(self):
                return self.value

            def delete(self, *_args):
                self.deleted = True

        code = "0104006396104441-TEST-GREEN"
        order = {
            "Кол-во блок": 1,
            "Товары": "Chapman Green OP 20",
            "_backend_order_item_id": "item-green",
        }
        fake = SimpleNamespace(
            ensure_update_allowed=lambda: True,
            operation_in_progress=False,
            current_order=order,
            scanned_codes=[],
            all_existing_codes=set(),
            today_orders=[],
            completed_orders=[{"Коды": [code]}],
            scan_entry=FakeWidget(code),
            show_error=mock.Mock(),
            show_busy_error=mock.Mock(),
            log_duplicate_code_async=mock.Mock(),
        )

        with (
            mock.patch(
                "taksklad.app_scanning.backend_duplicate_scan_reuse_status",
                return_value={
                    "checked": True,
                    "available": False,
                    "latest_movement_type": "outbound",
                    "reason": "latest movement is outbound",
                },
            ) as reuse_status,
            mock.patch("taksklad.app_scanning.write_scan_backup") as write_backup,
            mock.patch("taksklad.app_scanning.queue_backend_scan") as queue_scan,
        ):
            ScanningApp.on_scan(fake)

        reuse_status.assert_called_once_with(order, code)
        write_backup.assert_not_called()
        queue_scan.assert_not_called()
        fake.show_error.assert_called_once_with("Код уже использован в другом задании сегодня", popup=True)
        self.assertEqual(fake.scanned_codes, [])
        self.assertTrue(fake.scan_entry.deleted)

    def test_scan_keeps_active_order_duplicate_when_backend_reports_outbound(self):
        class FakeWidget:
            def __init__(self, value=""):
                self.value = value
                self.deleted = False

            def get(self):
                return self.value

            def delete(self, *_args):
                self.deleted = True

        code = "0104006396104441-TEST-GREEN"
        order = {
            "Кол-во блок": 1,
            "Товары": "Chapman Green OP 20",
            "_backend_order_item_id": "item-green",
        }
        fake = SimpleNamespace(
            ensure_update_allowed=lambda: True,
            operation_in_progress=False,
            current_order=order,
            scanned_codes=[],
            all_existing_codes={code},
            today_orders=[{
                "Клиент": "Other Client",
                "Дата отгрузки": "30.06.2026",
                "Товары": "Chapman Green OP 20",
                "Отсканированные коды": code,
            }],
            completed_orders=[],
            scan_entry=FakeWidget(code),
            show_error=mock.Mock(),
            show_busy_error=mock.Mock(),
            log_duplicate_code_async=mock.Mock(),
        )

        with (
            mock.patch(
                "taksklad.app_scanning.backend_duplicate_scan_reuse_status",
                return_value={
                    "checked": True,
                    "available": False,
                    "latest_movement_type": "outbound",
                    "reason": "latest movement is outbound",
                },
            ) as reuse_status,
            mock.patch("taksklad.app_scanning.write_scan_backup") as write_backup,
            mock.patch("taksklad.app_scanning.queue_backend_scan") as queue_scan,
        ):
            ScanningApp.on_scan(fake)

        reuse_status.assert_called_once_with(order, code)
        write_backup.assert_not_called()
        queue_scan.assert_not_called()
        fake.show_error.assert_called_once()
        fake.log_duplicate_code_async.assert_called_once_with(code)
        self.assertEqual(fake.scanned_codes, [])
        self.assertTrue(fake.scan_entry.deleted)

    def test_backend_sync_item_blocker_ignores_unrelated_poisoned_queue_event(self):
        sync_result = {
            "failed": 1,
            "remaining": 1,
            "blocked_events": [{
                "type": "scan",
                "payload": {"order_item_id": "old-item"},
                "last_error": "Code already scanned in another order item",
            }],
        }
        pending_events = [{
            "type": "scan",
            "payload": {"order_item_id": "old-item"},
            "last_error": "Code already scanned in another order item",
        }]

        self.assertEqual(backend_sync_item_blocker(sync_result, "current-item", pending_events), "")
        self.assertIn(
            "Code already scanned",
            backend_sync_item_blocker(sync_result, "old-item", pending_events),
        )

    def test_backend_blocked_scan_events_filter_current_item(self):
        sync_result = {
            "blocked_events": [
                {"type": "scan", "payload": {"order_item_id": "old-item", "code": "01000000000000000001"}},
                {"type": "scan", "payload": {"order_item_id": "item-1", "code": "01000000000000000002"}},
                {"type": "order_complete", "payload": {"order_id": "order-1"}},
            ]
        }

        events = backend_blocked_scan_events_for_item(sync_result, "item-1")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["code"], "01000000000000000002")

    def test_backend_blocked_scan_message_is_warehouse_friendly(self):
        message = format_backend_blocked_scan_message([
            {
                "type": "scan",
                "payload": {"code": "0104006396053978-TEST-BLOCKED"},
                "last_error": "Backend HTTP 409: Code already scanned in another order item",
                "last_error_detail": {
                    "message": "Code already scanned in another order item",
                    "existing_order": {
                        "client": "OOO Busy Client",
                        "order_date_display": "30.05.2026",
                        "product": "Chapman Brown OP 20",
                        "skladbot_request_number": "WH-R-100500",
                    },
                },
            }
        ])

        self.assertIn("КИЗ уже отсканирован в другом заказе", message)
        self.assertIn("OOO Busy Client", message)
        self.assertIn("30.05.2026", message)
        self.assertIn("Chapman Brown OP 20", message)
        self.assertIn("WH-R-100500", message)
        self.assertIn("Сканируйте другой КИЗ", message)
        self.assertNotIn("Backend HTTP", message)

    def test_scan_product_mismatch_message_includes_runtime_diagnostics(self):
        message = format_scan_product_mismatch_message(
            "0104006396054067-TEST-BROWN-SSL",
            "Chapman RED SSL 100`20",
        )

        self.assertIn("КИЗ не соответствует товару текущей позиции", message)
        self.assertIn("Позиция: Chapman RED SSL 100`20", message)
        self.assertIn("Ожидалось: RED SSL", message)
        self.assertIn("КИЗ распознан как: Brown SSL", message)
        self.assertIn("Префикс КИЗа: 0104006396054067", message)
        self.assertIn(main_module.APP_VERSION, message)

    def test_backend_wrong_sku_message_uses_backend_detail(self):
        message = format_backend_blocked_scan_message([
            {
                "type": "scan",
                "payload": {"code": "0104006396054067-TEST-BROWN-SSL"},
                "last_error": "Backend HTTP 409: Scan product does not match order item",
                "last_error_detail": {
                    "message": "Scan product does not match order item",
                    "product": "Chapman RED SSL 100`20",
                    "expected_product_key": "red:ssl",
                    "scan_product_key": "brown:ssl",
                },
            }
        ])

        self.assertIn("Ожидалось: RED SSL", message)
        self.assertIn("КИЗ распознан как: Brown SSL", message)
        self.assertNotIn("Backend HTTP", message)

    def test_local_duplicate_scan_message_uses_current_loaded_order_context(self):
        code = "0104006396053978-TEST-LOCAL"
        owner = find_code_owner_in_orders(code, [
            {
                "Клиент": "OOO Local Client",
                "Дата отгрузки": "31.05.2026",
                "Товары": "Chapman Brown OP 20",
                SKLADBOT_REQUEST_NUMBER_COLUMN: "WH-R-101",
                "_existing_scanned_codes": [code],
            }
        ])

        message = format_duplicate_scan_message(code, owner)

        self.assertIn("OOO Local Client", message)
        self.assertIn("31.05.2026", message)
        self.assertIn("Chapman Brown OP 20", message)
        self.assertIn("WH-R-101", message)

    def test_duplicate_scan_message_explains_unknown_owner_backend_busy(self):
        message = format_duplicate_scan_message(
            "0104006396053978-TEST-BUSY",
            {},
            {
                "checked": True,
                "available": False,
                "latest_movement_type": "outbound",
                "reason": "latest movement is outbound",
            },
        )

        self.assertIn("Владелец в локальном списке не найден", message)
        self.assertIn("Backend не разрешил повтор", message)
        self.assertIn("outbound", message)
        self.assertIn("Сканируйте другой КИЗ", message)

    def test_duplicate_scan_message_explains_backend_reusable_after_return(self):
        message = format_duplicate_scan_message(
            "0104006396053978-TEST-RETURNED",
            {},
            {
                "checked": True,
                "available": True,
                "latest_movement_type": "return",
                "reason": "latest movement is return",
            },
        )

        self.assertIn("Backend разрешил повтор", message)
        self.assertIn("return", message)

    def test_backend_sync_group_blocker_only_blocks_current_group_events(self):
        sync_result = {
            "failed": 1,
            "remaining": 1,
            "blocked_events": [{
                "type": "scan",
                "payload": {"order_item_id": "old-item"},
                "last_error": "Scan product does not match order item",
            }],
        }
        pending_events = [{
            "type": "scan",
            "payload": {"order_item_id": "old-item"},
            "last_error": "Scan product does not match order item",
        }]

        self.assertEqual(
            backend_sync_group_blocker(sync_result, {"current-item"}, {"current-order"}, pending_events),
            "",
        )
        self.assertIn(
            "does not match",
            backend_sync_group_blocker(sync_result, {"old-item"}, set(), pending_events),
        )

    def test_first_incomplete_order_index_skips_completed_backend_positions(self):
        orders = [
            {
                "Кол-во блок": 2,
                "_existing_scanned_codes": ["01000000000000000001", "01000000000000000002"],
            },
            {
                "Кол-во блок": 3,
                "_existing_scanned_codes": ["01000000000000000003"],
            },
        ]

        self.assertEqual(first_incomplete_order_index(orders), 1)

    def test_select_legal_entity_starts_from_first_incomplete_position(self):
        source = inspect.getsource(ScanningApp.select_legal_entity)

        self.assertIn("first_incomplete_order_index", source)

    def test_desktop_errors_use_non_blocking_status_notice(self):
        source_root = Path(__file__).resolve().parents[1] / "src" / "taksklad"
        forbidden = ("messagebox.showerror", "messagebox.showwarning", "messagebox.showinfo")
        hits = []
        for path in source_root.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for marker in forbidden:
                if marker in text:
                    hits.append(f"{path.name}: {marker}")

        self.assertEqual([], hits)
        self.assertEqual(main_module.STATUS_NOTICE_TIMEOUT_MS, 5000)
        self.assertNotIn("messagebox", inspect.getsource(ScanningApp.show_error))
        self.assertIn("STATUS_NOTICE_TIMEOUT_MS", inspect.getsource(ScanningApp.show_status_notice))

    def test_show_error_also_updates_visible_scan_message(self):
        class FakeVar:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class FakeLabel:
            def __init__(self):
                self.options = {}

            def config(self, **kwargs):
                self.options.update(kwargs)

        class FakeToast:
            def __init__(self):
                self.text = ""
                self.packed = False

            def set_text(self, value):
                self.text = value

            def pack(self, **_kwargs):
                self.packed = True

        fake = SimpleNamespace(
            status_var=FakeVar(),
            status_label=FakeLabel(),
            last_code_label=FakeLabel(),
            error_toast=FakeToast(),
            toast_visible=False,
            error_timer=None,
            safe_config=lambda widget, **kwargs: widget.config(**kwargs),
            after=lambda _timeout, _callback: "timer-1",
            after_cancel=lambda _timer: None,
            clear_error=lambda: None,
        )
        fake.show_status_notice = ScanningApp.show_status_notice.__get__(fake)
        fake.show_error_toast = ScanningApp.show_error_toast.__get__(fake)

        full_message = "КИЗ уже отсканирован в другом заказе.\nЗаказ: OOO Busy Client\nДата отгрузки: 30.05.2026"
        ScanningApp.show_error(fake, full_message)

        self.assertIn("OOO Busy Client", fake.status_var.value)
        self.assertIn("OOO Busy Client", fake.last_code_label.options["text"])
        self.assertEqual(fake.last_code_label.options["fg"], ERROR_FG)
        self.assertEqual(fake.error_toast.text, full_message)
        self.assertTrue(fake.error_toast.packed)
        self.assertTrue(fake.toast_visible)

    def test_warning_and_info_use_non_blocking_status_notice(self):
        class FakeVar:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class FakeLabel:
            def __init__(self):
                self.options = {}

            def config(self, **kwargs):
                self.options.update(kwargs)

        fake = SimpleNamespace(
            status_var=FakeVar(),
            status_label=FakeLabel(),
            error_timer=None,
            safe_config=lambda widget, **kwargs: widget.config(**kwargs),
            after=lambda _timeout, _callback: "timer-1",
            after_cancel=lambda _timer: None,
            clear_error=lambda: None,
        )
        fake.show_status_notice = ScanningApp.show_status_notice.__get__(fake)

        ScanningApp.show_warning(fake, "Проверьте очередь")
        self.assertIn("Проверьте очередь", fake.status_var.value)
        self.assertEqual(fake.status_label.options["bg"], WARNING)
        self.assertEqual(fake.status_label.options["fg"], FG_TEXT)

        ScanningApp.show_info(fake, "Готово")
        self.assertIn("Готово", fake.status_var.value)
        self.assertEqual(fake.status_label.options["bg"], BG_MAIN)
        self.assertEqual(fake.status_label.options["fg"], FG_MUTED)

    def test_critical_errors_send_alert_without_operational_documents(self):
        fake = SimpleNamespace(
            show_error=mock.Mock(),
            send_telegram_alert_async=mock.Mock(),
            send_telegram_documents_async=mock.Mock(),
        )

        ScanningApp.show_critical_error(fake, "Не удалось завершить заказ", RuntimeError("backend down"))

        fake.show_error.assert_called_once()
        fake.send_telegram_alert_async.assert_called_once()
        fake.send_telegram_documents_async.assert_not_called()
        self.assertIn("backend down", fake.send_telegram_alert_async.call_args.args[0])

        fake.show_error.reset_mock()
        fake.send_telegram_alert_async.reset_mock()
        ScanningApp.report_callback_exception(fake, RuntimeError, RuntimeError("ui down"), None)

        fake.show_error.assert_called_once()
        fake.send_telegram_alert_async.assert_called_once()
        fake.send_telegram_documents_async.assert_not_called()
        self.assertIn("ui down", fake.send_telegram_alert_async.call_args.args[0])

        self.assertNotIn("collect_operational_documents", inspect.getsource(app_runtime.AppRuntimeMixin.show_critical_error))
        self.assertNotIn("collect_operational_documents", inspect.getsource(app_runtime.AppRuntimeMixin.report_callback_exception))

    def test_backend_blocked_scan_removes_code_and_keeps_position_open(self):
        class FakeVar:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class FakeWidget:
            def __init__(self):
                self.options = {}
                self.focused = False

            def config(self, **kwargs):
                self.options.update(kwargs)

            def focus_set(self):
                self.focused = True

        good_code = "0104006396053978217GOOD"
        blocked_code = "0104006396053978217BAD"
        fake = SimpleNamespace(
            current_order={"Кол-во блок": 2, "_backend_order_item_id": "item-1"},
            scanned_codes=[good_code, blocked_code],
            all_existing_codes={good_code, blocked_code},
            progress_label=FakeWidget(),
            next_product_btn=FakeWidget(),
            finish_btn=FakeWidget(),
            last_code_label=FakeWidget(),
            status_var=FakeVar(),
            status_label=FakeWidget(),
            scan_entry=FakeWidget(),
            error_timer=None,
            safe_config=lambda widget, **kwargs: widget.config(**kwargs),
            after=lambda _timeout, _callback: "timer-1",
            after_cancel=lambda _timer: None,
            clear_error=lambda: None,
            update_stats_display=lambda: None,
        )
        fake.show_status_notice = ScanningApp.show_status_notice.__get__(fake)
        fake.show_error = ScanningApp.show_error.__get__(fake)

        with mock.patch("taksklad.app_scanning.write_scan_backup", return_value=True) as write_backup:
            applied = ScanningApp.apply_backend_blocked_scan_events(
                fake,
                [{
                    "type": "scan",
                    "payload": {"order_item_id": "item-1", "code": blocked_code},
                    "last_error": "Backend HTTP 409: Code already scanned in another order item",
                }],
            )

        self.assertTrue(applied)
        self.assertEqual(fake.scanned_codes, [good_code])
        self.assertEqual(fake.progress_label.options["text"], "1 / 2")
        self.assertEqual(fake.next_product_btn.options["state"], "disabled")
        self.assertEqual(fake.finish_btn.options["state"], "disabled")
        self.assertIn("Сканируйте другой КИЗ", fake.status_var.value)
        self.assertIn(blocked_code, fake.all_existing_codes)
        self.assertTrue(fake.scan_entry.focused)
        write_backup.assert_called_once()

    def test_finish_requires_every_position_saved_and_fully_scanned(self):
        orders = [
            {"Кол-во блок": 2, "Отсканированные коды": "01000000000000000001\n01000000000000000002"},
            {"Кол-во блок": 1, "Отсканированные коды": "01000000000000000003"},
        ]

        self.assertEqual(group_finish_blocker(orders, [{"Товары": "A"}]), "Сначала сохраните все позиции заказа")
        self.assertEqual(
            group_finish_blocker([{**orders[0], "Отсканированные коды": "01000000000000000001"}], [{"Товары": "A"}]),
            "Позиция 1: отсканировано 1 из 2 блоков",
        )
        self.assertEqual(group_finish_blocker(orders, [{"Товары": "A"}, {"Товары": "B"}]), "")

    def test_undo_saved_code_updates_active_row_and_keeps_finish_disabled_when_incomplete(self):
        source = inspect.getsource(ScanningApp.undo_last_scan)

        self.assertIn("allow_empty=True", source)
        self.assertNotIn("Нельзя отменить коды, уже записанные в Google Sheets", source)
        self.assertIn("self.finish_btn.config(state=\"disabled\")", source)

    def test_undo_saved_pending_save_keeps_state_consistent_without_google_or_backend(self):
        class FakeVar:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value

        class FakeWidget:
            def __init__(self):
                self.options = {}
                self.focused = False

            def config(self, **kwargs):
                self.options.update(kwargs)

            def focus_set(self):
                self.focused = True

        first_code = "01040063960540670001"
        removed_code = "01040063960540670002"
        order = {
            "Кол-во блок": 2,
            "Товары": "Chapman Brown SSL",
            "Отсканированные коды": f"{first_code}\n{removed_code}",
        }
        fake = SimpleNamespace(
            ensure_update_allowed=lambda: True,
            operation_in_progress=False,
            current_order=order,
            scanned_codes=[first_code, removed_code],
            saved_codes_count=2,
            sheet=None,
            current_product_idx=0,
            current_legal_entity_orders=[order],
            all_existing_codes={first_code, removed_code},
            progress_label=FakeWidget(),
            next_product_btn=FakeWidget(),
            finish_btn=FakeWidget(),
            last_code_label=FakeWidget(),
            status_var=FakeVar(),
            scan_entry=FakeWidget(),
            show_error=mock.Mock(),
        )

        with (
            mock.patch("taksklad.app_scanning.write_scan_backup", return_value=True) as write_backup,
            mock.patch("taksklad.app_scanning.update_pending_save_codes_for_undo", return_value=True) as update_pending,
            mock.patch("taksklad.app_scanning.order_uses_backend_scan_path", return_value=False),
            mock.patch("taksklad.app_scanning.undo_backend_scan") as undo_backend,
            mock.patch("taksklad.app_scanning.update_scanned_codes_to_gsheet") as update_google,
            mock.patch("taksklad.app_scanning.remove_pending_backend_scan") as remove_pending_backend,
        ):
            ScanningApp.undo_last_scan(fake)

        self.assertEqual(fake.scanned_codes, [first_code])
        self.assertEqual(fake.current_order["Отсканированные коды"], first_code)
        self.assertEqual(fake.current_order["_existing_scanned_codes"], [first_code])
        self.assertEqual(fake.saved_codes_count, 1)
        self.assertEqual(fake.progress_label.options["text"], "1 / 2")
        self.assertEqual(fake.next_product_btn.options["state"], "disabled")
        self.assertEqual(fake.finish_btn.options["state"], "disabled")
        self.assertNotIn(removed_code, fake.all_existing_codes)
        self.assertTrue(fake.scan_entry.focused)
        self.assertIn("(1/2)", fake.status_var.value)
        write_backup.assert_called_once()
        update_pending.assert_called_once()
        undo_backend.assert_not_called()
        update_google.assert_not_called()
        remove_pending_backend.assert_called_once_with(order, removed_code)
        fake.show_error.assert_not_called()

    def test_undo_saved_backend_failure_restores_local_state(self):
        class FakeWidget:
            def __init__(self):
                self.options = {}

            def config(self, **kwargs):
                self.options.update(kwargs)

        first_code = "01040063960540670001"
        removed_code = "01040063960540670002"
        order = {
            "_backend_order_item_id": "item-1",
            "Кол-во блок": 2,
            "Товары": "Chapman Brown SSL",
            "Отсканированные коды": f"{first_code}\n{removed_code}",
        }
        fake = SimpleNamespace(
            ensure_update_allowed=lambda: True,
            operation_in_progress=False,
            current_order=order,
            scanned_codes=[first_code, removed_code],
            saved_codes_count=2,
            sheet=None,
            all_existing_codes={first_code, removed_code},
            progress_label=FakeWidget(),
            next_product_btn=FakeWidget(),
            finish_btn=FakeWidget(),
            last_code_label=FakeWidget(),
            show_error=mock.Mock(),
        )

        with (
            mock.patch("taksklad.app_scanning.write_scan_backup", return_value=True),
            mock.patch("taksklad.app_scanning.update_pending_save_codes_for_undo", return_value=False),
            mock.patch("taksklad.app_scanning.order_uses_backend_scan_path", return_value=True),
            mock.patch("taksklad.app_scanning.undo_backend_scan", side_effect=RuntimeError("backend down")),
            mock.patch("taksklad.app_scanning.update_scanned_codes_to_gsheet") as update_google,
            mock.patch("taksklad.app_scanning.remove_pending_backend_scan") as remove_pending_backend,
        ):
            ScanningApp.undo_last_scan(fake)

        self.assertEqual(fake.scanned_codes, [first_code, removed_code])
        self.assertEqual(fake.saved_codes_count, 2)
        self.assertEqual(fake.current_order["Отсканированные коды"], f"{first_code}\n{removed_code}")
        self.assertIn(removed_code, fake.all_existing_codes)
        self.assertEqual(fake.progress_label.options, {})
        self.assertEqual(fake.finish_btn.options, {})
        fake.show_error.assert_called_once()
        self.assertIn("Не удалось отменить код в VDS", fake.show_error.call_args.args[0])
        update_google.assert_not_called()
        remove_pending_backend.assert_not_called()

    def test_undo_saved_google_failure_restores_local_state(self):
        class FakeWidget:
            def __init__(self):
                self.options = {}

            def config(self, **kwargs):
                self.options.update(kwargs)

        first_code = "01040063960540670001"
        removed_code = "01040063960540670002"
        order = {
            "Кол-во блок": 2,
            "Товары": "Chapman Brown SSL",
            "Отсканированные коды": f"{first_code}\n{removed_code}",
        }
        fake = SimpleNamespace(
            ensure_update_allowed=lambda: True,
            operation_in_progress=False,
            current_order=order,
            scanned_codes=[first_code, removed_code],
            saved_codes_count=2,
            sheet=object(),
            all_existing_codes={first_code, removed_code},
            progress_label=FakeWidget(),
            next_product_btn=FakeWidget(),
            finish_btn=FakeWidget(),
            last_code_label=FakeWidget(),
            show_error=mock.Mock(),
        )

        with (
            mock.patch("taksklad.app_scanning.write_scan_backup", return_value=True),
            mock.patch("taksklad.app_scanning.update_pending_save_codes_for_undo", return_value=False),
            mock.patch("taksklad.app_scanning.order_uses_backend_scan_path", return_value=False),
            mock.patch("taksklad.app_scanning.update_scanned_codes_to_gsheet", return_value=(False, "quota")) as update_google,
            mock.patch("taksklad.app_scanning.undo_backend_scan") as undo_backend,
            mock.patch("taksklad.app_scanning.remove_pending_backend_scan") as remove_pending_backend,
        ):
            ScanningApp.undo_last_scan(fake)

        self.assertEqual(fake.scanned_codes, [first_code, removed_code])
        self.assertEqual(fake.saved_codes_count, 2)
        self.assertEqual(fake.current_order["Отсканированные коды"], f"{first_code}\n{removed_code}")
        self.assertIn(removed_code, fake.all_existing_codes)
        self.assertEqual(fake.progress_label.options, {})
        self.assertEqual(fake.finish_btn.options, {})
        update_google.assert_called_once_with(fake.sheet, order, [first_code], allow_empty=True)
        fake.show_error.assert_called_once()
        self.assertIn("Не удалось отменить код в Google Sheets: quota", fake.show_error.call_args.args[0])
        undo_backend.assert_not_called()
        remove_pending_backend.assert_not_called()

    def test_undo_terminal_state_guard_does_not_block_completed_active_position(self):
        self.assertTrue(is_terminal_scan_state({"Статус": "Архив без КИЗ"}))
        self.assertTrue(is_terminal_scan_state({"Статус": "Возврат"}))
        self.assertFalse(is_terminal_scan_state({"Статус": "Выполнено"}))

    def test_finish_prints_before_backend_complete_and_skips_direct_google_archive_in_backend_mode(self):
        source = inspect.getsource(ScanningApp.finish_legal_entity)
        print_call = "print_summary(address, summary_products, print_settings=selected_print_settings)"

        self.assertLess(source.index(print_call), source.index("complete_backend_orders_or_raise"))
        self.assertIn("Заказ не завершён в backend", source)
        self.assertIn("not (backend_order_ids and backend_enabled())", source)
        self.assertIn("self.sheet and not uses_backend_finish", source)
        self.assertNotIn(
            "for order in current_orders:\n"
            "                    queue_backend_scans_for_order(order)\n"
            "                backend_sync_result = sync_pending_backend_events()",
            source,
        )

    def test_finish_requires_confirmed_pending_print_queue_updates(self):
        source = inspect.getsource(ScanningApp.finish_legal_entity)
        print_call = "print_summary(address, summary_products, print_settings=selected_print_settings)"

        self.assertIn("if not pending_print_id", source)
        self.assertIn("if not remove_pending_print(pending_print_id)", source)
        self.assertLess(source.index("if not pending_print_id"), source.index(print_call))
        self.assertLess(source.index("if not remove_pending_print(pending_print_id)"), source.index("complete_backend_orders_or_raise"))
        self.assertLess(source.index("if not remove_pending_print(pending_print_id)"), source.index("archive_order_group_to_gsheet"))

    def test_backend_complete_accepts_already_completed_order_for_repeat_print(self):
        completed = []

        def fake_complete(order_id):
            if order_id == "order-done":
                raise backend_client.BackendApiError(
                    "Backend HTTP 409: Order already completed",
                    status_code=409,
                    detail={"message": "Order already completed"},
                )
            completed.append(order_id)

        with (
            mock.patch("taksklad.backend_flow.backend_configured", return_value=True),
            mock.patch("taksklad.backend_flow.complete_order", side_effect=fake_complete),
        ):
            result = complete_backend_orders_or_raise(["order-new", "order-done"])

        self.assertEqual(completed, ["order-new"])
        self.assertEqual(result, {"completed": 1, "already_completed": 1})

    def test_return_mark_sends_confirmed_items_to_backend(self):
        fake_app = SimpleNamespace(telegram_lock_owner_label="warehouse-pc")
        confirmed_items = [{
            "item_id": "item-1",
            "product": "Chapman RED OP 20",
            "sku": "Chapman RED OP 20",
            "quantity_blocks": 2,
            "quantity_pieces": 20,
        }]
        with (
            mock.patch("taksklad.app_returns.backend_read_orders_enabled", return_value=True),
            mock.patch("taksklad.app_returns.mark_order_returned", return_value={"status": "returned"}) as mark_returned,
        ):
            result = ScanningApp.mark_return_for_display(
                fake_app,
                {"id": "order-1", "items": []},
                "WH-R-100",
                confirmed_items=confirmed_items,
            )

        self.assertEqual(result["status"], "returned")
        mark_returned.assert_called_once_with(
            "order-1",
            return_reference="WH-R-100",
            returned_by="warehouse-pc",
            confirmed_items=confirmed_items,
        )

    def test_backend_returns_list_reads_backend_without_google_fallback(self):
        fake_app = SimpleNamespace()

        with (
            mock.patch("taksklad.app_returns.backend_read_orders_enabled", return_value=True),
            mock.patch("taksklad.app_returns.fetch_returned_orders", return_value=[{"id": "order-1"}]) as fetch_backend,
            mock.patch("taksklad.app_returns.fetch_returned_orders_from_gsheet") as fetch_google,
        ):
            result = ScanningApp.fetch_returns_for_display(fake_app, limit=25)

        self.assertEqual(result, [{"id": "order-1"}])
        fetch_backend.assert_called_once_with(limit=25)
        fetch_google.assert_not_called()

    def test_backend_return_lookup_reads_backend_without_google_fallback(self):
        fake_app = SimpleNamespace()

        with (
            mock.patch("taksklad.app_returns.backend_read_orders_enabled", return_value=True),
            mock.patch("taksklad.app_returns.lookup_return_order", return_value={"id": "order-1"}) as lookup_backend,
            mock.patch("taksklad.app_returns.lookup_return_order_in_gsheet") as lookup_google,
        ):
            result = ScanningApp.lookup_return_for_display(fake_app, "WH-R-100")

        self.assertEqual(result, {"id": "order-1"})
        lookup_backend.assert_called_once_with("WH-R-100")
        lookup_google.assert_not_called()

    def test_backend_return_rejects_google_order_without_backend_id(self):
        fake_app = SimpleNamespace(telegram_lock_owner_label="warehouse-pc")
        google_order = {
            "source": "google_sheets",
            "_row_numbers": [42],
            "id": "GS-100",
        }

        with (
            mock.patch("taksklad.app_returns.backend_read_orders_enabled", return_value=True),
            mock.patch("taksklad.app_returns.mark_return_order_in_gsheet") as mark_gsheet_return,
            mock.patch("taksklad.app_returns.mark_order_returned") as mark_backend_return,
        ):
            with self.assertRaisesRegex(RuntimeError, "backend/order id"):
                ScanningApp.mark_return_for_display(
                    fake_app,
                    google_order,
                    "WH-R-101",
                    confirmed_items=[{"item_id": "item-1"}],
                )

        mark_gsheet_return.assert_not_called()
        mark_backend_return.assert_not_called()

    def test_backend_return_uses_backend_id_for_google_order(self):
        fake_app = SimpleNamespace(telegram_lock_owner_label="warehouse-pc")
        confirmed_items = [{
            "item_id": "item-1",
            "product": "Chapman RED OP 20",
            "sku": "Chapman RED OP 20",
            "quantity_blocks": 1,
            "quantity_pieces": 20,
        }]
        google_order = {
            "source": "google_sheets",
            "_row_numbers": [42],
            "_backend_order_id": "order-1",
            "id": "GS-100",
        }

        with (
            mock.patch("taksklad.app_returns.backend_read_orders_enabled", return_value=True),
            mock.patch("taksklad.app_returns.mark_return_order_in_gsheet") as mark_gsheet_return,
            mock.patch("taksklad.app_returns.mark_order_returned", return_value={"status": "returned"}) as mark_backend_return,
        ):
            result = ScanningApp.mark_return_for_display(
                fake_app,
                google_order,
                "WH-R-101",
                confirmed_items=confirmed_items,
            )

        self.assertEqual(result["status"], "returned")
        mark_backend_return.assert_called_once_with(
            "order-1",
            return_reference="WH-R-101",
            returned_by="warehouse-pc",
            confirmed_items=confirmed_items,
        )
        mark_gsheet_return.assert_not_called()

    def test_legacy_return_keeps_google_write_fallback_for_google_order(self):
        fake_app = SimpleNamespace(telegram_lock_owner_label="warehouse-pc")
        google_order = {
            "source": "google_sheets",
            "_row_numbers": [42],
            "id": "GS-100",
        }
        updated_order = {**google_order, "status": "returned"}

        with (
            mock.patch("taksklad.app_returns.backend_read_orders_enabled", return_value=False),
            mock.patch("taksklad.app_returns.mark_return_order_in_gsheet", return_value=updated_order) as mark_gsheet_return,
            mock.patch("taksklad.app_returns.mark_order_returned") as mark_backend_return,
        ):
            result = ScanningApp.mark_return_for_display(fake_app, google_order, "WH-R-101")

        self.assertEqual(result, updated_order)
        mark_gsheet_return.assert_called_once_with(
            google_order,
            return_reference="WH-R-101",
            returned_by="warehouse-pc",
        )
        mark_backend_return.assert_not_called()

    def test_legacy_google_return_rejects_partial_selection_before_full_order_write(self):
        fake_app = SimpleNamespace(telegram_lock_owner_label="warehouse-pc")
        google_order = {
            "source": "google_sheets",
            "_row_numbers": [42, 43],
            "id": "GS-100",
            "items": [
                {"Товары": "Chapman RED OP 20", "Кол-во блок": 2, "Кол-во ШТ": 20},
                {"Товары": "Chapman Brown SSL 20", "Кол-во блок": 1, "Кол-во ШТ": 10},
            ],
        }
        partial_items = ScanningApp.build_return_confirmed_items(
            fake_app,
            google_order,
            selected_item_ids={"google_row:1"},
        )

        with (
            mock.patch("taksklad.app_returns.backend_read_orders_enabled", return_value=False),
            mock.patch("taksklad.app_returns.mark_return_order_in_gsheet") as mark_gsheet_return,
            mock.patch("taksklad.app_returns.mark_order_returned") as mark_backend_return,
        ):
            with self.assertRaisesRegex(RuntimeError, "Частичный возврат пока не поддержан"):
                ScanningApp.mark_return_for_display(
                    fake_app,
                    google_order,
                    "WH-R-101",
                    confirmed_items=partial_items,
                )

        mark_gsheet_return.assert_not_called()
        mark_backend_return.assert_not_called()

    def test_legacy_google_return_accepts_explicit_full_selection(self):
        fake_app = SimpleNamespace(telegram_lock_owner_label="warehouse-pc")
        google_order = {
            "source": "google_sheets",
            "_row_numbers": [42, 43],
            "id": "GS-100",
            "items": [
                {"Товары": "Chapman RED OP 20", "Кол-во блок": 2, "Кол-во ШТ": 20},
                {"Товары": "Chapman Brown SSL 20", "Кол-во блок": 1, "Кол-во ШТ": 10},
            ],
        }
        full_items = ScanningApp.build_return_confirmed_items(fake_app, google_order)
        updated_order = {**google_order, "status": "returned"}

        with (
            mock.patch("taksklad.app_returns.backend_read_orders_enabled", return_value=False),
            mock.patch("taksklad.app_returns.mark_return_order_in_gsheet", return_value=updated_order) as mark_gsheet_return,
            mock.patch("taksklad.app_returns.mark_order_returned") as mark_backend_return,
        ):
            result = ScanningApp.mark_return_for_display(
                fake_app,
                google_order,
                "WH-R-101",
                confirmed_items=full_items,
            )

        self.assertEqual(result, updated_order)
        mark_gsheet_return.assert_called_once_with(
            google_order,
            return_reference="WH-R-101",
            returned_by="warehouse-pc",
        )
        mark_backend_return.assert_not_called()

    def test_return_confirmed_items_are_built_from_backend_items(self):
        order = {
            "items": [
                {
                    "id": "item-1",
                    "product": "Chapman RED OP 20",
                    "quantity_blocks": 2,
                    "quantity_pieces": 20,
                }
            ]
        }

        confirmed = ScanningApp.build_return_confirmed_items(SimpleNamespace(), order)

        self.assertEqual(confirmed, [{
            "item_id": "item-1",
            "product": "Chapman RED OP 20",
            "sku": "Chapman RED OP 20",
            "quantity_blocks": 2,
            "quantity_pieces": 20,
        }])

    def test_return_confirmed_items_can_build_selected_subset_with_allowed_fields_only(self):
        order = {
            "items": [
                {
                    "id": "item-1",
                    "product": "Chapman RED OP 20",
                    "quantity_blocks": 2,
                    "quantity_pieces": 20,
                    "address": "must not leak",
                    "raw_payload": {"secret": "must not leak"},
                },
                {
                    "id": "item-2",
                    "product": "Chapman Brown SSL 20",
                    "quantity_blocks": 1,
                    "quantity_pieces": 10,
                    "last_error_detail": "must not leak",
                },
            ]
        }

        confirmed = ScanningApp.build_return_confirmed_items(
            SimpleNamespace(),
            order,
            selected_item_ids={"item-2"},
        )

        self.assertEqual(confirmed, [{
            "item_id": "item-2",
            "product": "Chapman Brown SSL 20",
            "sku": "Chapman Brown SSL 20",
            "quantity_blocks": 1,
            "quantity_pieces": 10,
        }])
        self.assertEqual(set(confirmed[0]), set(app_returns.RETURN_CONFIRMED_ITEM_KEYS))

    def test_return_mark_sanitizes_confirmed_items_before_backend(self):
        fake_app = SimpleNamespace(telegram_lock_owner_label="warehouse-pc")
        order = {
            "id": "order-1",
            "items": [
                {
                    "id": "item-1",
                    "product": "Chapman RED OP 20",
                    "quantity_blocks": 2,
                    "quantity_pieces": 20,
                }
            ],
        }
        dirty_items = [{
            "item_id": "item-1",
            "product": "Chapman RED OP 20",
            "sku": "Chapman RED OP 20",
            "quantity_blocks": 2,
            "quantity_pieces": 20,
            "raw_payload": {"must": "not leak"},
            "address": "must not leak",
        }]
        expected_items = [{
            "item_id": "item-1",
            "product": "Chapman RED OP 20",
            "sku": "Chapman RED OP 20",
            "quantity_blocks": 2,
            "quantity_pieces": 20,
        }]

        with (
            mock.patch("taksklad.app_returns.backend_read_orders_enabled", return_value=True),
            mock.patch("taksklad.app_returns.mark_order_returned", return_value={"status": "returned"}) as mark_returned,
        ):
            result = ScanningApp.mark_return_for_display(
                fake_app,
                order,
                "WH-R-100",
                confirmed_items=dirty_items,
            )

        self.assertEqual(result["status"], "returned")
        mark_returned.assert_called_once_with(
            "order-1",
            return_reference="WH-R-100",
            returned_by="warehouse-pc",
            confirmed_items=expected_items,
        )

    def test_return_mark_rejects_empty_confirmed_items_before_backend(self):
        fake_app = SimpleNamespace(telegram_lock_owner_label="warehouse-pc")
        order = {
            "id": "order-1",
            "items": [
                {
                    "id": "item-1",
                    "product": "Chapman RED OP 20",
                    "quantity_blocks": 2,
                    "quantity_pieces": 20,
                }
            ],
        }

        with (
            mock.patch("taksklad.app_returns.backend_read_orders_enabled", return_value=True),
            mock.patch("taksklad.app_returns.mark_order_returned") as mark_returned,
        ):
            with self.assertRaisesRegex(RuntimeError, "выберите хотя бы одну позицию"):
                ScanningApp.mark_return_for_display(
                    fake_app,
                    order,
                    "WH-R-100",
                    confirmed_items=[],
                )

        mark_returned.assert_not_called()

    def test_return_order_already_returned_detects_status_context(self):
        self.assertTrue(app_returns.return_order_already_returned({"status": "returned"}))
        self.assertTrue(app_returns.return_order_already_returned({"return_status": "returned"}))
        self.assertFalse(app_returns.return_order_already_returned({"status": "completed"}))

    def test_return_totals_support_backend_and_google_item_shapes(self):
        order = {
            "items": [
                {
                    "product": "Chapman RED OP 20",
                    "quantity_blocks": 2,
                    "line_total": 480000,
                },
                {
                    "Товары": "Chapman Brown SSL 20",
                    "Кол-во блок": "3",
                    "Сумма": "720 000",
                },
            ]
        }

        self.assertEqual(app_returns.return_order_total_blocks(order), 5)
        self.assertEqual(app_returns.return_order_total_price(order), 1_200_000)

    def test_print_failure_after_backend_complete_keeps_repeat_print_message(self):
        message = format_print_failure_after_backend_complete(
            RuntimeError("printer offline"),
            {"completed": 1, "already_completed": 0},
        )

        self.assertIn("Backend уже завершил заказ", message)
        self.assertIn("Повторите печать", message)
        self.assertIn("printer offline", message)

    def test_selected_order_shows_party_summary(self):
        select_source = inspect.getsource(ScanningApp.select_legal_entity)
        summary_source = inspect.getsource(ScanningApp.update_party_summary_display)
        reset_source = inspect.getsource(ScanningApp.reset_current_selection)

        self.assertIn("self.update_party_summary_display()", select_source)
        self.assertIn("Партия:", summary_source)
        self.assertIn("Дата отгрузки:", summary_source)
        self.assertIn("Заявка:", summary_source)
        self.assertIn("format_money(total_sum)", summary_source)
        self.assertIn("Партия не выбрана", reset_source)

    def test_taksklad_palette_and_rounded_buttons_are_locked(self):
        self.assertEqual(WARNING.lower(), "#d8b64c")
        self.assertEqual(ACCENT.lower(), "#b28224")
        self.assertEqual(FG_TEXT.lower(), "#2e2c28")
        self.assertEqual(BG_MAIN.lower(), "#f4f1e8")

        signature = inspect.signature(AppButton.__init__)
        self.assertIn("radius", signature.parameters)
        self.assertGreaterEqual(signature.parameters["radius"].default, 16)

        build_source = inspect.getsource(ScanningApp._build_ui)
        self.assertIn("bg=WARNING, fg=FG_TEXT", build_source)
        self.assertIn("radius=8", build_source)

        widget_source = inspect.getsource(AppButton)
        self.assertIn("fade_hex", widget_source)
        self.assertLess(int(ACCENT[1:3], 16), int(fade_hex(ACCENT)[1:3], 16))

    def test_error_toast_is_visually_separated_from_version_label(self):
        show_toast_source = inspect.getsource(ScanningApp.show_error_toast)
        build_source = inspect.getsource(ScanningApp._build_ui)

        self.assertIn("before=self.status_label", show_toast_source)
        self.assertIn("pady=(0, 12)", show_toast_source)
        self.assertIn("version_frame.pack(fill=\"x\", pady=(10, 0))", build_source)

    def test_backend_status_is_visible_on_warehouse_screen(self):
        build_source = inspect.getsource(ScanningApp._build_ui)
        stats_source = inspect.getsource(app_day_end.DayEndActionsMixin.update_stats_display)

        self.assertIn("self.backend_status_label", build_source)
        self.assertIn("make_stat_tile", build_source)
        self.assertIn("self.sync_caption_label", build_source)
        self.assertIn("font=KPI_FONT", build_source)
        self.assertNotIn("Выполнено: 0", build_source)
        self.assertNotIn("Backend: ожидает проверки", build_source)
        self.assertIn("build_backend_status", stats_source)
        self.assertIn("load_pending_prints", stats_source)
        self.assertIn("load_pending_telegram", stats_source)
        self.assertIn("pending_saves + pending_prints + pending_telegram + pending_backend", stats_source)
        self.assertIn('self.pending_saves_label.config(text="OK"', stats_source)
        self.assertIn('sync_caption.config(text="Синхронизация")', stats_source)
        self.assertIn("self.sync_queue_btn", build_source)
        self.assertIn("command=self.show_sync_queue_window", build_source)

    def test_backend_status_text_covers_disabled_online_pending_and_error(self):
        with mock.patch.object(app_day_end, "backend_enabled", return_value=False):
            text, _ = build_backend_status()
        self.assertEqual(text, "")

        with (
            mock.patch.object(app_day_end, "backend_enabled", return_value=True),
            mock.patch.object(app_day_end, "backend_configured", return_value=False),
        ):
            text, _ = build_backend_status()
        self.assertEqual(text, "Синхронизация: сервер не настроен")

        with (
            mock.patch.object(app_day_end, "backend_enabled", return_value=True),
            mock.patch.object(app_day_end, "backend_configured", return_value=True),
        ):
            text, _ = build_backend_status({"backend": {"enabled": True}})
        self.assertEqual(text, "")

        with (
            mock.patch.object(app_day_end, "backend_enabled", return_value=True),
            mock.patch.object(app_day_end, "backend_configured", return_value=True),
        ):
            text, color = build_backend_status({"backend": {"enabled": True, "remaining": 1}}, pending_backend=2)
        self.assertEqual(text, "Синхронизация: ожидает отправки")
        self.assertEqual(color, FG_MUTED)

        with (
            mock.patch.object(app_day_end, "backend_enabled", return_value=True),
            mock.patch.object(app_day_end, "backend_configured", return_value=True),
        ):
            text, color = build_backend_status({"backend": {"enabled": True, "failed": 1, "remaining": 3}})
        self.assertEqual(text, "Синхронизация: ожидает повторной отправки")
        self.assertEqual(color, FG_MUTED)

        with (
            mock.patch.object(app_day_end, "backend_enabled", return_value=True),
            mock.patch.object(app_day_end, "backend_configured", return_value=True),
        ):
            text, color = build_backend_status({"backend": {"enabled": True, "failed": 1, "remaining": 0}})
        self.assertEqual(text, "Синхронизация: нужна проверка")
        self.assertEqual(color, ERROR_FG)

        with (
            mock.patch.object(app_day_end, "backend_enabled", return_value=True),
            mock.patch.object(app_day_end, "backend_configured", return_value=True),
        ):
            text, _ = build_backend_status({"backend": {"enabled": True, "blocked": 1}})
        self.assertEqual(text, "Синхронизация: заказ недосканирован")


if __name__ == "__main__":
    unittest.main()
