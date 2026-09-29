"""Завершение заказа: экран свободен сразу после печати сводного листа, завершение на сервере идёт в фоне"""

import unittest
from types import SimpleNamespace
from unittest import mock

from taksklad.app_data_loading import DataLoadingMixin
from taksklad.config import BG_MAIN, FG_MUTED, SKLADBOT_REQUEST_NUMBER_COLUMN
from taksklad.main import ScanningApp
from taksklad.orders import order_group_key


BUSY_TEXT = "⏳ Печатаю сводный лист и завершаю заказ..."
PRINTED_TEXT = "✅ Сводный лист напечатан, завершаю заказ в фоне"
COMPLETED_TEXT = "✅ Заказ завершён! Сводка отправлена на печать"
CRITICAL_TITLE = "Не удалось завершить заказ"


class FakeWidget:
    def __init__(self):
        self.options = {}
        self.history = []

    def config(self, **kwargs):
        self.options.update(kwargs)
        self.history.append(kwargs)


class FakeCardList:
    def __init__(self):
        self.calls = []

    def set_rows(self, rows, selected_key=None):
        self.calls.append({"rows": rows, "selected_key": selected_key})


def make_group(request, client="ООО Тест", address="Адрес 1", positions=2, first_row_number=None):
    orders = []
    for index in range(positions):
        order = {
            SKLADBOT_REQUEST_NUMBER_COLUMN: request,
            "Клиент": client,
            "Тип оплаты": "Перечисление",
            "Адрес": address,
            "Товары": "Chapman Brown SSL",
            "Кол-во блок": 1,
            "_backend_order_id": f"order-{request}",
            "_backend_order_item_id": f"item-{request}-{index + 1}",
        }
        if first_row_number is not None:
            order["_row_number"] = first_row_number + index
        orders.append(order)
    return orders


def run_entry(entry, error=None):
    """Повторяет run_background: work, затем on_success или on_error, затем on_finally"""
    try:
        if error is not None:
            raise error
        result = entry["work"]()
    except Exception as exc:
        try:
            if entry["on_error"]:
                entry["on_error"](exc)
            else:
                raise
        finally:
            if entry["on_finally"]:
                entry["on_finally"]()
        return
    try:
        if entry["on_success"]:
            entry["on_success"](result)
    finally:
        if entry["on_finally"]:
            entry["on_finally"]()


class FinishBackgroundTestCase(unittest.TestCase):
    def setUp(self):
        self.mocks = {}
        defaults = {
            "group_finish_blocker": "",
            "add_pending_print": "print-1",
            "print_summary": ["file.pdf"],
            "remove_pending_print": True,
            "sync_pending_backend_events": {"synced": 2, "failed": 0, "remaining": 0, "blocked_events": []},
            "load_pending_backend_events": [],
            "backend_sync_group_blocker": "",
            "complete_backend_orders_or_raise": None,
            "write_scan_backup": True,
        }
        for name, value in defaults.items():
            patcher = mock.patch(f"taksklad.app_finish.{name}", return_value=value)
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def make_app(self, group=None, other=None, auto=False, with_row_numbers=False):
        group = group if group is not None else make_group("SB-1", first_row_number=1 if with_row_numbers else None)
        other = other if other is not None else make_group(
            "SB-2", client="ООО Другое", address="Адрес 2",
            first_row_number=3 if with_row_numbers else None,
        )
        calls = []

        class FakeVar:
            def __init__(self):
                self.value = ""

            def set(self, value):
                self.value = value
                calls.append(("status", value))

        app = SimpleNamespace(
            calls=calls,
            background=[],
            group=group,
            other=other,
            today_orders=list(group) + list(other),
            operation_in_progress=False,
            operation_message="",
            current_legal_entity="ООО Тест",
            current_group_key=order_group_key(group[0]),
            current_product_idx=len(group),
            current_legal_entity_orders=group,
            current_legal_entity_products=[
                {"Адрес": "Адрес 1", "Коды": [f"code-{number}"]} for number in range(len(group))
            ],
            current_order=None,
            scanned_codes=[],
            saved_codes_count=0,
            finish_btn=FakeWidget(),
            next_product_btn=FakeWidget(),
            status_var=FakeVar(),
            status_label=FakeWidget(),
            order_card_list=FakeCardList(),
            ensure_update_allowed=lambda: True,
            confirm_print_settings=lambda: True,
            show_critical_error=mock.Mock(side_effect=lambda *args: calls.append(("critical", args))),
            show_busy_error=mock.Mock(),
            show_error=mock.Mock(),
        )
        app.safe_config = lambda widget, **kwargs: widget.config(**kwargs)

        def set_busy(message):
            app.operation_in_progress = True
            app.operation_message = message
            calls.append(("set_busy", message))
            app.status_var.set(message)

        def clear_busy():
            app.operation_in_progress = False
            app.operation_message = ""
            calls.append(("clear_busy",))

        def reset_current_selection():
            calls.append(("reset",))
            app.current_legal_entity = None
            app.current_group_key = None
            app.current_legal_entity_orders = []
            app.current_product_idx = 0
            app.current_order = None
            app.scanned_codes = []
            app.saved_codes_count = 0
            app.current_legal_entity_products = []

        def run_background(title, work, on_success=None, on_error=None, on_finally=None):
            entry = {
                "title": title,
                "work": work,
                "on_success": on_success,
                "on_error": on_error,
                "on_finally": on_finally,
            }
            app.background.append(entry)
            calls.append(("run_background", title))
            if auto:
                run_entry(entry)

        def refresh_legal_list():
            ScanningApp.refresh_legal_list(app)
            calls.append(("refresh", list(app.visible_order_groups)))

        app.set_busy = set_busy
        app.clear_busy = clear_busy
        app.reset_current_selection = reset_current_selection
        app.run_background = run_background
        app.refresh_legal_list = refresh_legal_list
        app.update_stats_display = lambda: None
        app._select_first_real_order = lambda: calls.append(("select_first",))
        app.sync_backend_events_async = mock.Mock(side_effect=lambda: calls.append(("sync_async",)))
        return app

    def start_finish(self, app):
        ScanningApp.finish_legal_entity(app)

    def group_key(self, orders):
        return order_group_key(orders[0])

    def call_names(self, app):
        return [call[0] for call in app.calls]


class FinishStageOneTests(FinishBackgroundTestCase):
    def test_finish_starts_print_stage_with_busy_screen_and_old_text(self):
        app = self.make_app()

        self.start_finish(app)

        self.assertEqual([call for call in app.calls if call[0] == "set_busy"], [("set_busy", BUSY_TEXT)])
        self.assertTrue(app.operation_in_progress)
        self.assertEqual(len(app.background), 1)
        self.assertEqual(app.background[0]["title"], CRITICAL_TITLE)
        self.mocks["print_summary"].assert_not_called()
        self.mocks["complete_backend_orders_or_raise"].assert_not_called()

    def test_print_stage_prints_and_does_not_touch_the_server_completion(self):
        app = self.make_app()
        self.start_finish(app)

        app.background[0]["work"]()

        self.mocks["add_pending_print"].assert_called_once()
        self.mocks["print_summary"].assert_called_once()
        self.mocks["remove_pending_print"].assert_called_once_with("print-1")
        self.mocks["sync_pending_backend_events"].assert_not_called()
        self.mocks["complete_backend_orders_or_raise"].assert_not_called()
        self.mocks["write_scan_backup"].assert_not_called()

    def test_after_printing_screen_is_free_and_status_is_new_text(self):
        app = self.make_app()
        self.start_finish(app)

        run_entry(app.background[0])

        self.assertFalse(app.operation_in_progress)
        self.assertEqual(app.status_var.value, PRINTED_TEXT)
        self.assertEqual(app.status_label.options, {"bg": BG_MAIN, "fg": FG_MUTED})
        self.assertEqual(
            [call for call in app.calls if call[0] == "set_busy"],
            [("set_busy", BUSY_TEXT)],
        )

    def test_after_printing_group_is_hidden_and_marked_finishing(self):
        app = self.make_app()
        group_key = self.group_key(app.group)
        other_key = self.group_key(app.other)
        self.start_finish(app)

        run_entry(app.background[0])

        self.assertEqual(app.finishing_group_keys, {group_key})
        self.assertEqual(app.today_orders, app.other)
        refresh_calls = [call for call in app.calls if call[0] == "refresh"]
        self.assertEqual(len(refresh_calls), 1)
        self.assertIn(other_key, refresh_calls[0][1])
        self.assertNotIn(group_key, refresh_calls[0][1])

    def test_after_printing_step_order_and_selection_reset(self):
        app = self.make_app()
        self.start_finish(app)

        run_entry(app.background[0])

        names = self.call_names(app)
        ordered = ["clear_busy", "reset", "refresh", "select_first"]
        positions = [names.index(name) for name in ordered]
        positions.append(len(names) - 1 - names[::-1].index("run_background"))
        self.assertEqual(positions, sorted(positions))
        status_positions = [index for index, call in enumerate(app.calls) if call == ("status", PRINTED_TEXT)]
        self.assertEqual(len(status_positions), 1)
        self.assertGreater(status_positions[0], names.index("refresh"))
        self.assertLess(status_positions[0], names.index("select_first"))
        self.assertIsNone(app.current_legal_entity)
        self.assertIsNone(app.current_group_key)

    def test_after_printing_completion_starts_in_background_without_busy_screen(self):
        app = self.make_app()
        self.start_finish(app)

        run_entry(app.background[0])

        self.assertEqual(len(app.background), 2)
        self.assertEqual(app.background[1]["title"], CRITICAL_TITLE)
        self.assertFalse(app.operation_in_progress)
        self.mocks["complete_backend_orders_or_raise"].assert_not_called()
        self.assertEqual([call for call in app.calls if call[0] == "set_busy"], [("set_busy", BUSY_TEXT)])

    def test_completion_starts_even_if_screen_update_after_printing_fails(self):
        # лист уже в руках: сбой перерисовки не должен оставить заказ скрытым и незавершённым
        app = self.make_app()
        self.start_finish(app)
        app.refresh_legal_list = mock.Mock(side_effect=RuntimeError("tk broke"))

        with self.assertRaises(RuntimeError):
            run_entry(app.background[0])

        self.assertEqual(len(app.background), 2)

    def test_row_numbers_path_removes_only_the_finished_positions(self):
        app = self.make_app(with_row_numbers=True)
        self.start_finish(app)

        run_entry(app.background[0])

        self.assertEqual(app.today_orders, app.other)

    def test_print_failure_keeps_screen_error_and_visible_group(self):
        app = self.make_app()
        self.mocks["print_summary"].side_effect = RuntimeError("принтер недоступен")
        self.start_finish(app)
        before = list(app.today_orders)

        run_entry(app.background[0])

        self.assertFalse(app.operation_in_progress)
        self.assertEqual(app.today_orders, before)
        self.assertFalse(getattr(app, "finishing_group_keys", set()))
        self.assertEqual(len(app.background), 1)
        app.show_critical_error.assert_called_once()
        title, exc = app.show_critical_error.call_args.args
        self.assertEqual(title, CRITICAL_TITLE)
        self.assertEqual(
            str(exc),
            "Сводный лист не напечатался. Заказ не завершён в backend. Причина: принтер недоступен",
        )
        self.assertEqual(app.finish_btn.options["state"], "normal")
        self.assertNotIn(PRINTED_TEXT, [call[1] for call in app.calls if call[0] == "status"])
        self.assertNotIn("reset", self.call_names(app))
        self.mocks["complete_backend_orders_or_raise"].assert_not_called()

    def test_print_queue_failure_keeps_old_error_text_and_visible_group(self):
        app = self.make_app()
        self.mocks["add_pending_print"].return_value = ""
        self.start_finish(app)

        run_entry(app.background[0])

        title, exc = app.show_critical_error.call_args.args
        self.assertEqual(title, CRITICAL_TITLE)
        self.assertEqual(
            str(exc),
            "Не удалось поставить сводный лист в очередь печати. Заказ не завершён в backend.",
        )
        self.assertEqual(len(app.today_orders), 4)
        self.assertFalse(app.operation_in_progress)
        self.assertEqual(len(app.background), 1)

    def test_print_queue_update_failure_keeps_old_error_text(self):
        app = self.make_app()
        self.mocks["remove_pending_print"].return_value = False
        self.start_finish(app)

        run_entry(app.background[0])

        _, exc = app.show_critical_error.call_args.args
        self.assertEqual(
            str(exc),
            "Сводный лист напечатан, но очередь печати не обновилась. Заказ не завершён в backend.",
        )
        self.assertEqual(len(app.today_orders), 4)
        self.assertEqual(len(app.background), 1)


class FinishStageTwoTests(FinishBackgroundTestCase):
    def finish_to_stage_two(self, app):
        self.start_finish(app)
        run_entry(app.background[0])
        return app.background[1]

    def test_completion_work_keeps_the_step_order_and_the_group_filter(self):
        app = self.make_app()
        order = []
        sync_result = {"synced": 2, "failed": 0, "remaining": 0, "blocked_events": []}
        pending_now = []
        self.mocks["sync_pending_backend_events"].side_effect = lambda **kwargs: order.append(("sync", kwargs)) or sync_result
        self.mocks["load_pending_backend_events"].side_effect = lambda: pending_now
        self.mocks["backend_sync_group_blocker"].side_effect = lambda *args: order.append(("blocker", args)) or ""
        self.mocks["complete_backend_orders_or_raise"].side_effect = lambda ids: order.append(("complete", ids))
        self.mocks["write_scan_backup"].side_effect = lambda *args, **kwargs: order.append(("backup", args, kwargs)) or True
        stage_two = self.finish_to_stage_two(app)

        stage_two["work"]()

        self.assertEqual([step[0] for step in order], ["sync", "blocker", "complete", "backup"])
        self.assertEqual(
            order[0][1],
            {"order_item_ids": {"item-SB-1-1", "item-SB-1-2"}, "order_ids": {"order-SB-1"}},
        )
        self.assertIs(order[1][1][0], sync_result)
        self.assertIs(order[1][1][3], pending_now)
        self.assertEqual(order[2][1], ["order-SB-1"])
        self.assertEqual(order[3][1][0], "address_finished")
        self.assertEqual(order[3][2]["codes"], ["code-0", "code-1"])

    def test_completion_success_gives_old_status_and_group_does_not_come_back(self):
        app = self.make_app()
        group_key = self.group_key(app.group)
        stage_two = self.finish_to_stage_two(app)

        run_entry(stage_two)

        self.assertEqual(app.status_var.value, COMPLETED_TEXT)
        self.assertEqual(app.status_label.options, {"bg": BG_MAIN, "fg": FG_MUTED})
        self.assertNotIn(group_key, app.finishing_group_keys)
        self.assertEqual(app.today_orders, app.other)
        app.sync_backend_events_async.assert_called_once()
        self.assertFalse(app.operation_in_progress)
        names = self.call_names(app)
        status_index = app.calls.index(("status", COMPLETED_TEXT))
        last_refresh_index = len(names) - 1 - names[::-1].index("refresh")
        self.assertLess(last_refresh_index, status_index)
        self.assertLess(status_index, names.index("sync_async"))

    def test_completion_success_does_not_reset_the_operators_current_order(self):
        app = self.make_app()
        stage_two = self.finish_to_stage_two(app)
        other_key = self.group_key(app.other)
        # пока идёт завершение, оператор выбрал другой заказ и уже сканирует
        app.current_legal_entity = "ООО Другое"
        app.current_group_key = other_key
        app.current_legal_entity_orders = app.other
        app.current_order = app.other[0]
        app.scanned_codes = ["scan-1", "scan-2"]
        app.saved_codes_count = 1
        resets_before = self.call_names(app).count("reset")

        run_entry(stage_two)

        self.assertEqual(self.call_names(app).count("reset"), resets_before)
        self.assertEqual(app.current_legal_entity, "ООО Другое")
        self.assertEqual(app.current_group_key, other_key)
        self.assertIs(app.current_order, app.other[0])
        self.assertEqual(app.scanned_codes, ["scan-1", "scan-2"])
        self.assertEqual(app.saved_codes_count, 1)
        self.assertEqual(app.order_card_list.calls[-1]["selected_key"], other_key)

    def test_completion_error_returns_group_and_shows_old_error(self):
        app = self.make_app()
        group_key = self.group_key(app.group)
        self.mocks["complete_backend_orders_or_raise"].side_effect = RuntimeError("Backend недоступен")
        stage_two = self.finish_to_stage_two(app)
        self.assertEqual(app.today_orders, app.other)

        run_entry(stage_two)

        self.assertNotIn(group_key, app.finishing_group_keys)
        self.assertCountEqual(
            [order["_backend_order_item_id"] for order in app.today_orders],
            ["item-SB-1-1", "item-SB-1-2", "item-SB-2-1", "item-SB-2-2"],
        )
        app.show_critical_error.assert_called_once()
        title, exc = app.show_critical_error.call_args.args
        self.assertEqual(title, CRITICAL_TITLE)
        self.assertEqual(str(exc), "Backend недоступен")
        refresh_calls = [call for call in app.calls if call[0] == "refresh"]
        self.assertIn(group_key, refresh_calls[-1][1])
        self.assertNotIn(COMPLETED_TEXT, [call[1] for call in app.calls if call[0] == "status"])
        app.sync_backend_events_async.assert_not_called()
        self.assertFalse(app.operation_in_progress)

    def test_completion_error_does_not_reset_the_operators_current_order(self):
        app = self.make_app()
        self.mocks["sync_pending_backend_events"].side_effect = RuntimeError("нет связи")
        stage_two = self.finish_to_stage_two(app)
        other_key = self.group_key(app.other)
        app.current_legal_entity = "ООО Другое"
        app.current_group_key = other_key
        app.current_legal_entity_orders = app.other
        app.current_order = app.other[1]
        app.scanned_codes = ["scan-1"]
        resets_before = self.call_names(app).count("reset")
        finish_options_before = dict(app.finish_btn.options)

        run_entry(stage_two)

        self.assertEqual(self.call_names(app).count("reset"), resets_before)
        self.assertIs(app.current_order, app.other[1])
        self.assertEqual(app.scanned_codes, ["scan-1"])
        self.assertEqual(app.current_group_key, other_key)
        self.assertEqual(app.order_card_list.calls[-1]["selected_key"], other_key)
        # кнопка завершения принадлежит теперь другому заказу, стадия 2 её не трогает
        self.assertEqual(app.finish_btn.options, finish_options_before)
        app.show_critical_error.assert_called_once()

    def test_completion_error_texts_from_blocker_and_backup_stay_the_same(self):
        app = self.make_app()
        self.mocks["write_scan_backup"].return_value = False
        stage_two = self.finish_to_stage_two(app)

        run_entry(stage_two)

        _, exc = app.show_critical_error.call_args.args
        self.assertEqual(str(exc), "Сводка напечатана, но backup завершения заказа не создан")
        self.assertEqual(len(app.today_orders), 4)

    def test_group_stays_hidden_while_completion_is_running(self):
        app = self.make_app()
        group_key = self.group_key(app.group)
        self.finish_to_stage_two(app)

        self.assertIn(group_key, app.finishing_group_keys)
        self.assertEqual(app.today_orders, app.other)
        self.assertFalse(app.operation_in_progress)


class FinishingGroupProtectionTests(FinishBackgroundTestCase):
    def loading_app(self, finishing_keys=None):
        app = SimpleNamespace(
            today_orders=[],
            sheet=None,
            all_existing_codes=set(),
            last_sync_result={},
            update_stats_display=lambda: None,
            show_warning=mock.Mock(),
        )
        if finishing_keys is not None:
            app.finishing_group_keys = set(finishing_keys)
        return app

    def loaded_orders(self):
        return make_group("SB-1"), make_group("SB-2", client="ООО Другое", address="Адрес 2")

    def test_refresh_result_does_not_bring_back_a_finishing_group(self):
        group, other = self.loaded_orders()
        app = self.loading_app({order_group_key(group[0])})

        with mock.patch("taksklad.app_data_loading.log_refresh_diagnostic_summary"):
            DataLoadingMixin.apply_loaded_data(app, (group + other, None, set()), show_empty_warning=False)

        self.assertEqual(app.today_orders, other)

    def test_refresh_result_with_sync_result_does_not_bring_back_a_finishing_group(self):
        group, other = self.loaded_orders()
        app = self.loading_app({order_group_key(group[0])})
        sync_result = {"synced": 0, "failed": 0, "remaining": 0}

        with mock.patch("taksklad.app_data_loading.log_refresh_diagnostic_summary"):
            DataLoadingMixin.apply_loaded_data(app, (group + other, None, set(), sync_result), show_empty_warning=False)

        self.assertEqual(app.today_orders, other)
        self.assertIs(app.last_sync_result, sync_result)

    def test_initial_load_does_not_bring_back_a_finishing_group(self):
        group, other = self.loaded_orders()
        app = self.loading_app({order_group_key(group[0])})

        with mock.patch("taksklad.app_data_loading.fetch_sheet_data", return_value=(group + other, None, set())):
            DataLoadingMixin.load_data(app, show_empty_warning=False)

        self.assertEqual(app.today_orders, other)

    def test_refresh_without_finishing_groups_keeps_all_orders(self):
        group, other = self.loaded_orders()
        app = self.loading_app()

        with mock.patch("taksklad.app_data_loading.log_refresh_diagnostic_summary"):
            DataLoadingMixin.apply_loaded_data(app, (group + other, None, set()), show_empty_warning=False)

        self.assertEqual(app.today_orders, group + other)

    def test_refresh_returns_the_group_after_it_left_the_finishing_set(self):
        group, other = self.loaded_orders()
        app = self.loading_app(set())

        with mock.patch("taksklad.app_data_loading.log_refresh_diagnostic_summary"):
            DataLoadingMixin.apply_loaded_data(app, (group + other, None, set()), show_empty_warning=False)

        self.assertEqual(app.today_orders, group + other)

    def test_refresh_between_the_stages_does_not_bring_the_group_back(self):
        app = self.make_app()
        self.start_finish(app)
        run_entry(app.background[0])
        server_snapshot = list(app.group) + list(app.other)
        app.sheet = None
        app.all_existing_codes = set()
        app.last_sync_result = {}

        with mock.patch("taksklad.app_data_loading.log_refresh_diagnostic_summary"):
            DataLoadingMixin.apply_loaded_data(app, (server_snapshot, None, set()), show_empty_warning=False)

        self.assertEqual(app.today_orders, app.other)

    def test_finish_does_nothing_for_a_group_that_is_finishing(self):
        app = self.make_app()
        app.finishing_group_keys = {app.current_group_key}

        self.start_finish(app)

        self.assertEqual(app.background, [])
        self.assertFalse(app.operation_in_progress)
        self.assertEqual([call for call in app.calls if call[0] == "set_busy"], [])
        app.show_busy_error.assert_called_once_with()

    def test_select_does_nothing_for_a_group_that_is_finishing(self):
        app = self.make_app()
        group_key = self.group_key(app.group)
        app.finishing_group_keys = {group_key}
        app.current_legal_entity = None
        app.current_group_key = None
        app.current_legal_entity_orders = []
        app._selected_order_group = lambda: group_key
        app.load_current_product = mock.Mock()
        app.update_party_summary_display = mock.Mock()
        app.product_catalog = {}

        ScanningApp.select_legal_entity(app)

        app.show_busy_error.assert_called_once_with()
        app.load_current_product.assert_not_called()
        self.assertIsNone(app.current_legal_entity)
        self.assertIsNone(app.current_group_key)
        self.assertEqual(app.current_legal_entity_orders, [])

    def test_select_still_works_for_a_group_that_is_not_finishing(self):
        app = self.make_app()
        other_key = self.group_key(app.other)
        app.finishing_group_keys = {self.group_key(app.group)}
        app.current_legal_entity = None
        app.current_group_key = None
        app.current_legal_entity_orders = []
        app._selected_order_group = lambda: other_key
        app.load_current_product = mock.Mock()
        app.update_party_summary_display = mock.Mock()
        app.product_catalog = {}
        app.scan_entry = SimpleNamespace(focus_set=lambda: None)

        ScanningApp.select_legal_entity(app)

        app.show_busy_error.assert_not_called()
        app.load_current_product.assert_called_once()
        self.assertEqual(app.current_group_key, other_key)

    def test_refresh_list_keeps_the_selected_group_key(self):
        app = self.make_app()
        other_key = self.group_key(app.other)
        app.current_group_key = other_key

        ScanningApp.refresh_legal_list(app)

        self.assertEqual(app.order_card_list.calls[-1]["selected_key"], other_key)


class FinishFromLastPositionTests(FinishBackgroundTestCase):
    FIRST_CODE = "01040063960540670001XXXXXXXXXXXXXXX"
    SECOND_CODE = "01040063960540670002XXXXXXXXXXXXXXX"

    def make_last_position_app(self):
        group = make_group("SB-1", positions=1)
        group[0]["Кол-во блок"] = 2
        group[0]["Отсканированные коды"] = ""
        app = self.make_app(group=group, auto=True)
        app.current_legal_entity_products = []
        app.current_product_idx = 0
        app.current_order = group[0]
        app.scanned_codes = [self.FIRST_CODE, self.SECOND_CODE]
        app.completed_orders = []
        app.product_catalog = {}
        app.set_scan_entry_enabled = lambda *args, **kwargs: None
        app.after = lambda delay, callback: callback()
        app.finish_legal_entity = lambda from_next_product=False: ScanningApp.finish_legal_entity(
            app, from_next_product=from_next_product
        )
        return app

    def test_last_position_path_reaches_the_same_split(self):
        app = self.make_last_position_app()
        operation_state_at_completion = []
        self.mocks["complete_backend_orders_or_raise"].side_effect = (
            lambda ids: operation_state_at_completion.append(app.operation_in_progress)
        )
        group_key = self.group_key(app.group)

        with (
            mock.patch("taksklad.app_scanning.order_uses_backend_scan_path", return_value=True),
            mock.patch("taksklad.app_scanning.is_scan_delivered", return_value=True),
            mock.patch("taksklad.app_scanning.queue_backend_scan", return_value="event"),
            mock.patch("taksklad.app_scanning.sync_pending_backend_events", return_value={
                "synced": 1, "failed": 0, "remaining": 0, "blocked_events": [],
            }),
            mock.patch("taksklad.app_scanning.load_pending_backend_events", return_value=[]),
            mock.patch("taksklad.app_scanning.write_scan_backup", return_value=True),
        ):
            ScanningApp.next_product(app, finish_after_save=True)

        busy_messages = [call[1] for call in app.calls if call[0] == "set_busy"]
        self.assertEqual(busy_messages, ["⏳ Сохраняю КИЗы в VDS...", BUSY_TEXT])
        statuses = [call[1] for call in app.calls if call[0] == "status"]
        self.assertIn(PRINTED_TEXT, statuses)
        self.assertLess(statuses.index(PRINTED_TEXT), statuses.index(COMPLETED_TEXT))
        self.assertEqual(statuses[-1], COMPLETED_TEXT)
        self.assertEqual(operation_state_at_completion, [False])
        self.mocks["complete_backend_orders_or_raise"].assert_called_once_with(["order-SB-1"])
        self.assertEqual(app.today_orders, app.other)
        self.assertNotIn(group_key, app.finishing_group_keys)
        self.assertFalse(app.operation_in_progress)


if __name__ == "__main__":
    unittest.main()
