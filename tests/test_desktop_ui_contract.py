import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from taksklad import app_day_end, app_returns, app_runtime
from taksklad import main as main_module
from taksklad.config import ACCENT, BG_MAIN, DANGER, ERROR_FG, FG_MUTED, FG_TEXT, WARNING
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
from taksklad.startup_check import format_app_version_label
from taksklad.ui_widgets import AppButton, fade_hex


class DesktopUiContractTests(unittest.TestCase):
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
        self.assertIn("format_app_version_label", source)
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
            "for completed in self.completed_orders:",
        ]

        for guard in duplicate_guards:
            self.assertIn(guard, source)
            self.assertLess(source.index(guard), backup_idx)
            self.assertLess(source.index(guard), backend_queue_idx)

        self.assertIn("format_duplicate_scan_message", source)
        self.assertIn("log_duplicate_code_async", source)

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
                "payload": {"code": "0104006396053978217abcdef"},
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
            "01040063960540672171Zs<C,939y-AKO2z0",
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
                "payload": {"code": "01040063960540672171Zs<C,939y-AKO2z0"},
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
        code = "0104006396053978217abcdef"
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
        confirmed_items = [{"item_id": "item-1", "quantity_blocks": 1}]
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
        self.assertIn('self.pending_saves_label.config(text="OK"', stats_source)
        self.assertIn('sync_caption.config(text="Синхронизация")', stats_source)

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
