from datetime import datetime
import tkinter as tk
from tkinter import messagebox

from .config import SHEET_NAME, SPREADSHEET_ID
from .backend_events import load_pending_backend_events
from .orders import order_group_key
from .pending_store import load_pending_saves
from .reports import create_day_report_excel
from .sheets import get_google_client
from .telegram_service import send_daily_report_result_to_telegram


class DayEndActionsMixin:
    def update_stats_display(self):
        if not hasattr(self, "completed_count_label"):
            return
        completed = len(self.completed_orders)
        total_blocks = sum(o.get("Отсканировано", 0) for o in self.completed_orders)
        self.completed_count_label.config(text=f"Выполнено: {completed}")
        self.total_blocks_label.config(text=f"Блоков: {total_blocks}")
        active_groups = len({order_group_key(order) for order in self.today_orders})
        pending_saves = len(load_pending_saves())
        pending_backend = len(load_pending_backend_events())
        self.active_orders_label.config(text=f"Активных заказов: {active_groups}")
        if pending_backend:
            self.pending_saves_label.config(text=f"Очередь записи: {pending_saves}, backend: {pending_backend}")
        else:
            self.pending_saves_label.config(text=f"Очередь записи: {pending_saves}")

    def end_day(self):
        if not self.ensure_update_allowed():
            return

        if self.operation_in_progress:
            self.show_busy_error()
            return

        if self.current_legal_entity:
            if not messagebox.askyesno("Внимание", "У вас есть незавершённый заказ!\n\nЗавершить день без сохранения текущего заказа?"):
                return

        self.set_busy("⏳ Формирую и отправляю Excel-отчёт за день...")
        self.safe_config(self.report_btn, state="disabled")

        def work():
            sheet = self.sheet
            result = create_day_report_excel(sheet, report_date=datetime.now().date())
            if result.get("empty") and not sheet:
                client = get_google_client()
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
                result = create_day_report_excel(sheet, report_date=datetime.now().date())

            result["sheet"] = sheet
            if not result.get("empty"):
                ok, message, status = send_daily_report_result_to_telegram(
                    result,
                    reason="Отправлено при ручном завершении дня",
                )
                result["telegram_ok"] = ok
                result["telegram_message"] = message
                result["telegram_status"] = status
            return result

        def on_success(result):
            self.sheet = result.get("sheet") or self.sheet
            if result.get("empty"):
                messagebox.showwarning("Нет данных", "За сегодня нет отсканированных КИЗов для отчёта")
                return

            total_report_rows = result["total_report_rows"]
            telegram_status = {
                "sent": "отправлен",
                "queued": "поставлен в очередь отправки",
                "failed": "не отправлен",
            }.get(result.get("telegram_status"), "не отправлен")
            messagebox.showinfo(
                "Отчёт сохранён",
                f"📊 Отчёт сохранён: {result['filename']}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ Строк КИЗов: {total_report_rows}\n"
                f"📦 Блоков: {total_report_rows}\n"
                f"🔢 Кодов: {total_report_rows}\n"
                f"├─ Терминал: {result['terminal_count']} кодов\n"
                f"├─ Перечисление: {result['transfer_count']} кодов\n"
                f"└─ Не распознано: {result['unknown_count']} кодов\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Telegram: отчёт {telegram_status}\n"
                f"{result.get('telegram_message', '')}",
            )

            self.on_close()

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
