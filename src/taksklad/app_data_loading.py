import logging
import tkinter as tk

from .backend_client import backend_enabled, backend_read_orders_enabled
from .backend_events import load_pending_backend_events, sync_pending_backend_events
from .backend_flow import backend_blocked_scan_events_for_item
from .config import BG_MAIN, FG_MUTED, STATUS_COLUMN, STATUS_COMPLETED, STATUS_NOT_COMPLETED
from .desktop_diagnostics import log_refresh_diagnostic_summary
from .desktop_scan_rules import (
    is_terminal_scan_state,
    scanned_blocks_for_order,
    scanned_codes_for_order,
)
from .desktop_refresh_service import (
    fetch_sheet_data,
    fetch_sheet_data_with_sync,
    format_refresh_error_message,
)
from .orders import get_plan_blocks
from .pending_store import load_pending_saves
from .scan_quantities import scan_entries_for_order_codes
from .utils import normalize_kiz_code, normalize_text


TERMINAL_REFRESH_STATUSES = {
    "completed",
    "complete",
    "done",
    "returned",
    "выполнено",
    "возврат",
    "завершено",
}


def refresh_order_identity(order):
    order = order if isinstance(order, dict) else {}
    backend_item_id = normalize_text(order.get("_backend_order_item_id") or order.get("order_item_id"))
    if backend_item_id:
        return ("backend_item", backend_item_id)
    source_import_id = normalize_text(order.get("ID импорта") or order.get("source_import_id") or order.get("import_id"))
    if source_import_id:
        return ("source_import", source_import_id)
    row_number = normalize_text(order.get("_row_number") or order.get("row_number"))
    if row_number:
        return ("google_row", row_number)
    source_order_id = normalize_text(order.get("ID заказа") or order.get("id") or order.get("_backend_order_id"))
    product = normalize_text(order.get("Товары") or order.get("product"))
    if source_order_id and product:
        return ("source_item", source_order_id, product.casefold())
    request_number = normalize_text(order.get("Номер заявки SkladBot") or order.get("skladbot_request_number"))
    client = normalize_text(order.get("Клиент") or order.get("client"))
    address = normalize_text(order.get("Адрес") or order.get("address"))
    if request_number and product:
        return ("request_item", request_number, product.casefold(), client.casefold(), address.casefold())
    return ()


def find_refreshed_order(current_order, loaded_orders):
    identity = refresh_order_identity(current_order)
    if not identity:
        return None
    for order in loaded_orders or []:
        if refresh_order_identity(order) == identity:
            return order
    return None


def merge_remote_and_local_scan_codes(remote_order, local_codes):
    remote_codes = scanned_codes_for_order(remote_order)
    seen = {normalize_kiz_code(code) for code in remote_codes if normalize_kiz_code(code)}
    merged = list(remote_codes)
    local_unsaved = []
    for code in local_codes or []:
        normalized = normalize_kiz_code(code)
        if not normalized or normalized in seen:
            continue
        merged.append(code)
        local_unsaved.append(code)
        seen.add(normalized)
    return {
        "remote_codes": remote_codes,
        "local_unsaved": local_unsaved,
        "merged_codes": merged,
    }


def refresh_order_is_terminal(order):
    if not order:
        return False
    status = normalize_text(order.get(STATUS_COLUMN) or order.get("status")).lower().replace("ё", "е")
    return is_terminal_scan_state(order) or status in TERMINAL_REFRESH_STATUSES


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

    def _set_refresh_scan_controls(self, *, enabled, message=""):
        set_scan_entry_enabled = getattr(self, "set_scan_entry_enabled", None)
        if callable(set_scan_entry_enabled):
            set_scan_entry_enabled(enabled, message)
        if not enabled:
            self.safe_config(getattr(self, "next_product_btn", None), state="disabled")
            self.safe_config(getattr(self, "finish_btn", None), state="disabled")
            self.safe_config(getattr(self, "undo_btn", None), state="disabled")

    def reconcile_current_order_after_refresh(self):
        current_order = self.current_order
        if not current_order:
            return {"status": "none"}

        refreshed_order = find_refreshed_order(current_order, self.today_orders)
        if refreshed_order is None:
            self._set_refresh_scan_controls(
                enabled=False,
                message="SKU-защита недоступна: позиция больше не активна после обновления.",
            )
            return {"status": "missing", "message": "Текущая позиция больше не активна после обновления."}

        refreshed_snapshot = dict(refreshed_order)
        terminal = refresh_order_is_terminal(refreshed_snapshot)
        saved_count = max(0, min(int(self.saved_codes_count or 0), len(self.scanned_codes)))
        local_unsaved_codes = list(self.scanned_codes[saved_count:])
        merged = merge_remote_and_local_scan_codes(refreshed_snapshot, local_unsaved_codes)
        active_codes = merged["remote_codes"] if terminal else merged["merged_codes"]

        current_order.clear()
        current_order.update(refreshed_snapshot)
        current_order["_existing_scanned_codes"] = merged["remote_codes"].copy()
        current_order["_existing_scan_entries"] = scan_entries_for_order_codes(
            current_order,
            merged["remote_codes"],
        )
        self.scanned_codes = active_codes
        self.saved_codes_count = len(merged["remote_codes"])
        for code in active_codes:
            normalized = normalize_kiz_code(code)
            if normalized:
                self.all_existing_codes.add(normalized)

        plan_blocks = get_plan_blocks(current_order)
        scanned_count = scanned_blocks_for_order(current_order, self.scanned_codes)
        self.safe_config(getattr(self, "progress_label", None), text=f"{scanned_count} / {plan_blocks}")
        if terminal:
            self._set_refresh_scan_controls(
                enabled=False,
                message="SKU-защита недоступна: позиция уже закрыта после обновления.",
            )
            return {
                "status": "terminal",
                "remote_codes": merged["remote_codes"],
                "local_unsaved": merged["local_unsaved"],
                "message": "Позиция уже закрыта или возвращена на другом ПК.",
            }

        self._set_refresh_scan_controls(enabled=True)
        return {
            "status": "merged",
            "remote_codes": merged["remote_codes"],
            "local_unsaved": merged["local_unsaved"],
            "merged_codes": merged["merged_codes"],
        }


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
                reconcile_result = {"status": "reset"}
            else:
                reconcile_result = self.reconcile_current_order_after_refresh()
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
                reconcile_status = reconcile_result.get("status")
                if reconcile_status == "terminal":
                    status_text += ", текущая позиция закрыта на другом ПК"
                elif reconcile_status == "missing":
                    status_text += ", текущая позиция больше не активна"
                else:
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
