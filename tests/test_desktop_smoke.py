import unittest

from taksklad.desktop_smoke import (
    collect_main_screen_smoke_snapshot,
    run_tk_app_smoke,
    validate_main_screen_smoke_snapshot,
)


class FakeWidget:
    def __init__(self, text="", state="normal", width=None, height=None, placeholder=None):
        self._options = {"text": text, "state": state}
        if width is not None:
            self._options["width"] = width
        if height is not None:
            self._options["height"] = height
        if placeholder is not None:
            self._placeholder_text = placeholder

    def cget(self, key):
        return self._options.get(key, "")


class FakeVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class FakeOrderCardList:
    canvas = object()
    scrollbar = object()


class DesktopSmokeTests(unittest.TestCase):
    def test_gui_smoke_builds_ui_flushes_layout_and_closes_window(self):
        events = []

        class FakeApp:
            def __init__(self):
                attach_main_screen_widgets(self)

            def update_idletasks(self):
                events.append("update_idletasks")

            def destroy(self):
                events.append("destroy")

        result = run_tk_app_smoke(FakeApp)

        self.assertEqual(result, 0)
        self.assertEqual(events, ["update_idletasks", "destroy"])

    def test_gui_smoke_closes_window_when_layout_flush_fails(self):
        events = []

        class FakeApp:
            def __init__(self):
                attach_main_screen_widgets(self)

            def update_idletasks(self):
                events.append("update_idletasks")
                raise RuntimeError("tk failed")

            def destroy(self):
                events.append("destroy")

        with self.assertRaisesRegex(RuntimeError, "tk failed"):
            run_tk_app_smoke(FakeApp)

        self.assertEqual(events, ["update_idletasks", "destroy"])

    def test_gui_smoke_collects_semantic_main_screen_snapshot(self):
        app = type("FakeApp", (), {})()
        attach_main_screen_widgets(app)

        snapshot = collect_main_screen_smoke_snapshot(app)
        validate_main_screen_smoke_snapshot(snapshot)

        self.assertEqual(snapshot["texts"]["current_info"], "Не выбрано")
        self.assertEqual(snapshot["texts"]["party_summary_label"], "Партия не выбрана")
        self.assertIn("ВЫБРАТЬ ЗАКАЗ", snapshot["texts"]["select_btn"])
        self.assertIn("Готов", snapshot["status_text"])
        self.assertEqual(snapshot["product_photo_size"], (170, 170))
        self.assertTrue(snapshot["order_card_list_scrollable"])

    def test_gui_smoke_rejects_missing_main_screen_widget(self):
        app = type("FakeApp", (), {})()
        attach_main_screen_widgets(app)
        delattr(app, "scan_entry")

        snapshot = collect_main_screen_smoke_snapshot(app)

        with self.assertRaisesRegex(RuntimeError, "scan_entry"):
            validate_main_screen_smoke_snapshot(snapshot)


def attach_main_screen_widgets(app):
    app.order_list_subtitle_label = FakeWidget("0 активных заказов · список листается вниз")
    app.search_entry = FakeWidget(placeholder="Поиск клиента, адреса или заявки")
    app.order_card_list = FakeOrderCardList()
    app.order_list_counter_label = FakeWidget("Показаны 0 из 0")
    app.select_btn = FakeWidget("✅ ВЫБРАТЬ ЗАКАЗ")
    app.returns_btn = FakeWidget("↩ ВОЗВРАТЫ")
    app.current_info = FakeWidget("Не выбрано")
    app.product_photo_canvas = FakeWidget(width=170, height=170)
    app.product_photo_gtin_label = FakeWidget("GTIN")
    app.product_photo_caption_label = FakeWidget("Фото товара")
    app.current_client_label = FakeWidget("")
    app.current_product_label = FakeWidget("")
    app.party_summary_label = FakeWidget("Партия не выбрана")
    app.position_label = FakeWidget("")
    app.progress_label = FakeWidget("0 / 0")
    app.scan_entry = FakeWidget("", state="disabled")
    app.scan_guard_label = FakeWidget("SKU-защита недоступна: выберите позицию.")
    app.last_code_label = FakeWidget("")
    app.undo_btn = FakeWidget("↩️ ОТМЕНИТЬ ПОСЛЕДНИЙ КОД", state="disabled")
    app.next_product_btn = FakeWidget("➡️ СЛЕДУЮЩАЯ ПОЗИЦИЯ", state="disabled")
    app.finish_btn = FakeWidget("🏁 ЗАВЕРШИТЬ ЗАКАЗ", state="disabled")
    app.completed_count_label = FakeWidget("0")
    app.total_blocks_label = FakeWidget("0")
    app.active_orders_label = FakeWidget("0")
    app.pending_events_label = FakeWidget("OK")
    app.sync_caption_label = FakeWidget("Синхронизация")
    app.backend_status_label = FakeWidget("")
    app.sync_queue_btn = FakeWidget("ОЧЕРЕДИ")
    app.diagnostics_btn = FakeWidget("ДИАГНОСТИКА")
    app.report_btn = FakeWidget("📊 ЗАКРЫТЬ СМЕНУ")
    app.error_toast = FakeWidget("")
    app.status_var = FakeVar("✅ Готов к работе")
    app.status_label = FakeWidget("")
    app.version_status_label = FakeWidget("Версия: 2.0.50 · MVP 2.0 · проверка обновлений · ПК abc123")


if __name__ == "__main__":
    unittest.main()
