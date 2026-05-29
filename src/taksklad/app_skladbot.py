import logging
import tkinter as tk

from .config import BG_MAIN, FG_MUTED, SKLADBOT_SYNC_INTERVAL_MS
from .skladbot_sync import sync_skladbot_request_numbers


class SkladBotActionsMixin:
    def run_skladbot_periodic_refresh(self):
        try:
            if (
                not self.update_required
                and not self.operation_in_progress
                and not self.refresh_in_progress
                and not self.current_order
            ):
                self.sync_skladbot_async()
        finally:
            try:
                self.after(SKLADBOT_SYNC_INTERVAL_MS, self.run_skladbot_periodic_refresh)
            except tk.TclError:
                pass

    def sync_skladbot_async(self):
        if (
            self.skladbot_sync_running
            or self.refresh_in_progress
            or self.operation_in_progress
            or self.current_order
            or not self.sheet
        ):
            return

        self.skladbot_sync_running = True

        def work():
            skladbot_result = sync_skladbot_request_numbers(self.sheet)
            loaded = None
            if skladbot_result.get("updated"):
                loaded = self.fetch_sheet_data_after_skladbot_sync()
            return skladbot_result, loaded

        def on_success(result):
            skladbot_result, loaded = result
            if loaded and not self.operation_in_progress and not self.current_order:
                self.apply_loaded_data(loaded, show_empty_warning=False)
                self.refresh_legal_list()
            if isinstance(self.last_sync_result, dict):
                self.last_sync_result["skladbot"] = skladbot_result
            if loaded and not self.operation_in_progress and not self.current_order:
                self.status_var.set(
                    "✅ SkladBot обновлён в фоне, список заказов актуализирован"
                )
                self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)

        def on_error(exc):
            logging.error(
                "SkladBot: фоновая синхронизация не выполнена",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            if not self.operation_in_progress and not self.refresh_in_progress and not self.current_order:
                self.status_var.set(
                    "⚠️ SkladBot не обновился в фоне, список заказов оставлен без изменений"
                )
                self.safe_config(self.status_label, bg=BG_MAIN, fg=FG_MUTED)

        def on_finally():
            self.skladbot_sync_running = False

        self.run_background(
            "SkladBot: фоновая синхронизация не выполнена",
            work,
            on_success=on_success,
            on_error=on_error,
            on_finally=on_finally,
        )
