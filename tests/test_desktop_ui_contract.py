import inspect
import unittest
from unittest import mock

from taksklad import app_day_end
from taksklad.config import ACCENT, BG_MAIN, FG_TEXT, WARNING
from taksklad.app_day_end import build_backend_status
from taksklad.main import ScanningApp
from taksklad.startup_check import format_app_version_label
from taksklad.ui_widgets import AppButton


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
        self.assertIn("format_app_version_label", source)
        self.assertIn("MVP 2.0", format_app_version_label())

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
        self.assertIn("print_summary(address, summary_products)", finish_source)

    def test_final_position_guides_warehouse_to_finish_order(self):
        load_source = inspect.getsource(ScanningApp.load_current_product)
        scan_source = inspect.getsource(ScanningApp.on_scan)
        finish_source = inspect.getsource(ScanningApp.finish_legal_entity)

        self.assertIn("self.finish_btn.config(state=\"disabled\")", load_source)
        self.assertIn("Заказ выполнен! Нажмите 'ЗАВЕРШИТЬ ЗАКАЗ'", scan_source)
        self.assertIn("self.current_product_idx >= len(self.current_legal_entity_orders) - 1", scan_source)
        self.assertIn("self.current_product_idx == len(self.current_legal_entity_orders) - 1", finish_source)
        self.assertIn("self.next_product()", finish_source)

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
        self.assertEqual(WARNING.lower(), "#f0e68c")
        self.assertEqual(ACCENT.lower(), "#111111")
        self.assertEqual(FG_TEXT.lower(), "#111111")
        self.assertEqual(BG_MAIN.lower(), "#f7f5df")

        signature = inspect.signature(AppButton.__init__)
        self.assertIn("radius", signature.parameters)
        self.assertGreaterEqual(signature.parameters["radius"].default, 8)

        build_source = inspect.getsource(ScanningApp._build_ui)
        self.assertIn("bg=WARNING, fg=FG_TEXT", build_source)

    def test_backend_status_is_visible_on_warehouse_screen(self):
        build_source = inspect.getsource(ScanningApp._build_ui)
        stats_source = inspect.getsource(app_day_end.DayEndActionsMixin.update_stats_display)

        self.assertIn("self.backend_status_label", build_source)
        self.assertNotIn("Backend: ожидает проверки", build_source)
        self.assertIn("build_backend_status", stats_source)
        self.assertIn("Синхронизация: OK", stats_source)

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
            text, _ = build_backend_status({"backend": {"enabled": True, "remaining": 1}}, pending_backend=2)
        self.assertEqual(text, "Синхронизация: ожидает отправки")

        with (
            mock.patch.object(app_day_end, "backend_enabled", return_value=True),
            mock.patch.object(app_day_end, "backend_configured", return_value=True),
        ):
            text, _ = build_backend_status({"backend": {"enabled": True, "failed": 1, "remaining": 3}})
        self.assertEqual(text, "Синхронизация: временная ошибка")

        with (
            mock.patch.object(app_day_end, "backend_enabled", return_value=True),
            mock.patch.object(app_day_end, "backend_configured", return_value=True),
        ):
            text, _ = build_backend_status({"backend": {"enabled": True, "blocked": 1}})
        self.assertEqual(text, "Синхронизация: заказ недосканирован")


if __name__ == "__main__":
    unittest.main()
