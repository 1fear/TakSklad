import os
import re
import sys
import logging
import threading
import time
import socket
import uuid
from datetime import datetime

import tkinter as tk
from tkinter import messagebox


from .catalog import (
    get_product_rule,
    load_product_catalog,
)
from .config import *
from .app_catalog import CatalogActionsMixin
from .app_control_panel import ControlPanelMixin
from .app_day_end import DayEndActionsMixin
from .app_imports import ImportActionsMixin
from .app_printing import PrintingActionsMixin
from .app_skladbot import SkladBotActionsMixin
from .app_telegram import TelegramActionsMixin
from .app_updates import UpdateMixin
from .backend_client import (
    backend_enabled,
    backend_read_orders_enabled,
    fetch_backend_sheet_data,
)
from .backend_events import (
    get_pending_backend_codes,
    remove_pending_backend_scan,
    queue_backend_order_complete,
    queue_backend_scan,
    sync_pending_backend_events,
)
from .orders import (
    get_order_status,
    get_plan_blocks,
    order_group_key,
)
from .pending_store import (
    add_pending_print,
    add_pending_save,
    get_pending_codes,
    is_retryable_save_error,
    load_pending_saves,
    remove_pending_print,
    sync_pending_saves,
    write_scan_backup,
)
from .printing import print_summary
from .reports import (
    build_summary_products_from_gsheet,
    create_day_report_excel,
    order_group_display_sort_key,
    unpack_order_group_key,
)
from .sheets import (
    get_all_existing_codes,
    get_today_orders,
    release_telegram_poll_lock,
    update_scanned_codes_to_gsheet,
)
from .update_service import ensure_windows_desktop_shortcut, maybe_rename_windows_executable
from .skladbot_sync import sync_skladbot_request_numbers
from .storage import (
    credentials_available,
    migrate_legacy_json_files_to_app_data,
)
from .telegram_service import (
    collect_operational_documents,
)
from .ui_widgets import AppButton
from .utils import (
    normalize_text,
    parse_int_value,
)

# Гарантируем существование папки docs/ до того, как logging откроет файл —
# иначе FileNotFoundError при первом запуске после клона/установки.
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8"
)

def format_exception_message(title, exc):
    return (
        f"{title}\n\n"
        f"Причина: {exc}\n\n"
        f"Подробности записаны в лог:\n{LOG_FILE}"
    )

def show_exception_message(title, exc):
    logging.exception(title)
    try:
        messagebox.showerror("Ошибка", format_exception_message(title, exc))
    except Exception:
        pass


def format_refresh_error_message(exc, has_cached_orders=False):
    reason = normalize_text(exc) or "ошибка без подробностей"
    if has_cached_orders:
        return (
            f"Список заказов не обновился: {reason}. "
            "Работаем с последним загруженным списком; повторите обновление позже."
        )
    return (
        f"Список заказов пока не загружен: {reason}. "
        "Проверьте связь с Google Sheets и повторите обновление."
    )


def global_exception_handler(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.error(
        "Неперехваченная ошибка",
        exc_info=(exc_type, exc_value, exc_traceback)
    )
    try:
        messagebox.showerror(
            "Критическая ошибка",
            format_exception_message("Неперехваченная ошибка", exc_value)
        )
    except Exception:
        pass

sys.excepthook = global_exception_handler

def fetch_sheet_data():
    if backend_read_orders_enabled():
        today_orders, sheet, all_existing_codes = fetch_backend_sheet_data()
        all_existing_codes.update(get_pending_backend_codes())
        return today_orders, sheet, all_existing_codes

    today_orders, sheet, all_rows = get_today_orders(apply_skladbot_filter=False, include_rows=True)
    all_existing_codes = get_all_existing_codes(sheet, all_rows=all_rows) if sheet else set()
    all_existing_codes.update(get_pending_codes())
    all_existing_codes.update(get_pending_backend_codes())
    return today_orders, sheet, all_existing_codes

def fetch_sheet_data_with_sync(sync_skladbot=True):
    if backend_read_orders_enabled():
        backend_result = sync_pending_backend_events()
        today_orders, sheet, all_existing_codes = fetch_backend_sheet_data()
        all_existing_codes.update(get_pending_backend_codes())
        sync_result = {
            "synced": 0,
            "failed": 0,
            "remaining": 0,
            "backend": backend_result,
            "skladbot": {
                "enabled": False,
                "updated": 0,
                "matched": 0,
                "not_found": 0,
                "multiple": 0,
                "errors": 0,
                "message": "Заказы загружены из backend",
            },
        }
        return today_orders, sheet, all_existing_codes, sync_result

    fallback_orders, sheet, all_rows = get_today_orders(apply_skladbot_filter=False, include_rows=True)
    today_orders = fallback_orders
    sync_result = sync_pending_saves(sheet)
    backend_result = sync_pending_backend_events() if backend_enabled() else {"enabled": False}
    sheet_rows_changed = bool(sync_result.get("synced"))
    if sync_skladbot:
        skladbot_result = sync_skladbot_request_numbers(sheet)
        sheet_rows_changed = sheet_rows_changed or bool(skladbot_result.get("updated"))
    else:
        skladbot_result = {
            "enabled": False,
            "updated": 0,
            "matched": 0,
            "not_found": 0,
            "multiple": 0,
            "errors": 0,
            "message": "SkladBot синхронизируется отдельно",
        }
    if sync_skladbot and skladbot_result.get("enabled") and not skladbot_result.get("errors"):
        today_orders, sheet, all_rows = get_today_orders(apply_skladbot_filter=False, include_rows=True)
    elif sync_result.get("synced"):
        today_orders, sheet, all_rows = get_today_orders(apply_skladbot_filter=False, include_rows=True)
    elif skladbot_result.get("errors"):
        logging.warning(
            "SkladBot недоступен, показываем активные Google-заказы без фильтра SkladBot: %s",
            skladbot_result.get("message", ""),
        )
    elif sheet_rows_changed:
        today_orders, sheet, all_rows = get_today_orders(apply_skladbot_filter=False, include_rows=True)
    all_existing_codes = get_all_existing_codes(sheet, all_rows=all_rows) if sheet else set()
    all_existing_codes.update(get_pending_codes())
    all_existing_codes.update(get_pending_backend_codes())
    sync_result["skladbot"] = skladbot_result
    sync_result["backend"] = backend_result
    return today_orders, sheet, all_existing_codes, sync_result

class ScanningApp(
    UpdateMixin,
    TelegramActionsMixin,
    ImportActionsMixin,
    CatalogActionsMixin,
    ControlPanelMixin,
    PrintingActionsMixin,
    DayEndActionsMixin,
    SkladBotActionsMixin,
    tk.Tk,
):
    def __init__(self):
        super().__init__()
        self.title(f"📦 {APP_NAME} — система учёта склада")
        self.configure(bg=BG_MAIN)
        self.geometry("1250x780")

        self.today_orders = []
        self.sheet = None
        self.all_existing_codes = set()
        self.current_legal_entity = None
        self.current_legal_entity_orders = []
        self.current_product_idx = 0
        self.current_order = None
        self.scanned_codes = []
        self.saved_codes_count = 0
        self.completed_orders = []
        self.current_legal_entity_products = []
        self.error_timer = None
        self.visible_order_groups = []
        self.current_group_key = None
        self.operation_in_progress = False
        self.operation_started_at = None
        self.operation_message = ""
        self.refresh_in_progress = False
        self.refresh_started_at = None
        self.refresh_message = ""
        self.refresh_notice_token = 0
        self.update_required = False
        self.update_info = None
        self.telegram_poll_running = False
        self.telegram_lock_owner_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.telegram_lock_owner_label = f"{socket.gethostname()} pid {os.getpid()}"
        self.telegram_lock_checked_at = 0
        self.telegram_lock_owned_until = 0
        self.telegram_lock_skip_logged_at = 0
        self.daily_report_check_running = False
        self.skladbot_sync_running = False
        self.backend_sync_running = False
        self.last_sync_result = {"synced": 0, "failed": 0, "remaining": 0}
        self.product_catalog = load_product_catalog()
        os.makedirs(BACKUP_DIR, exist_ok=True)
        os.makedirs(REPORTS_DIR, exist_ok=True)

        self._build_ui()
        self.center_window()
        self.after(100, lambda: self.scan_entry.focus_set())
        self.after(150, lambda: self.refresh_from_sheet(initial=True))
        self.after(500, self.check_pending_prints)
        self.after(1200, self.check_for_updates)
        self.after(2500, self.sync_pending_telegram_async)
        self.after(4000, self.poll_telegram_bot_async)
        self.after(12000, self.check_daily_reports_async)
        self.after(13000, self.sync_backend_events_async)
        self.after(15000, self.run_skladbot_periodic_refresh)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def load_data(self, show_empty_warning=True):
        self.today_orders, self.sheet, self.all_existing_codes = fetch_sheet_data()
        if show_empty_warning and not self.today_orders:
            messagebox.showwarning("Нет заданий",
                f"Нет заказов со статусом '{STATUS_NOT_COMPLETED}'.\n\n"
                f"Проверьте:\n"
                f"1. В таблице есть строки заказов\n"
                f"2. Колонка '{STATUS_COLUMN}' не заполнена как '{STATUS_COMPLETED}'\n"
                f"3. Колонка 'Отсканированные коды' заполнена не полностью")

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
            remaining = result.get("remaining", 0) if isinstance(result, dict) else 0
            if remaining:
                logging.info("Backend queue: осталось событий в очереди: %s", remaining)
            self.update_stats_display()

        def on_error(exc):
            logging.info("Backend queue: синхронизация отложена: %s", exc)

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
            messagebox.showwarning(
                "Нет заданий",
                f"Нет заказов со статусом '{STATUS_NOT_COMPLETED}'.\n\n"
                f"Проверьте:\n"
                f"1. В таблице есть строки заказов\n"
                f"2. Колонка '{STATUS_COLUMN}' не заполнена как '{STATUS_COMPLETED}'\n"
                f"3. Колонка 'Отсканированные коды' пустая или заполнена не полностью у активных заказов"
            )
        self.update_stats_display()

    def run_background(self, title, work, on_success=None, on_error=None, on_finally=None):
        def worker():
            try:
                result = work()
            except Exception as exc:
                logging.exception(title)

                def fail(exc=exc):
                    try:
                        if on_error:
                            on_error(exc)
                        else:
                            self.show_critical_error(title, exc)
                    finally:
                        if on_finally:
                            on_finally()

                try:
                    self.after(0, fail)
                except tk.TclError:
                    pass
                return

            def done():
                try:
                    if on_success:
                        on_success(result)
                finally:
                    if on_finally:
                        on_finally()

            try:
                self.after(0, done)
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def set_busy(self, message):
        self.operation_in_progress = True
        self.operation_started_at = time.monotonic()
        self.operation_message = normalize_text(message)
        logging.info("Операция начата: %s", self.operation_message)
        self.status_var.set(message)
        self.safe_config(self.status_label, bg=BG_MAIN, fg=FG_MUTED)

    def clear_busy(self):
        if self.operation_in_progress:
            elapsed = None
            if self.operation_started_at is not None:
                elapsed = time.monotonic() - self.operation_started_at
            if elapsed is None:
                logging.info("Операция завершена: %s", self.operation_message)
            else:
                logging.info("Операция завершена: %s (%.1f сек.)", self.operation_message, elapsed)
        self.operation_in_progress = False
        self.operation_started_at = None
        self.operation_message = ""

    def set_refresh_in_progress(self, message):
        self.refresh_in_progress = True
        self.refresh_started_at = time.monotonic()
        self.refresh_message = normalize_text(message)
        self.refresh_notice_token += 1
        notice_token = self.refresh_notice_token
        logging.info("Фоновое обновление начато: %s", self.refresh_message)
        self.status_var.set(message)
        self.safe_config(self.status_label, bg=BG_MAIN, fg=FG_MUTED)
        try:
            self.after(15000, lambda token=notice_token: self.show_refresh_long_running_notice(token))
        except tk.TclError:
            pass

    def clear_refresh_in_progress(self):
        if self.refresh_in_progress:
            elapsed = None
            if self.refresh_started_at is not None:
                elapsed = time.monotonic() - self.refresh_started_at
            if elapsed is None:
                logging.info("Фоновое обновление завершено: %s", self.refresh_message)
            else:
                logging.info("Фоновое обновление завершено: %s (%.1f сек.)", self.refresh_message, elapsed)
        self.refresh_in_progress = False
        self.refresh_started_at = None
        self.refresh_message = ""
        self.refresh_notice_token += 1

    def safe_config(self, widget, **kwargs):
        try:
            if widget is not None and widget.winfo_exists():
                widget.config(**kwargs)
        except tk.TclError:
            logging.debug("UI: виджет уже недоступен при изменении состояния", exc_info=True)

    def show_busy_error(self):
        message = "Дождитесь завершения текущей операции"
        if self.operation_message:
            message += f": {self.operation_message}"
            if self.operation_started_at is not None:
                elapsed = int(time.monotonic() - self.operation_started_at)
                message += f" ({elapsed} сек.)"
        self.show_error(message)

    def show_refresh_busy_error(self):
        message = "Обновление списка уже идёт в фоне"
        if self.refresh_started_at is not None:
            elapsed = int(time.monotonic() - self.refresh_started_at)
            message += f" ({elapsed} сек.)"
        message += ". Можно продолжать работу с уже загруженным списком."
        self.show_error(message)

    def show_refresh_long_running_notice(self, notice_token=None):
        if not self.refresh_in_progress or notice_token != self.refresh_notice_token:
            return
        elapsed = 0
        if self.refresh_started_at is not None:
            elapsed = int(time.monotonic() - self.refresh_started_at)
        self.status_var.set(
            f"⏳ Обновление списка всё ещё идёт ({elapsed} сек.). "
            "Можно продолжать работу с уже загруженным списком."
        )
        self.safe_config(self.status_label, bg=BG_MAIN, fg=FG_MUTED)
        try:
            self.after(15000, lambda token=notice_token: self.show_refresh_long_running_notice(token))
        except tk.TclError:
            pass

    def center_window(self):
        self.update_idletasks()
        x = (self.winfo_screenwidth() - self.winfo_width()) // 2
        y = (self.winfo_screenheight() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def show_error(self, message, popup=True):
        logging.warning("Ошибка для пользователя: %s", message)
        self.status_var.set(f"❌ {message}")
        self.safe_config(self.status_label, bg=ERROR_BG, fg=ERROR_FG)
        if self.error_timer:
            self.after_cancel(self.error_timer)
        self.error_timer = self.after(3000, self.clear_error)
        if popup:
            messagebox.showerror(
                "Ошибка",
                f"Причина: {message}\n\nЕсли ошибка повторяется, подробности будут в логе:\n{LOG_FILE}"
            )

    def show_critical_error(self, title, exc_or_message):
        if isinstance(exc_or_message, BaseException):
            message = str(exc_or_message)
            logging.error(
                title,
                exc_info=(type(exc_or_message), exc_or_message, exc_or_message.__traceback__)
            )
            detail = format_exception_message(title, exc_or_message)
        else:
            message = str(exc_or_message)
            logging.error("%s: %s", title, message)
            detail = f"{title}\n\nПричина: {message}\n\nПодробности записаны в лог:\n{LOG_FILE}"

        self.show_error(message, popup=False)
        messagebox.showerror("Ошибка", detail)
        self.send_telegram_alert_async(f"{APP_NAME}: ошибка приложения\n\n" + detail[:3800])
        self.send_telegram_documents_async(collect_operational_documents(include_error_log=True))

    def report_callback_exception(self, exc_type, exc_value, exc_traceback):
        logging.error(
            "Ошибка в интерфейсе",
            exc_info=(exc_type, exc_value, exc_traceback)
        )
        try:
            self.show_error(str(exc_value), popup=False)
            detail = format_exception_message("Ошибка в интерфейсе", exc_value)
            messagebox.showerror(
                "Ошибка",
                detail
            )
            self.send_telegram_alert_async(f"{APP_NAME}: ошибка интерфейса\n\n" + detail[:3800])
            self.send_telegram_documents_async(collect_operational_documents(include_error_log=True))
        except Exception:
            pass

    def clear_error(self):
        if self.update_required:
            self.status_var.set("⛔ Требуется обновление приложения")
            self.safe_config(self.status_label, bg=ERROR_BG, fg=ERROR_FG)
            self.error_timer = None
            return
        if self.refresh_in_progress:
            self.status_var.set(self.refresh_message or "⏳ Обновляю список заказов в фоне...")
            self.safe_config(self.status_label, bg=BG_MAIN, fg=FG_MUTED)
            self.error_timer = None
            return
        self.status_var.set("✅ Готов к работе")
        self.safe_config(self.status_label, bg=BG_MAIN, fg=FG_MUTED)
        self.error_timer = None

    def validate_code(self, code):
        if not code:
            return False, "Код пустой"
        if not code.startswith('01'):
            return False, "КИЗ должен начинаться с 01"
        if len(code) < KIZ_MIN_LENGTH:
            return False, f"Код слишком короткий для КИЗа (минимум {KIZ_MIN_LENGTH} символов)"
        if len(code) > KIZ_MAX_LENGTH:
            return False, f"Код слишком длинный для КИЗа (максимум {KIZ_MAX_LENGTH} символов)"
        if re.search(r'[а-яА-ЯёЁ]', code):
            return False, "Код содержит русские буквы! Используйте только латиницу"
        if re.search(r'\s', code):
            return False, "Код содержит пробелы или переносы"
        if not re.fullmatch(r'[\x1d\x21-\x7E]+', code):
            return False, "Код содержит недопустимые символы"
        return True, ""

    def undo_last_scan(self):
        if not self.ensure_update_allowed():
            return

        if self.operation_in_progress:
            self.show_busy_error()
            return

        if not self.current_order:
            self.show_error("Нет активной позиции")
            return

        if not self.scanned_codes:
            self.show_error("Нет кодов для отмены")
            return

        if len(self.scanned_codes) <= self.saved_codes_count:
            self.show_error("Нельзя отменить коды, уже записанные в Google Sheets")
            return

        removed_code = self.scanned_codes.pop()
        self.all_existing_codes.discard(removed_code)
        remove_pending_backend_scan(self.current_order, removed_code)
        write_scan_backup("undo_scan", self.current_order, code=removed_code, codes=self.scanned_codes.copy())

        plan_blocks = get_plan_blocks(self.current_order)

        scanned_count = len(self.scanned_codes)
        self.progress_label.config(text=f"{scanned_count} / {plan_blocks}")
        self.last_code_label.config(text=f"Отменён код: {removed_code[:40]}...")
        self.status_var.set(f"↩️ Отменён последний код ({scanned_count}/{plan_blocks})")

        if scanned_count < plan_blocks:
            self.next_product_btn.config(state="disabled")
            self.finish_btn.config(state="normal")

        self.scan_entry.focus_set()

    def _build_ui(self):
        main = tk.Frame(self, bg=BG_MAIN)
        main.pack(fill="both", expand=True, padx=25, pady=20)

        title = tk.Label(main, text="📦 УЧЁТ СКАНИРОВАНИЯ БЛОКОВ",
                        bg=BG_MAIN, fg=FG_TEXT, font=("Segoe UI", 24, "bold"))
        title.pack(pady=(0, 5))

        date_label = tk.Label(main, text=f"Дата: {datetime.now().strftime('%d.%m.%Y')}",
                             bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 12))
        date_label.pack(pady=(0, 20))

        content = tk.Frame(main, bg=BG_MAIN)
        content.pack(fill="both", expand=True)

        left_panel = tk.Frame(content, bg=BG_MAIN)
        left_panel.pack(side="left", fill="both", expand=True, padx=(0, 15))

        list_card = tk.Frame(left_panel, bg=BG_CARD, relief="flat", bd=1, highlightbackground=BORDER)
        list_card.pack(fill="both", expand=True)

        list_header = tk.Frame(list_card, bg=BG_CARD)
        list_header.pack(fill="x", padx=20, pady=(15, 10))

        tk.Label(list_header, text="🏢 Заказы на сегодня",
                bg=BG_CARD, fg=FG_TEXT, font=("Segoe UI", 14, "bold")).pack(side="left")

        self.refresh_btn = AppButton(list_header, text="🔄 ОБНОВИТЬ",
                                     bg=INFO, fg="white", font=("Segoe UI", 9, "bold"),
                                     command=self.refresh_from_sheet, relief="flat", cursor="hand2")
        self.refresh_btn.pack(side="right")

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh_legal_list())
        self.search_entry = tk.Entry(list_card, textvariable=self.search_var, bg=BG_MAIN, fg=FG_TEXT,
                                     font=("Segoe UI", 11), relief="flat", bd=0,
                                     highlightbackground=BORDER, highlightcolor=ACCENT,
                                     highlightthickness=1, insertbackground=FG_TEXT)
        self.search_entry.pack(fill="x", padx=15, pady=(0, 10))

        tools_frame = tk.Frame(list_card, bg=BG_CARD)
        tools_frame.pack(fill="x", padx=15, pady=(0, 10))

        self.import_btn = AppButton(
            tools_frame,
            text="📥 ИМПОРТ EXCEL",
            bg=SUCCESS,
            fg="white",
            font=("Segoe UI", 9, "bold"),
            command=self.import_excel_orders,
            relief="flat",
            cursor="hand2"
        )
        self.import_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.catalog_btn = AppButton(
            tools_frame,
            text="📦 ТОВАРЫ",
            bg=WARNING,
            fg="white",
            font=("Segoe UI", 9, "bold"),
            command=self.show_product_catalog,
            relief="flat",
            cursor="hand2"
        )
        self.catalog_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.control_btn = AppButton(
            tools_frame,
            text="📊 КОНТРОЛЬ",
            bg=INFO,
            fg="white",
            font=("Segoe UI", 9, "bold"),
            command=self.show_control_panel,
            relief="flat",
            cursor="hand2"
        )
        self.control_btn.pack(side="left", fill="x", expand=True)

        list_container = tk.Frame(list_card, bg=BG_CARD)
        list_container.pack(fill="both", expand=True, padx=15, pady=(0, 15))

        self.legal_listbox = tk.Listbox(list_container, bg=BG_CARD, fg=FG_TEXT,
                                        font=("Segoe UI", 11), selectmode=tk.SINGLE,
                                        relief="flat", selectbackground=ACCENT, selectforeground="white")
        self.legal_listbox.pack(side="left", fill="both", expand=True)

        scrollbar = tk.Scrollbar(list_container, orient="vertical", command=self.legal_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.legal_listbox.config(yscrollcommand=scrollbar.set)

        self.refresh_legal_list()

        self.select_btn = AppButton(left_panel, text="✅ ВЫБРАТЬ ЗАКАЗ",
                                   bg=ACCENT, fg="white", font=("Segoe UI", 12, "bold"),
                                   command=self.select_legal_entity, relief="flat", pady=12,
                                   cursor="hand2")
        self.select_btn.pack(pady=(15, 0), fill="x")

        right_panel = tk.Frame(content, bg=BG_MAIN)
        right_panel.pack(side="right", fill="both", expand=True, padx=(15, 0))

        info_card = tk.Frame(right_panel, bg=BG_CARD, relief="flat", bd=1, highlightbackground=BORDER)
        info_card.pack(fill="x", pady=(0, 15))

        tk.Label(info_card, text="📋 ТЕКУЩАЯ ПОЗИЦИЯ",
                bg=BG_CARD, fg=ACCENT, font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=20, pady=(15, 10))

        self.current_info = tk.Label(info_card, text="Не выбрано",
                                    bg=BG_CARD, fg=FG_TEXT, font=("Segoe UI", 11),
                                    wraplength=400, justify="left")
        self.current_info.pack(anchor="w", padx=20, pady=(0, 10))

        positions_frame = tk.Frame(info_card, bg=BG_CARD)
        positions_frame.pack(fill="x", padx=20, pady=(0, 10))

        self.position_label = tk.Label(positions_frame, text="", bg=BG_CARD, fg=WARNING, font=("Segoe UI", 11, "bold"))
        self.position_label.pack(side="left")

        progress_frame = tk.Frame(info_card, bg=BG_CARD)
        progress_frame.pack(fill="x", padx=20, pady=(0, 15))

        tk.Label(progress_frame, text="Сканирование:", bg=BG_CARD, fg=FG_MUTED, font=("Segoe UI", 11)).pack(side="left")
        self.progress_label = tk.Label(progress_frame, text="0 / 0", bg=BG_CARD, fg=SUCCESS, font=("Segoe UI", 14, "bold"))
        self.progress_label.pack(side="left", padx=(10, 0))

        scan_card = tk.Frame(right_panel, bg=BG_CARD, relief="flat", bd=1, highlightbackground=BORDER)
        scan_card.pack(fill="x", pady=(0, 15))

        tk.Label(scan_card, text="🔍 СКАНИРОВАНИЕ КОДА",
                bg=BG_CARD, fg=ACCENT, font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=20, pady=(15, 10))

        self.scan_entry = tk.Entry(scan_card, bg=BG_MAIN, fg=FG_TEXT, font=("Segoe UI", 14),
                                   relief="flat", bd=0, highlightbackground=BORDER,
                                   highlightcolor=ACCENT, highlightthickness=1,
                                   insertbackground=FG_TEXT)
        self.scan_entry.pack(fill="x", padx=20, pady=(0, 10))
        self.scan_entry.bind("<Return>", self.on_scan)

        self.last_code_label = tk.Label(scan_card, text="", bg=BG_CARD, fg=SUCCESS, font=("Segoe UI", 10))
        self.last_code_label.pack(anchor="w", padx=20, pady=(5, 5))

        actions_frame = tk.Frame(right_panel, bg=BG_MAIN)
        actions_frame.pack(fill="x", pady=(0, 15))

        self.undo_btn = AppButton(actions_frame, text="↩️ ОТМЕНИТЬ ПОСЛЕДНИЙ КОД",
                                 bg=DANGER, fg="white", font=("Segoe UI", 10, "bold"),
                                 command=self.undo_last_scan, relief="flat", state="disabled",
                                 cursor="hand2")
        self.undo_btn.pack(side="left", fill="x", expand=True, padx=(0, 10), pady=5)

        self.next_product_btn = AppButton(actions_frame, text="➡️ СЛЕДУЮЩАЯ ПОЗИЦИЯ",
                                         bg=WARNING, fg="white", font=("Segoe UI", 11, "bold"),
                                         command=self.next_product, relief="flat", state="disabled",
                                         cursor="hand2")
        self.next_product_btn.pack(side="left", fill="x", expand=True, padx=(0, 10), pady=5)

        self.finish_btn = AppButton(actions_frame, text="🏁 ЗАВЕРШИТЬ ЗАКАЗ",
                                   bg=SUCCESS, fg="white", font=("Segoe UI", 11, "bold"),
                                   command=self.finish_legal_entity, relief="flat", state="disabled",
                                   cursor="hand2")
        self.finish_btn.pack(side="right", fill="x", expand=True, padx=(10, 0), pady=5)

        stats_card = tk.Frame(right_panel, bg=BG_CARD, relief="flat", bd=1, highlightbackground=BORDER)
        stats_card.pack(fill="x")

        tk.Label(stats_card, text="📊 СТАТИСТИКА",
                bg=BG_CARD, fg=ACCENT, font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=20, pady=(15, 10))

        stats_frame = tk.Frame(stats_card, bg=BG_CARD)
        stats_frame.pack(fill="x", padx=20, pady=(0, 15))

        self.completed_count_label = tk.Label(stats_frame, text="Выполнено: 0", bg=BG_CARD, fg=FG_TEXT, font=("Segoe UI", 11))
        self.completed_count_label.pack(side="left", padx=(0, 20))

        self.total_blocks_label = tk.Label(stats_frame, text="Блоков: 0", bg=BG_CARD, fg=FG_TEXT, font=("Segoe UI", 11))
        self.total_blocks_label.pack(side="left")

        stats_frame_2 = tk.Frame(stats_card, bg=BG_CARD)
        stats_frame_2.pack(fill="x", padx=20, pady=(0, 15))

        self.active_orders_label = tk.Label(stats_frame_2, text="Активных заказов: 0", bg=BG_CARD, fg=FG_TEXT, font=("Segoe UI", 10))
        self.active_orders_label.pack(side="left", padx=(0, 20))

        self.pending_saves_label = tk.Label(stats_frame_2, text="Очередь записи: 0", bg=BG_CARD, fg=FG_TEXT, font=("Segoe UI", 10))
        self.pending_saves_label.pack(side="left")

        self.report_btn = AppButton(right_panel, text="📊 ЗАВЕРШИТЬ ДЕНЬ (ОТЧЁТ)",
                                   bg=INFO, fg="white", font=("Segoe UI", 11, "bold"),
                                   command=self.end_day, relief="flat", pady=10,
                                   cursor="hand2")
        self.report_btn.pack(fill="x", pady=(10, 0))

        status_frame = tk.Frame(main, bg=BG_MAIN)
        status_frame.pack(fill="x", pady=(20, 0))

        self.status_var = tk.StringVar(value="✅ Готов к работе")
        self.status_label = tk.Label(status_frame, textvariable=self.status_var,
                                     bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 10))
        self.status_label.pack()

        version_frame = tk.Frame(main, bg=BG_MAIN)
        version_frame.pack(fill="x", pady=(6, 0))
        tk.Label(
            version_frame,
            text=f"Версия: {APP_VERSION}",
            bg=BG_MAIN,
            fg=FG_MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left")

    def refresh_legal_list(self):
        self.legal_listbox.delete(0, tk.END)
        self.visible_order_groups = []
        grouped_orders = {}
        search_text = normalize_text(self.search_var.get()).lower() if hasattr(self, "search_var") else ""

        for order in self.today_orders:
            key = order_group_key(order)
            request_number, client, payment_type, address = unpack_order_group_key(key)
            display_request_number = request_number or "Без номера SkladBot"
            client = client or "Клиент не указан"
            payment_type = payment_type or "Оплата не указана"
            address = address or "Адрес не указан"
            search_area = " ".join([
                display_request_number,
                client,
                payment_type,
                address,
                normalize_text(order.get("Торговый представитель")),
                normalize_text(order.get("Товары")),
            ]).lower()
            if search_text and search_text not in search_area:
                continue
            grouped_orders.setdefault((request_number, client, payment_type, address), []).append(order)

        for key in sorted(grouped_orders.keys(), key=order_group_display_sort_key):
            request_number, client, payment_type, address = unpack_order_group_key(key)
            display_request_number = request_number or "Без номера SkladBot"
            count = len(grouped_orders[key])
            self.visible_order_groups.append(key)
            self.legal_listbox.insert(tk.END, f"{display_request_number} | {client} | {payment_type} | {count} поз. | {address}")
        self.update_stats_display()

    def reset_current_selection(self):
        self.current_legal_entity = None
        self.current_group_key = None
        self.current_legal_entity_orders = []
        self.current_product_idx = 0
        self.current_order = None
        self.scanned_codes = []
        self.saved_codes_count = 0
        self.current_legal_entity_products = []
        self.current_info.config(text="Не выбрано")
        self.position_label.config(text="")
        self.progress_label.config(text="0 / 0")
        self.next_product_btn.config(state="disabled")
        self.finish_btn.config(state="disabled")
        self.undo_btn.config(state="disabled")
        self.last_code_label.config(text="")

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
            if backend_result.get("enabled") and backend_read_orders_enabled():
                if backend_result.get("remaining"):
                    status_text = f"⚠️ Список из backend обновлён, очередь backend: {backend_result.get('remaining')}"
                else:
                    status_text = "✅ Список заказов обновлён из backend"
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
            lambda: fetch_sheet_data_with_sync(sync_skladbot=False),
            on_success=on_success,
            on_error=on_error,
            on_finally=on_finally
        )

    def select_legal_entity(self):
        if not self.ensure_update_allowed():
            return

        if self.operation_in_progress:
            self.show_busy_error()
            return

        if not self.today_orders:
            messagebox.showwarning("Ошибка", "Нет доступных юридических лиц!")
            return

        selection = self.legal_listbox.curselection()
        if not selection:
            messagebox.showwarning("Ошибка", "Выберите заказ из списка")
            return

        if selection[0] >= len(self.visible_order_groups):
            messagebox.showwarning("Ошибка", "Выбранный заказ не найден в списке")
            return

        selected_group = self.visible_order_groups[selection[0]]
        request_number, legal_entity, payment_type, address = unpack_order_group_key(selected_group)
        display_request_number = request_number or "Без номера SkladBot"

        self.current_legal_entity = legal_entity
        self.current_group_key = selected_group
        self.current_legal_entity_orders = [
            o for o in self.today_orders
            if order_group_key(o) == selected_group
        ]
        self.current_legal_entity_orders.sort(key=lambda order: parse_int_value(order.get("_row_number")))
        self.current_product_idx = 0
        self.scanned_codes = []
        self.current_legal_entity_products = []

        self.load_current_product()

        self.status_var.set(f"✅ Выбран заказ: {display_request_number} | {legal_entity} | {payment_type} | {address}")
        self.scan_entry.focus_set()

    def load_current_product(self):
        if self.current_product_idx >= len(self.current_legal_entity_orders):
            return

        self.current_order = self.current_legal_entity_orders[self.current_product_idx]

        plan_blocks = get_plan_blocks(self.current_order)
        pieces_per_block = get_product_rule(self.current_order.get("Товары", ""), self.product_catalog)["pieces_per_block"]

        info_text = f"""№ SkladBot: {self.current_order.get(SKLADBOT_REQUEST_NUMBER_COLUMN, '')}
🏢 Юр.лицо: {self.current_order.get('Клиент', '')}
👤 Торг.пред: {self.current_order.get('Торговый представитель', '')}
📍 Адрес: {self.current_order.get('Адрес', 'Адрес не указан')}
📦 Товар: {self.current_order.get('Товары', '')}
💳 Тип оплаты: {self.current_order.get('Тип оплаты', '')}
📦 План: {plan_blocks} блоков (1 блок = {pieces_per_block} ШТ)"""

        self.current_info.config(text=info_text)

        total_products = len(self.current_legal_entity_orders)
        self.position_label.config(text=f"Позиция {self.current_product_idx + 1} из {total_products}")

        existing_codes = self.current_order.get("_existing_scanned_codes", [])
        self.scanned_codes = existing_codes.copy()
        self.saved_codes_count = len(existing_codes)
        self.progress_label.config(text=f"{len(self.scanned_codes)} / {plan_blocks}")
        self.next_product_btn.config(state="disabled")
        self.finish_btn.config(state="normal")
        self.undo_btn.config(state="normal")
        self.scan_entry.delete(0, tk.END)
        if existing_codes:
            self.last_code_label.config(text=f"Уже записано в таблице: {len(existing_codes)} кодов")
        else:
            self.last_code_label.config(text="")
        if plan_blocks > 0 and len(self.scanned_codes) >= plan_blocks:
            self.next_product_btn.config(state="normal")
            self.finish_btn.config(state="disabled")
        self.scan_entry.focus_set()

    def on_scan(self, event=None):
        if not self.ensure_update_allowed():
            self.scan_entry.delete(0, tk.END)
            return

        if self.operation_in_progress:
            self.show_busy_error()
            self.scan_entry.delete(0, tk.END)
            return

        if not self.current_order:
            messagebox.showwarning("Ошибка", "Сначала выберите заказ")
            return

        code = self.scan_entry.get().strip()
        if not code:
            return

        is_valid, error_msg = self.validate_code(code)
        if not is_valid:
            self.show_error(error_msg)
            self.scan_entry.delete(0, tk.END)
            return

        plan_blocks = get_plan_blocks(self.current_order)
        if plan_blocks <= 0:
            self.show_error("В заказе не указано корректное 'Кол-во блок'")
            self.scan_entry.delete(0, tk.END)
            return

        if len(self.scanned_codes) >= plan_blocks:
            self.show_error(f"План выполнен! Нельзя сканировать больше {plan_blocks} блоков")
            self.scan_entry.delete(0, tk.END)
            return

        if code in self.scanned_codes:
            self.show_error("Код уже отсканирован в этой позиции")
            self.scan_entry.delete(0, tk.END)
            return

        if code in self.all_existing_codes:
            self.show_error(f"Код {code[:20]}... уже существует в Google Sheets!")
            self.log_duplicate_code_async(code)
            self.scan_entry.delete(0, tk.END)
            return

        for completed in self.completed_orders:
            if code in completed.get("Коды", []):
                self.show_error("Код уже использован в другом задании сегодня")
                self.scan_entry.delete(0, tk.END)
                return

        if not write_scan_backup("scan", self.current_order, code=code, codes=self.scanned_codes + [code]):
            self.show_error("Не удалось сохранить локальный backup. Код не принят")
            self.scan_entry.delete(0, tk.END)
            return

        self.scanned_codes.append(code)
        self.all_existing_codes.add(code)
        queue_backend_scan(self.current_order, code)
        scanned_count = len(self.scanned_codes)

        self.progress_label.config(text=f"{scanned_count} / {plan_blocks}")
        self.last_code_label.config(text=f"Последний код: {code[:40]}...")
        self.status_var.set(f"✅ Отсканирован код ({scanned_count}/{plan_blocks})")
        self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
        self.scan_entry.delete(0, tk.END)

        if scanned_count >= plan_blocks:
            self.status_var.set(f"🎯 Позиция выполнена! Нажмите 'Следующая позиция'")
            self.next_product_btn.config(state="normal")
            self.finish_btn.config(state="disabled")

        self.scan_entry.focus_set()

    def next_product(self):
        if not self.ensure_update_allowed():
            return

        if self.operation_in_progress:
            self.show_busy_error()
            return

        if not self.current_order:
            return

        plan_blocks = get_plan_blocks(self.current_order)

        scanned_count = len(self.scanned_codes)

        if scanned_count != plan_blocks:
            self.show_error(f"Отсканировано {scanned_count} из {plan_blocks} блоков. Завершите позицию!")
            return

        order = self.current_order
        scanned_codes = self.scanned_codes.copy()
        pieces_per_block = get_product_rule(order.get("Товары", ""), self.product_catalog)["pieces_per_block"]
        self.set_busy("⏳ Сохраняю КИЗы в Google Sheets...")
        self.safe_config(self.next_product_btn, state="disabled")
        self.safe_config(self.finish_btn, state="disabled")

        def work():
            ok = False
            message = "Нет подключения к Google Sheets"
            if self.sheet:
                ok, message = update_scanned_codes_to_gsheet(self.sheet, order, scanned_codes)

            if not ok:
                if not is_retryable_save_error(message):
                    raise RuntimeError(message)
                add_pending_save(order, scanned_codes, message)
                if not write_scan_backup("position_queued", order, codes=scanned_codes):
                    raise RuntimeError("Google Sheets недоступен, и локальная очередь записи не создана")
                return {"queued": True, "message": message}

            if not write_scan_backup("position_saved", order, codes=scanned_codes):
                raise RuntimeError("Коды записаны в Google Sheets, но локальный backup позиции не создан")
            return {"queued": False, "message": message}

        def on_success(result):
            product_result = {
                "Клиент": order.get('Клиент', ''),
                "Адрес": order.get('Адрес', ''),
                "Торговый представитель": order.get('Торговый представитель', ''),
                "Товары": order.get('Товары', ''),
                "Тип оплаты": order.get('Тип оплаты', ''),
                "Кол-во ШТ в блоке": pieces_per_block,
                "План": plan_blocks,
                "Отсканировано": scanned_count,
                "Коды": scanned_codes.copy()
            }
            self.current_legal_entity_products.append(product_result)
            order["Отсканированные коды"] = "\n".join(scanned_codes)
            order[STATUS_COLUMN] = get_order_status(order)
            order["_existing_scanned_codes"] = scanned_codes.copy()

            completed_result = product_result.copy()
            completed_result["План блоков"] = plan_blocks
            self.completed_orders.append(completed_result)

            self.current_product_idx += 1
            self.clear_busy()

            if self.current_product_idx < len(self.current_legal_entity_orders):
                self.load_current_product()
                if result.get("queued"):
                    self.status_var.set("⚠️ Позиция сохранена локально, отправится при обновлении")
                else:
                    self.status_var.set("✅ Позиция сохранена")
                self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
            else:
                self.finish_legal_entity(from_next_product=True)
            self.update_stats_display()

        def on_error(exc):
            self.show_critical_error("КИЗы не записаны", exc)
            self.clear_busy()
            self.safe_config(self.next_product_btn, state="normal")

        self.run_background(
            "Не удалось сохранить позицию",
            work,
            on_success=on_success,
            on_error=on_error
        )

    def finish_legal_entity(self, from_next_product=False):
        if not self.ensure_update_allowed():
            return

        if self.operation_in_progress:
            self.show_busy_error()
            return

        if not self.current_legal_entity:
            return

        if self.current_product_idx < len(self.current_legal_entity_orders):
            self.show_error("Сначала завершите все позиции по заказу!")
            return

        if not self.current_legal_entity_products:
            self.show_error("Нет завершённых позиций по заказу!")
            return

        if not self.confirm_print_settings():
            self.show_error("Печать сводного листа отменена")
            self.finish_btn.config(state="normal")
            return

        group_key = self.current_group_key
        current_products = [product.copy() for product in self.current_legal_entity_products]
        backend_order_ids = sorted({
            normalize_text(order.get("_backend_order_id"))
            for order in self.current_legal_entity_orders
            if normalize_text(order.get("_backend_order_id"))
        })
        self.set_busy("⏳ Готовлю и печатаю сводный лист...")
        self.safe_config(self.finish_btn, state="disabled")
        self.safe_config(self.next_product_btn, state="disabled")

        def work():
            first_product = current_products[0]
            address = first_product.get('Адрес', 'Адрес не указан')
            summary_products = current_products

            if self.sheet:
                sheet_products = build_summary_products_from_gsheet(
                    self.sheet,
                    group_key or order_group_key(first_product)
                )
                if sheet_products:
                    summary_products = sheet_products
                    first_product = summary_products[0]
                    address = first_product.get('Адрес', address)

            pending_print_id = add_pending_print(address, summary_products)

            printed_files = print_summary(address, summary_products)
            if not printed_files:
                raise RuntimeError("Сводочный лист не создан или не отправлен на печать")

            remove_pending_print(pending_print_id)

            if not write_scan_backup(
                "address_finished",
                first_product,
                codes=[code for product in summary_products for code in product.get("Коды", [])]
            ):
                raise RuntimeError("Сводка напечатана, но backup завершения заказа не создан")

            for backend_order_id in backend_order_ids:
                queue_backend_order_complete(backend_order_id)

            return {
                "first_product": first_product,
                "summary_products": summary_products,
                "finished_group": group_key or order_group_key(first_product),
            }

        def on_success(result):
            self.update_stats_display()

            finished_group = result["finished_group"]
            self.today_orders = [o for o in self.today_orders if order_group_key(o) != finished_group]
            self.refresh_legal_list()

            self.reset_current_selection()
            self.status_var.set("✅ Заказ завершён! Сводка отправлена на печать")
            self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
            self.sync_backend_events_async()

            if self.legal_listbox.size() > 0:
                self.legal_listbox.selection_set(0)

        def on_error(exc):
            self.show_critical_error("Не удалось завершить заказ", exc)
            self.safe_config(self.finish_btn, state="normal")

        def on_finally():
            self.clear_busy()

        self.run_background(
            "Не удалось завершить заказ",
            work,
            on_success=on_success,
            on_error=on_error,
            on_finally=on_finally
        )

    def on_close(self):
        if self.current_order and len(self.scanned_codes) > self.saved_codes_count:
            if not messagebox.askyesno(
                "Закрыть программу?",
                "Есть несохранённые сканы по текущей позиции.\n\nЗакрыть программу без завершения позиции?"
            ):
                return
        if telegram_single_listener_lock_enabled():
            try:
                release_telegram_poll_lock(self.telegram_lock_owner_id)
            except Exception:
                logging.info("Telegram: lock не освобождён при закрытии", exc_info=True)
        self.destroy()

def run_app():
    if maybe_rename_windows_executable():
        return 0

    ensure_windows_desktop_shortcut()
    migrate_legacy_json_files_to_app_data()

    if not credentials_available() and not backend_read_orders_enabled():
        messagebox.showerror("Ошибка",
            f"Не найдены учётные данные Google Sheets.\n\n"
            f"Положите credentials.json рядом с программой или перенесите его в {TAKSKLAD_DATA_FILE}")
    else:
        app = ScanningApp()
        app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(run_app())
