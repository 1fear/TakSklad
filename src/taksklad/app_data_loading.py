import logging
import tkinter as tk

from .backend_client import backend_enabled, backend_read_orders_enabled
from .backend_events import load_pending_backend_events, sync_pending_backend_events
from .backend_flow import backend_blocked_scan_events_for_item
from .config import BG_MAIN, FG_MUTED, STATUS_COLUMN, STATUS_COMPLETED, STATUS_NOT_COMPLETED
from .desktop_diagnostics import log_refresh_diagnostic_summary
from .desktop_refresh_service import (
    fetch_sheet_data,
    fetch_sheet_data_with_sync,
    format_refresh_error_message,
)
from .pending_store import load_pending_saves


class DataLoadingMixin:
    def load_data(self, show_empty_warning=True):
        self.today_orders, self.sheet, self.all_existing_codes = fetch_sheet_data()
        if show_empty_warning and not self.today_orders:
            self.show_warning(
                f"Нет заказов со статусом '{STATUS_NOT_COMPLETED}'.\n\n"
                f"Проверьте:\n"
                f"1. В таблице есть строки заказов\n"
                f"2. Колонка '{STATUS_COLUMN}' не заполнена как '{STATUS_COMPLETED}'\n"
                f"3. Колонка 'Отсканированные коды' заполнена не полностью"
            )


    def fetch_sheet_data_after_import(self):
        return fetch_sheet_data_with_sync(sync_skladbot=False)


    def fetch_sheet_data_after_skladbot_sync(self):
        return fetch_sheet_data()


    def sync_backend_events_async(self):
        if not backend_enabled() or self.backend_sync_running:
            try:
                self.after(15000, self.sync_backend_events_async)
            except tk.TclError:
                pass
            return

        self.backend_sync_running = True

        def work():
            return sync_pending_backend_events()

        def on_success(result):
            if isinstance(result, dict):
                self.last_sync_result["backend"] = result
            remaining = result.get("remaining", 0) if isinstance(result, dict) else 0
            if remaining:
                logging.info("Backend queue: осталось событий в очереди: %s", remaining)
            if isinstance(result, dict) and self.current_order:
                blocked_events = backend_blocked_scan_events_for_item(
                    result,
                    self.current_order.get("_backend_order_item_id"),
                )
                if blocked_events:
                    self.apply_backend_blocked_scan_events(blocked_events)
            self.update_stats_display()

        def on_error(exc):
            logging.info("Backend queue: синхронизация отложена: %s", exc)
            self.last_sync_result["backend"] = {
                "enabled": True,
                "failed": 1,
                "remaining": len(load_pending_backend_events()),
            }
            self.update_stats_display()

        def on_finally():
            self.backend_sync_running = False
            try:
                self.after(15000, self.sync_backend_events_async)
            except tk.TclError:
                pass

        self.run_background(
            "Не удалось синхронизировать backend-очередь",
            work,
            on_success=on_success,
            on_error=on_error,
            on_finally=on_finally,
        )


    def apply_loaded_data(self, result, show_empty_warning):
        if len(result) == 4:
            self.today_orders, self.sheet, self.all_existing_codes, self.last_sync_result = result
        else:
            self.today_orders, self.sheet, self.all_existing_codes = result
            self.last_sync_result = {"synced": 0, "failed": 0, "remaining": len(load_pending_saves())}

        if show_empty_warning and not self.today_orders:
            self.show_warning(
                f"Нет заказов со статусом '{STATUS_NOT_COMPLETED}'.\n\n"
                f"Проверьте:\n"
                f"1. В таблице есть строки заказов\n"
                f"2. Колонка '{STATUS_COLUMN}' не заполнена как '{STATUS_COMPLETED}'\n"
                f"3. Колонка 'Отсканированные коды' пустая или заполнена не полностью у активных заказов"
            )
        self.update_stats_display()
        log_refresh_diagnostic_summary(
            self.today_orders,
            self.all_existing_codes,
            sync_result=self.last_sync_result,
            source="backend" if backend_read_orders_enabled() else "google",
        )


    def refresh_from_sheet(self, initial=False):
        if not self.ensure_update_allowed():
            return

        if self.operation_in_progress:
            self.show_busy_error()
            return

        if self.refresh_in_progress:
            self.show_refresh_busy_error()
            return

        refresh_started_with_selection = bool(self.current_order) and not initial
        if refresh_started_with_selection:
            self.set_refresh_in_progress("⏳ Обновляю список заказов в фоне, сканирование доступно...")
        else:
            self.set_refresh_in_progress("⏳ Обновляю список заказов...")
        self.safe_config(self.refresh_btn, state="disabled")
        self.safe_config(self.import_btn, state="disabled")

        def on_success(result):
            keep_current_selection = bool(self.current_order) and not initial
            self.apply_loaded_data(result, show_empty_warning=initial)
            if not keep_current_selection:
                self.reset_current_selection()
            self.refresh_legal_list()
            sync_result = self.last_sync_result or {}
            skladbot_result = sync_result.get("skladbot", {}) if isinstance(sync_result, dict) else {}
            backend_result = sync_result.get("backend", {}) if isinstance(sync_result, dict) else {}
            google_result = sync_result.get("google_sheets", {}) if isinstance(sync_result, dict) else {}
            if backend_result.get("enabled") and backend_read_orders_enabled():
                if backend_result.get("remaining"):
                    status_text = f"⚠️ Список из backend обновлён, очередь backend: {backend_result.get('remaining')}"
                else:
                    google_updates = int(google_result.get("orders_updated") or 0) + int(google_result.get("items_updated") or 0)
                    if skladbot_result.get("errors"):
                        status_text = f"⚠️ Список обновлён из backend, Google правок: {google_updates}, SkladBot недоступен"
                    elif skladbot_result.get("pending"):
                        status_text = (
                            f"✅ Список обновлён из Google/backend, Google правок {google_updates}; "
                            "SkladBot обновляется в фоне"
                        )
                    elif skladbot_result.get("enabled"):
                        status_text = (
                            f"✅ Список обновлён из всех источников: Google правок {google_updates}, "
                            f"SkladBot найдено {skladbot_result.get('matched', 0)}"
                        )
                    else:
                        status_text = f"✅ Список заказов обновлён из backend, Google правок: {google_updates}"
            elif sync_result.get("synced"):
                status_text = f"✅ Список обновлён, отправлено из очереди: {sync_result['synced']}"
            elif skladbot_result.get("errors"):
                status_text = (
                    "⚠️ Список загружен из Google, SkladBot временно недоступен"
                )
            elif skladbot_result.get("enabled"):
                status_text = (
                    "✅ Список обновлён, SkladBot: "
                    f"найдено {skladbot_result.get('matched', 0)}, "
                    f"не найдено {skladbot_result.get('not_found', 0)}, "
                    f"дублей {skladbot_result.get('multiple', 0)}"
                )
            else:
                status_text = "✅ Список заказов обновлён"
            if keep_current_selection:
                status_text += ", текущая позиция сохранена"
            self.status_var.set(status_text)
            self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)

        def on_error(exc):
            logging.error(
                "Не удалось обновить список заказов",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self.show_error(
                format_refresh_error_message(exc, has_cached_orders=bool(self.today_orders)),
                popup=True,
            )

        def on_finally():
            self.clear_refresh_in_progress()
            self.safe_config(self.refresh_btn, state="normal")
            self.safe_config(self.import_btn, state="normal")
            try:
                if not self.current_order and not self.operation_in_progress:
                    self.after(100, self.sync_skladbot_async)
            except tk.TclError:
                pass

        self.run_background(
            "Не удалось обновить список заказов",
            lambda: fetch_sheet_data_with_sync(sync_skladbot=True),
            on_success=on_success,
            on_error=on_error,
            on_finally=on_finally
        )
