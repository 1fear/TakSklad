from datetime import datetime
import tkinter as tk
from tkinter import messagebox

from .config import DANGER, ERROR_FG, FG_MUTED, FG_TEXT, SUCCESS, WARNING
from .backend_client import backend_configured, backend_enabled, fetch_day_report
from .backend_events import load_pending_backend_events
from .orders import order_group_key
from .pending_store import load_pending_prints
from .telegram_service import load_pending_telegram


def build_backend_status(sync_result=None, pending_backend=0):
    if not backend_enabled():
        return "", FG_MUTED
    if not backend_configured():
        return "Синхронизация: сервер не настроен", ERROR_FG

    backend_result = {}
    if isinstance(sync_result, dict) and isinstance(sync_result.get("backend"), dict):
        backend_result = sync_result["backend"]

    failed = parse_count(backend_result.get("failed"))
    blocked = parse_count(backend_result.get("blocked"))
    remaining = max(parse_count(pending_backend), parse_count(backend_result.get("remaining")))

    if blocked:
        return "Синхронизация: заказ недосканирован", DANGER
    if failed and remaining:
        return "Синхронизация: ожидает повторной отправки", FG_MUTED
    if failed:
        return "Синхронизация: нужна проверка", ERROR_FG
    if remaining:
        return "Синхронизация: ожидает отправки", FG_MUTED
    if backend_result.get("enabled"):
        return "", FG_MUTED
    return "", FG_MUTED


def parse_count(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class DayEndActionsMixin:
    def update_stats_display(self):
        if not hasattr(self, "completed_count_label"):
            return
        completed = len(self.completed_orders)
        total_blocks = sum(o.get("Отсканировано", 0) for o in self.completed_orders)
        self.completed_count_label.config(text=str(completed), fg=FG_TEXT)
        self.total_blocks_label.config(text=str(total_blocks), fg=FG_TEXT)
        active_groups = len({order_group_key(order) for order in self.today_orders})
        pending_prints = len(load_pending_prints())
        pending_telegram = len(load_pending_telegram())
        pending_backend = len(load_pending_backend_events())
        self.active_orders_label.config(text=str(active_groups), fg=FG_TEXT)
        pending_total = pending_prints + pending_telegram + pending_backend
        sync_caption = getattr(self, "sync_caption_label", None)
        if pending_total:
            self.pending_events_label.config(text=str(pending_total), fg=WARNING)
            if sync_caption:
                sync_caption.config(text="В очереди")
        else:
            self.pending_events_label.config(text="OK", fg=SUCCESS)
            if sync_caption:
                sync_caption.config(text="Синхронизация")
        if hasattr(self, "backend_status_label"):
            backend_status_text, backend_status_color = build_backend_status(
                getattr(self, "last_sync_result", {}),
                pending_backend=pending_backend,
            )
            self.backend_status_label.config(text=backend_status_text, fg=backend_status_color)

    def end_day(self):
        if not self.ensure_update_allowed():
            return

        if self.operation_in_progress:
            self.show_busy_error()
            return

        if self.current_legal_entity:
            if not messagebox.askyesno("Внимание", "У вас есть незавершённый заказ!\n\nЗавершить день без сохранения текущего заказа?"):
                return

        self.set_busy("⏳ Формирую и отправляю Excel-отчёты смены...")
        self.safe_config(self.report_btn, state="disabled")

        def work():
            if not backend_configured():
                raise RuntimeError("Backend не настроен. Закрытие смены заблокировано")
            return fetch_day_report(datetime.now().date())

        def on_success(result):
            totals = result.get("totals") or {}
            if not totals.get("scan_codes"):
                self.show_warning("За сегодня нет отсканированных КИЗов для отчёта")
                return
            self.show_info(
                "📊 Смена сверена с backend/PostgreSQL\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ КИЗов: {totals.get('scan_codes', 0)}\n"
                f"📦 Отсканировано блоков: {totals.get('scanned_blocks', 0)}\n"
                f"📋 Заказов завершено: {totals.get('completed_orders', 0)}\n"
                f"⏳ Осталось блоков: {totals.get('remaining_blocks', 0)}\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Excel/Telegram-отчёт формирует серверный worker.",
            )

            try:
                self.after(5000, self.on_close)
            except tk.TclError:
                pass

        def on_error(exc):
            if isinstance(exc, ImportError):
                self.show_critical_error("Не установлены зависимости для Excel-отчёта", "Установите pandas и openpyxl:\npip install pandas openpyxl")
            else:
                self.show_critical_error("Не удалось сохранить Excel-отчёт", exc)

        def on_finally():
            self.clear_busy()
            try:
                if self.winfo_exists():
                    self.safe_config(self.report_btn, state="normal")
            except tk.TclError:
                pass

        self.run_background(
            "Не удалось сохранить Excel-отчёт",
            work,
            on_success=on_success,
            on_error=on_error,
            on_finally=on_finally,
        )
