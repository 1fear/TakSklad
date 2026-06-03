from datetime import datetime
import tkinter as tk
from tkinter import messagebox

from .config import DANGER, ERROR_FG, FG_MUTED, SHEET_NAME, SPREADSHEET_ID
from .backend_client import backend_configured, backend_enabled
from .backend_events import load_pending_backend_events
from .orders import order_group_key
from .pending_store import load_pending_saves
from .reports import create_shift_report_excels_by_order_date
from .sheets import get_google_client
from .telegram_service import send_daily_report_result_to_telegram


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
        self.completed_count_label.config(text=f"Выполнено: {completed}")
        self.total_blocks_label.config(text=f"Блоков: {total_blocks}")
        active_groups = len({order_group_key(order) for order in self.today_orders})
        pending_saves = len(load_pending_saves())
        pending_backend = len(load_pending_backend_events())
        self.active_orders_label.config(text=f"Активных заказов: {active_groups}")
        pending_total = pending_saves + pending_backend
        if pending_total:
            self.pending_saves_label.config(text="Синхронизация: ожидает отправки")
        else:
            self.pending_saves_label.config(text="Синхронизация: OK")
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
            sheet = self.sheet
            result = create_shift_report_excels_by_order_date(sheet, scan_date=datetime.now().date())
            if result.get("empty") and not sheet:
                client = get_google_client()
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
                result = create_shift_report_excels_by_order_date(sheet, scan_date=datetime.now().date())

            result["sheet"] = sheet
            if not result.get("empty"):
                telegram_results = []
                for report in result.get("reports") or [result]:
                    ok, message, status = send_daily_report_result_to_telegram(
                        report,
                        reason="Отправлено при ручном закрытии смены",
                    )
                    telegram_results.append({
                        "filename": report.get("filename"),
                        "shipment_date_display": report.get("shipment_date_display"),
                        "part_number": report.get("part_number"),
                        "ok": ok,
                        "message": message,
                        "status": status,
                    })
                result["telegram_results"] = telegram_results
            return result

        def on_success(result):
            self.sheet = result.get("sheet") or self.sheet
            if result.get("empty"):
                messagebox.showwarning("Нет данных", "За сегодня нет отсканированных КИЗов для отчёта")
                return

            total_report_rows = result["total_report_rows"]
            reports = result.get("reports") or [result]
            telegram_results = result.get("telegram_results") or []
            report_lines = []
            for index, report in enumerate(reports, start=1):
                status = ""
                if index <= len(telegram_results):
                    status = {
                        "sent": "отправлен",
                        "queued": "в очереди Telegram",
                        "failed": "не отправлен",
                    }.get(telegram_results[index - 1].get("status"), "не отправлен")
                shipment_date = report.get("shipment_date_display") or report.get("report_date_display")
                part = report.get("part_number")
                part_text = f", ч{part}" if part else ""
                repeat_text = ", уже был сформирован" if report.get("already_exists") else ""
                report_lines.append(
                    f"- {shipment_date}{part_text}: {report.get('total_report_rows', 0)} КИЗ, {status}{repeat_text}"
                )
            messagebox.showinfo(
                "Отчёт сохранён",
                f"📊 Отчётов сохранено: {len(reports)}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ Строк КИЗов: {total_report_rows}\n"
                f"📦 Блоков: {total_report_rows}\n"
                f"🔢 Кодов: {total_report_rows}\n"
                f"├─ Терминал: {result['terminal_count']} кодов\n"
                f"├─ Перечисление: {result['transfer_count']} кодов\n"
                f"└─ Не распознано: {result['unknown_count']} кодов\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                + "\n".join(report_lines),
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
