import os
import sys
import logging
import threading
import time
import socket
import uuid
from datetime import datetime, timedelta

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
    BackendApiError,
    backend_configured,
    backend_enabled,
    backend_read_orders_enabled,
    complete_order,
    fetch_backend_sheet_data,
    fetch_returned_orders,
    lookup_return_order,
    mark_order_returned,
    sync_backend_sources,
)
from .backend_events import (
    get_pending_backend_codes,
    load_pending_backend_events,
    remove_pending_backend_scan,
    queue_backend_scan,
    queue_backend_scans_for_order,
    sync_pending_backend_events,
    undo_backend_scan,
)
from .desktop_diagnostics import log_refresh_diagnostic_summary
from .orders import (
    get_order_date_value,
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
    update_pending_save_codes_for_undo,
    write_scan_backup,
)
from .printing import print_summary
from .reports import (
    build_summary_products_from_gsheet,
    create_day_report_excel,
    order_group_display_sort_key,
    unpack_order_group_key,
)
from .scan_quantities import (
    SCAN_TYPE_AGGREGATE_BOX,
    aggregate_product_mismatch,
    scan_entries_for_order_codes,
    scan_metadata_for_code,
    scan_product_mismatch,
    scanned_blocks_for_order_codes,
)
from .sheets import (
    archive_order_group_to_gsheet,
    fetch_returned_orders_from_gsheet,
    get_all_existing_codes,
    get_today_orders,
    google_backoff_remaining,
    lookup_return_order_in_gsheet,
    mark_return_order_in_gsheet,
    release_telegram_poll_lock,
    update_scanned_codes_to_gsheet,
)
from .update_service import ensure_windows_desktop_shortcut, maybe_rename_windows_executable
from .skladbot_sync import sync_skladbot_request_numbers
from .storage import (
    credentials_available,
    migrate_legacy_json_files_to_app_data,
)
from .startup_check import format_app_version_label, log_startup_self_check
from .telegram_service import (
    collect_operational_documents,
    telegram_single_listener_lock_enabled,
)
from .ui_widgets import AppButton, RoundedNotice
from .logging_setup import configure_app_logging
from .utils import (
    normalize_kiz_code,
    normalize_text,
    parse_date_to_standard,
    parse_int_value,
    split_codes,
    validate_kiz_code,
)

configure_app_logging(LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT)

STATUS_NOTICE_TIMEOUT_MS = 5000

def format_exception_message(title, exc):
    return (
        f"{title}\n\n"
        f"Причина: {exc}\n\n"
        f"Подробности записаны в лог:\n{LOG_FILE}"
    )

def show_startup_error_message(title, message):
    try:
        root = tk.Tk()
        root.title(title)
        root.configure(bg=ERROR_BG)
        root.resizable(False, False)
        root.attributes("-topmost", True)
        tk.Label(
            root,
            text=f"❌ {message}",
            bg=ERROR_BG,
            fg=ERROR_FG,
            font=("Segoe UI", 11, "bold"),
            padx=24,
            pady=18,
            wraplength=560,
            justify="left",
        ).pack(fill="both", expand=True)
        root.update_idletasks()
        x = max((root.winfo_screenwidth() - root.winfo_width()) // 2, 0)
        y = max(root.winfo_screenheight() - root.winfo_height() - 80, 0)
        root.geometry(f"+{x}+{y}")
        root.after(STATUS_NOTICE_TIMEOUT_MS, root.destroy)
        root.mainloop()
    except Exception:
        pass

def show_exception_message(title, exc):
    logging.exception(title)
    show_startup_error_message("Ошибка", format_exception_message(title, exc))


def date_sort_key(value):
    text = normalize_text(value)
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return datetime.max


def format_order_date_header(value):
    text = parse_date_to_standard(value) or normalize_text(value) or "Без даты отгрузки"
    parsed = None
    try:
        parsed = datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        return text

    today = datetime.now().date()
    if parsed == today:
        prefix = "Сегодня"
    elif parsed == today + timedelta(days=1):
        prefix = "Завтра"
    elif parsed == today - timedelta(days=1):
        prefix = "Вчера"
    else:
        prefix = ""
    display = parsed.strftime("%d.%m.%Y")
    return f"{prefix}, {display}" if prefix else display


def format_money(value):
    amount = parse_int_value(value)
    if amount <= 0:
        return "сумма не указана"
    return f"{amount:,} сум".replace(",", " ")


def scanned_codes_for_order(order):
    return split_codes(order.get("Отсканированные коды") or "\n".join(order.get("_existing_scanned_codes") or []))


def scanned_blocks_for_order(order, codes=None):
    return scanned_blocks_for_order_codes(order, codes if codes is not None else scanned_codes_for_order(order))


def first_incomplete_order_index(orders):
    for index, order in enumerate(orders):
        plan_blocks = get_plan_blocks(order)
        if plan_blocks <= 0:
            return index
        if scanned_blocks_for_order(order) < plan_blocks:
            return index
    return len(orders)


def build_product_result(order, scanned_codes, product_catalog):
    pieces_per_block = get_product_rule(order.get("Товары", ""), product_catalog)["pieces_per_block"]
    plan_blocks = get_plan_blocks(order)
    return {
        "Дата отгрузки": get_order_date_value(order),
        "Клиент": order.get("Клиент", ""),
        "Адрес": order.get("Адрес", ""),
        "Торговый представитель": order.get("Торговый представитель", ""),
        "Товары": order.get("Товары", ""),
        "Тип оплаты": order.get("Тип оплаты", ""),
        "Кол-во ШТ в блоке": pieces_per_block,
        "План": plan_blocks,
        "Отсканировано": scanned_blocks_for_order(order, scanned_codes),
        "Сумма позиции": parse_int_value(order.get("Сумма позиции")),
        "Цена заказа": parse_int_value(order.get("Сумма позиции")),
        "Коды": list(scanned_codes),
    }


def group_finish_blocker(orders, completed_products):
    if not orders:
        return "Нет строк заказа для завершения"
    if len(completed_products) < len(orders):
        return "Сначала сохраните все позиции заказа"
    for idx, order in enumerate(orders, start=1):
        plan_blocks = get_plan_blocks(order)
        scanned_count = scanned_blocks_for_order(order)
        if plan_blocks <= 0:
            return f"В позиции {idx} не указано корректное 'Кол-во блок'"
        if scanned_count < plan_blocks:
            return f"Позиция {idx}: отсканировано {scanned_count} из {plan_blocks} блоков"
    return ""


def is_terminal_scan_state(order):
    status = normalize_text(order.get(STATUS_COLUMN)).lower().replace("ё", "е")
    return any(marker in status for marker in ("архив", "возврат", "закрыт", "closed", "returned", "archive"))


def order_uses_backend_scan_path(order):
    return bool(backend_enabled() and normalize_text(order.get("_backend_order_item_id")))


def backend_event_matches_item(item, order_item_id):
    order_item_id = normalize_text(order_item_id)
    if not order_item_id or item.get("type") != "scan":
        return False
    return normalize_text((item.get("payload") or {}).get("order_item_id")) == order_item_id


def backend_event_matches_group(item, order_item_ids, order_ids):
    event_type = item.get("type")
    payload = item.get("payload") or {}
    if event_type == "scan":
        return normalize_text(payload.get("order_item_id")) in order_item_ids
    if event_type == "order_complete":
        return normalize_text(payload.get("order_id")) in order_ids
    return False


def backend_event_error_message(item):
    return normalize_text(item.get("last_error")) or "Backend не принял событие"


def backend_event_error_detail(item):
    detail = item.get("last_error_detail")
    return detail if isinstance(detail, dict) else {}


def backend_blocked_scan_events_for_item(sync_result, order_item_id):
    return [
        item for item in (sync_result.get("blocked_events") or [])
        if backend_event_matches_item(item, order_item_id)
    ]


def backend_blocked_scan_code(item):
    return normalize_kiz_code((item.get("payload") or {}).get("code"))


def format_duplicate_scan_message(code, existing_order=None):
    code = normalize_kiz_code(code)
    existing_order = existing_order if isinstance(existing_order, dict) else {}
    client = normalize_text(existing_order.get("client") or existing_order.get("Клиент"))
    order_date = normalize_text(
        existing_order.get("order_date_display")
        or existing_order.get("order_date")
        or existing_order.get("Дата отгрузки")
    )
    product = normalize_text(existing_order.get("product") or existing_order.get("Товары"))
    request_number = normalize_text(
        existing_order.get("skladbot_request_number")
        or existing_order.get("№ SkladBot")
        or existing_order.get("SkladBot")
    )
    lines = ["КИЗ уже отсканирован в другом заказе."]
    if client:
        lines.append(f"Заказ: {client}")
    if order_date:
        lines.append(f"Дата отгрузки: {order_date}")
    if product:
        lines.append(f"Товар: {product}")
    if request_number:
        lines.append(f"SkladBot: {request_number}")
    if code:
        lines.append(f"Код: {code}")
    lines.append("Сканируйте другой КИЗ.")
    return "\n".join(lines)


def find_code_owner_in_orders(code, orders):
    code = normalize_kiz_code(code)
    if not code:
        return {}
    for order in orders or []:
        order_codes = {normalize_kiz_code(value) for value in scanned_codes_for_order(order)}
        if code not in order_codes:
            continue
        return {
            "client": order.get("Клиент", ""),
            "order_date_display": get_order_date_value(order) or "",
            "product": order.get("Товары", ""),
            "skladbot_request_number": order.get(SKLADBOT_REQUEST_NUMBER_COLUMN, ""),
        }
    return {}


def format_backend_blocked_scan_message(blocked_events):
    first_event = (blocked_events or [{}])[0]
    code = backend_blocked_scan_code(first_event)
    detail_payload = backend_event_error_detail(first_event)
    detail_message = normalize_text(detail_payload.get("message")) or backend_event_error_message(first_event)
    detail = detail_message.lower()
    suffix = f": {code[:24]}..." if code else ""
    if "already scanned in another order item" in detail or "already scanned for another order item" in detail:
        return format_duplicate_scan_message(code, detail_payload.get("existing_order"))
    if "does not match order item" in detail:
        return f"КИЗ не соответствует товару текущей позиции{suffix}"
    if "exceeds remaining order item blocks" in detail:
        return f"Код короба превышает остаток позиции{suffix}"
    return f"Backend отклонил КИЗ. Сканируйте другой код{suffix}"


def backend_sync_item_blocker(sync_result, order_item_id, pending_events):
    for item in sync_result.get("blocked_events") or []:
        if backend_event_matches_item(item, order_item_id):
            return backend_event_error_message(item)
    current_pending = [item for item in pending_events if backend_event_matches_item(item, order_item_id)]
    if current_pending:
        first_error = backend_event_error_message(current_pending[0])
        return f"Backend не принял КИЗы текущей позиции. Осталось по позиции: {len(current_pending)}. {first_error}"
    return ""


def backend_sync_group_blocker(sync_result, order_item_ids, order_ids, pending_events):
    order_item_ids = {normalize_text(item_id) for item_id in order_item_ids if normalize_text(item_id)}
    order_ids = {normalize_text(order_id) for order_id in order_ids if normalize_text(order_id)}
    for item in sync_result.get("blocked_events") or []:
        if backend_event_matches_group(item, order_item_ids, order_ids):
            return backend_event_error_message(item)
    current_pending = [
        item for item in pending_events
        if backend_event_matches_group(item, order_item_ids, order_ids)
    ]
    if current_pending:
        first_error = backend_event_error_message(current_pending[0])
        return f"Backend не принял события текущего заказа. Осталось по заказу: {len(current_pending)}. {first_error}"
    return ""


def unsaved_backend_scan_codes(order, scanned_codes):
    existing_codes = {
        normalize_kiz_code(code)
        for code in (order.get("_existing_scanned_codes") or [])
        if normalize_kiz_code(code)
    }
    return [
        code for code in scanned_codes
        if normalize_kiz_code(code) and normalize_kiz_code(code) not in existing_codes
    ]


def is_backend_order_already_completed_error(exc):
    if not isinstance(exc, BackendApiError) or exc.retryable:
        return False
    detail = normalize_text(exc.detail or exc).lower()
    return any(
        marker in detail
        for marker in (
            "already completed",
            "already complete",
            "already closed",
            "order completed",
            "order is completed",
            "order closed",
            "заказ уже заверш",
            "заказ заверш",
            "заказ уже закрыт",
            "заказ закрыт",
        )
    )


def complete_backend_orders_or_raise(order_ids):
    order_ids = [normalize_text(order_id) for order_id in order_ids if normalize_text(order_id)]
    if not order_ids:
        return {"completed": 0, "already_completed": 0}
    if not backend_configured():
        raise RuntimeError("Backend включён, но URL сервера не настроен. Заказ не архивирован.")

    result = {"completed": 0, "already_completed": 0}
    for order_id in order_ids:
        try:
            complete_order(order_id)
            result["completed"] += 1
        except BackendApiError as exc:
            if is_backend_order_already_completed_error(exc):
                result["already_completed"] += 1
                continue
            raise
    return result


def format_print_failure_after_backend_complete(exc, backend_complete_result):
    completed = int(backend_complete_result.get("completed") or 0)
    already_completed = int(backend_complete_result.get("already_completed") or 0)
    if completed or already_completed:
        return (
            "Backend уже завершил заказ, но сводный лист не напечатался. "
            "Статус на сервере не откатываю. Повторите печать через завершение заказа или очередь печати. "
            f"Причина: {exc}"
        )
    return f"Сводный лист не напечатался. Заказ не архивирован. Причина: {exc}"


def is_date_separator(value):
    return isinstance(value, tuple) and len(value) == 2 and value[0] == "__date__"


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
    show_startup_error_message(
        "Критическая ошибка",
        format_exception_message("Неперехваченная ошибка", exc_value),
    )

sys.excepthook = global_exception_handler

def fetch_google_sheet_data():
    today_orders, sheet, all_rows = get_today_orders(apply_skladbot_filter=False, include_rows=True)
    all_existing_codes = get_all_existing_codes(sheet, all_rows=all_rows) if sheet else set()
    all_existing_codes.update(get_pending_codes())
    all_existing_codes.update(get_pending_backend_codes())
    return today_orders, sheet, all_existing_codes


def fetch_sheet_data():
    if backend_read_orders_enabled():
        try:
            today_orders, sheet, all_existing_codes = fetch_backend_sheet_data()
            all_existing_codes.update(get_pending_backend_codes())
            return today_orders, sheet, all_existing_codes
        except Exception:
            logging.warning("Backend orders unavailable, fallback to Google Sheets", exc_info=True)

    try:
        return fetch_google_sheet_data()
    except Exception:
        if not backend_read_orders_enabled():
            raise
        logging.warning("Google Sheets недоступен, загружаем fallback из backend", exc_info=True)
        today_orders, sheet, all_existing_codes = fetch_backend_sheet_data()
        all_existing_codes.update(get_pending_backend_codes())
        return today_orders, sheet, all_existing_codes


def fetch_sheet_data_with_sync(sync_skladbot=True):
    if backend_read_orders_enabled():
        try:
            backend_result = sync_pending_backend_events()
        except Exception as exc:
            logging.warning("Backend queue sync failed before refresh", exc_info=True)
            backend_result = {
                "enabled": True,
                "synced": 0,
                "failed": 1,
                "remaining": len(load_pending_backend_events()),
                "message": str(exc),
            }

        try:
            sources_result = sync_backend_sources(sync_skladbot=sync_skladbot, wait_skladbot=False)
        except Exception as exc:
            logging.warning("Backend sources sync failed before backend refresh", exc_info=True)
            sources_result = {
                "status": "error",
                "message": str(exc),
                "google_sheets": {"status": "unknown", "message": str(exc)},
                "skladbot": {"status": "unknown", "message": str(exc), "errors": 1},
            }

        try:
            today_orders, sheet, all_existing_codes = fetch_backend_sheet_data()
            all_existing_codes.update(get_pending_backend_codes())
            google_queue_result = {
                "synced": 0,
                "failed": 0,
                "remaining": len(load_pending_saves()),
            }
            primary_source = "backend"
        except Exception:
            logging.warning("Backend primary refresh failed, fallback to Google Sheets", exc_info=True)
            today_orders, sheet, all_existing_codes = fetch_google_sheet_data()
            google_queue_result = {"synced": 0, "failed": 0, "remaining": len(load_pending_saves())}
            primary_source = "google_fallback"

        sync_result = {
            "synced": google_queue_result.get("synced", 0),
            "failed": google_queue_result.get("failed", 0),
            "remaining": google_queue_result.get("remaining", 0),
            "dropped": google_queue_result.get("dropped", 0),
            "backend": backend_result,
            "sources": sources_result,
            "primary_source": primary_source,
            "google_sheets": sources_result.get("google_sheets", {}) if isinstance(sources_result, dict) else {},
            "skladbot": backend_skladbot_sync_result(sources_result),
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


def backend_skladbot_sync_result(sources_result):
    if not isinstance(sources_result, dict):
        return {
            "enabled": True,
            "updated": 0,
            "matched": 0,
            "not_found": 0,
            "multiple": 0,
            "errors": 1,
            "message": "Backend sync вернул неизвестный ответ",
        }
    skladbot_result = sources_result.get("skladbot", {}) or {}
    if skladbot_result.get("status") == "skipped":
        return {
            "enabled": False,
            "updated": 0,
            "matched": 0,
            "not_found": 0,
            "multiple": 0,
            "errors": 0,
            "message": "SkladBot sync пропущен",
        }
    if skladbot_result.get("status") in {"started", "busy"}:
        return {
            "enabled": True,
            "pending": True,
            "updated": 0,
            "matched": 0,
            "not_found": 0,
            "multiple": 0,
            "errors": 0,
            "message": skladbot_result.get("message") or "SkladBot sync запущен",
        }
    return {
        "enabled": True,
        "updated": int(skladbot_result.get("updated") or 0),
        "matched": int(skladbot_result.get("matched") or 0),
        "not_found": int(skladbot_result.get("not_found") or 0),
        "multiple": int(skladbot_result.get("multiple") or 0),
        "errors": 1 if skladbot_result.get("status") == "error" else 0,
        "message": skladbot_result.get("error") or skladbot_result.get("message") or "SkladBot sync через backend",
    }

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
        self.last_completed_summary = None
        self.error_timer = None
        self.toast_visible = False
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
        self.return_lookup_result = None
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
        self.after(13000, self.sync_backend_events_async)
        self.after(15000, self.run_skladbot_periodic_refresh)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

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

    def show_status_notice(self, message, *, bg, fg, prefix, log_level=None):
        text = normalize_text(message)
        if log_level is not None:
            logging.log(log_level, "Уведомление пользователю: %s", text)
        self.status_var.set(f"{prefix} {text}" if prefix else text)
        self.safe_config(self.status_label, bg=bg, fg=fg)
        if self.error_timer:
            try:
                self.after_cancel(self.error_timer)
            except tk.TclError:
                pass
        self.error_timer = self.after(STATUS_NOTICE_TIMEOUT_MS, self.clear_error)

    def show_error_toast(self, message):
        if not hasattr(self, "error_toast"):
            return
        text = normalize_text(message)
        self.error_toast.set_text(text)
        if not self.toast_visible:
            self.error_toast.pack(fill="x", pady=(0, 8))
            self.toast_visible = True

    def hide_error_toast(self):
        if not hasattr(self, "error_toast") or not self.toast_visible:
            return
        try:
            self.error_toast.pack_forget()
        except tk.TclError:
            pass
        self.toast_visible = False

    def show_error(self, message, popup=True):
        logging.warning("Ошибка для пользователя: %s", message)
        self.show_status_notice(message, bg=ERROR_BG, fg=ERROR_FG, prefix="❌", log_level=None)
        show_toast = getattr(self, "show_error_toast", None)
        if callable(show_toast) and hasattr(self, "error_toast"):
            show_toast(message)
        if hasattr(self, "last_code_label"):
            self.safe_config(
                self.last_code_label,
                text=f"Ошибка: {normalize_text(message)}",
                fg=ERROR_FG,
            )

    def show_warning(self, message):
        self.show_status_notice(message, bg=WARNING, fg=FG_TEXT, prefix="⚠", log_level=logging.WARNING)

    def show_info(self, message):
        self.show_status_notice(message, bg=BG_MAIN, fg=FG_MUTED, prefix="✅", log_level=logging.INFO)

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

        self.show_error(f"{title}: {message}", popup=False)
        self.send_telegram_alert_async(f"{APP_NAME}: ошибка приложения\n\n" + detail[:3800])
        self.send_telegram_documents_async(collect_operational_documents(include_error_log=True))

    def report_callback_exception(self, exc_type, exc_value, exc_traceback):
        logging.error(
            "Ошибка в интерфейсе",
            exc_info=(exc_type, exc_value, exc_traceback)
        )
        try:
            self.show_error(f"Ошибка в интерфейсе: {exc_value}", popup=False)
            detail = format_exception_message("Ошибка в интерфейсе", exc_value)
            self.send_telegram_alert_async(f"{APP_NAME}: ошибка интерфейса\n\n" + detail[:3800])
            self.send_telegram_documents_async(collect_operational_documents(include_error_log=True))
        except Exception:
            pass

    def clear_error(self):
        self.hide_error_toast()
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
        is_valid, error_msg, _normalized_code = validate_kiz_code(code)
        return is_valid, error_msg

    def apply_backend_blocked_scan_events(self, blocked_events, order=None):
        order = order or self.current_order
        if not order:
            return False
        blocked_codes = [
            code for code in (backend_blocked_scan_code(item) for item in blocked_events)
            if code
        ]
        if not blocked_codes:
            return False
        blocked_set = set(blocked_codes)
        kept_codes = [
            code for code in self.scanned_codes
            if normalize_kiz_code(code) not in blocked_set
        ]
        if len(kept_codes) == len(self.scanned_codes):
            return False

        self.scanned_codes = kept_codes
        for item in blocked_events:
            code = backend_blocked_scan_code(item)
            detail = backend_event_error_message(item).lower()
            if not code:
                continue
            if "already scanned in another order item" in detail or "already scanned for another order item" in detail:
                self.all_existing_codes.add(code)
            else:
                self.all_existing_codes.discard(code)

        order["_existing_scan_entries"] = scan_entries_for_order_codes(order, self.scanned_codes)
        scanned_count = scanned_blocks_for_order(order, self.scanned_codes)
        plan_blocks = get_plan_blocks(order)
        self.safe_config(self.progress_label, text=f"{scanned_count} / {plan_blocks}")
        if scanned_count < plan_blocks:
            self.safe_config(self.next_product_btn, state="disabled")
            self.safe_config(self.finish_btn, state="disabled")
        if not write_scan_backup("backend_blocked_scan_removed", order, codes=self.scanned_codes):
            logging.warning("Backend отклонил КИЗ, но локальный backup после удаления не создан")
        self.show_error(format_backend_blocked_scan_message(blocked_events), popup=False)
        try:
            self.scan_entry.focus_set()
        except tk.TclError:
            pass
        self.update_stats_display()
        return True

    def undo_last_scan(self):
        if not self.ensure_update_allowed():
            return

        if self.operation_in_progress:
            self.show_busy_error()
            return

        if not self.current_order:
            self.show_error("Нет активной позиции")
            return

        if is_terminal_scan_state(self.current_order):
            self.show_error("Нельзя отменить код в архиве, возврате или закрытой смене")
            return

        if not self.scanned_codes:
            self.show_error("Нет кодов для отмены")
            return

        previous_codes = self.scanned_codes.copy()
        removed_code = self.scanned_codes.pop()
        remaining_codes = self.scanned_codes.copy()
        was_saved = len(self.scanned_codes) < self.saved_codes_count

        if not write_scan_backup("undo_scan", self.current_order, code=removed_code, codes=remaining_codes):
            self.scanned_codes.append(removed_code)
            self.show_error("Не удалось сохранить локальный backup отмены. Код не отменён")
            return

        pending_updated = update_pending_save_codes_for_undo(
            self.current_order,
            previous_codes,
            remaining_codes,
            "Откат последнего КИЗа в desktop",
        )

        if was_saved and order_uses_backend_scan_path(self.current_order) and not pending_updated:
            try:
                undo_backend_scan(self.current_order, removed_code)
            except Exception as exc:
                self.scanned_codes.append(removed_code)
                self.show_error(f"Не удалось отменить код в VDS: {exc}")
                return
            self.saved_codes_count = len(remaining_codes)
        elif was_saved and not self.sheet and not pending_updated:
            self.scanned_codes.append(removed_code)
            self.show_error("Нет подключения к Google Sheets для отмены уже записанного кода")
            return

        if was_saved and self.sheet and not pending_updated and not order_uses_backend_scan_path(self.current_order):
            ok, message = update_scanned_codes_to_gsheet(
                self.sheet,
                self.current_order,
                remaining_codes,
                allow_empty=True,
            )
            if not ok:
                self.scanned_codes.append(removed_code)
                self.show_error(f"Не удалось отменить код в Google Sheets: {message}")
                return
            self.saved_codes_count = len(remaining_codes)

        self.current_order["Отсканированные коды"] = "\n".join(remaining_codes)
        self.current_order["_existing_scanned_codes"] = remaining_codes.copy()
        self.current_order["_existing_scan_entries"] = scan_entries_for_order_codes(self.current_order, remaining_codes)
        self.current_order[STATUS_COLUMN] = get_order_status(self.current_order)
        self.all_existing_codes.discard(removed_code)
        remove_pending_backend_scan(self.current_order, removed_code)

        plan_blocks = get_plan_blocks(self.current_order)

        scanned_count = scanned_blocks_for_order(self.current_order, self.scanned_codes)
        self.progress_label.config(text=f"{scanned_count} / {plan_blocks}")
        self.last_code_label.config(text=f"Отменён код: {removed_code[:40]}...", fg=SUCCESS)
        self.status_var.set(f"↩️ Отменён последний код ({scanned_count}/{plan_blocks})")

        if scanned_count < plan_blocks:
            self.next_product_btn.config(state="disabled")
            self.finish_btn.config(state="disabled")
        elif self.current_product_idx >= len(self.current_legal_entity_orders) - 1:
            self.next_product_btn.config(state="disabled")
            self.finish_btn.config(state="normal")
        else:
            self.next_product_btn.config(state="normal")
            self.finish_btn.config(state="disabled")

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

        tk.Label(list_header, text="🏢 Заказы для КИЗов",
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

        self.import_btn = None
        self.catalog_btn = None
        self.control_btn = None

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

        self.returns_btn = AppButton(left_panel, text="↩ ВОЗВРАТЫ",
                                     bg=WARNING, fg=FG_TEXT, font=("Segoe UI", 12, "bold"),
                                     command=self.show_returns_window, relief="flat", pady=12,
                                     cursor="hand2")
        self.returns_btn.pack(pady=(10, 0), fill="x")

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

        self.current_client_label = tk.Label(
            info_card,
            text="",
            bg=BG_CARD,
            fg=FG_TEXT,
            font=("Segoe UI", 16, "bold"),
            wraplength=620,
            justify="left",
            anchor="w",
        )
        self.current_client_label.pack(anchor="w", fill="x", padx=20, pady=(0, 6))

        self.current_product_label = tk.Label(
            info_card,
            text="",
            bg=BG_CARD,
            fg=ACCENT,
            font=("Segoe UI", 15, "bold"),
            wraplength=620,
            justify="left",
            anchor="w",
        )
        self.current_product_label.pack(anchor="w", fill="x", padx=20, pady=(0, 10))

        self.party_summary_label = tk.Label(
            info_card,
            text="Партия не выбрана",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Segoe UI", 10, "bold"),
            wraplength=420,
            justify="left",
        )
        self.party_summary_label.pack(anchor="w", padx=20, pady=(0, 10))

        positions_frame = tk.Frame(info_card, bg=BG_CARD)
        positions_frame.pack(fill="x", padx=20, pady=(0, 10))

        self.position_label = tk.Label(positions_frame, text="", bg=BG_CARD, fg=FG_TEXT, font=("Segoe UI", 11, "bold"))
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
                                         bg=WARNING, fg=FG_TEXT, font=("Segoe UI", 11, "bold"),
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

        stats_frame_3 = tk.Frame(stats_card, bg=BG_CARD)
        stats_frame_3.pack(fill="x", padx=20, pady=(0, 15))

        self.backend_status_label = tk.Label(
            stats_frame_3,
            text="",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Segoe UI", 10, "bold"),
        )
        self.backend_status_label.pack(side="left")

        self.report_btn = AppButton(right_panel, text="📊 ЗАКРЫТЬ СМЕНУ",
                                   bg=INFO, fg="white", font=("Segoe UI", 11, "bold"),
                                   command=self.end_day, relief="flat", pady=10,
                                   cursor="hand2")
        self.report_btn.pack(fill="x", pady=(10, 0))

        status_frame = tk.Frame(main, bg=BG_MAIN)
        status_frame.pack(fill="x", pady=(20, 0))

        self.error_toast = RoundedNotice(
            status_frame,
            bg=DANGER,
            fg="white",
            font=("Segoe UI", 10, "bold"),
            radius=24,
            padx=18,
            pady=12,
        )

        self.status_var = tk.StringVar(value="✅ Готов к работе")
        self.status_label = tk.Label(status_frame, textvariable=self.status_var,
                                     bg=BG_MAIN, fg=FG_MUTED, font=("Segoe UI", 10),
                                     padx=14, pady=8, wraplength=900, justify="center")
        self.status_label.pack(fill="x")

        version_frame = tk.Frame(main, bg=BG_MAIN)
        version_frame.pack(fill="x", pady=(6, 0))
        tk.Label(
            version_frame,
            text=format_app_version_label(),
            bg=BG_MAIN,
            fg=FG_MUTED,
            font=("Segoe UI", 9),
        ).pack(side="left")

    def refresh_legal_list(self):
        self.legal_listbox.delete(0, tk.END)
        self.visible_order_groups = []
        grouped_orders = {}
        group_dates = {}
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
            group_dates.setdefault(
                (request_number, client, payment_type, address),
                parse_date_to_standard(get_order_date_value(order)) or "Без даты",
            )

        date_groups = {}
        for key in grouped_orders:
            date_groups.setdefault(group_dates.get(key, "Без даты"), []).append(key)

        for date_value in sorted(date_groups.keys(), key=date_sort_key):
            header_index = self.legal_listbox.size()
            self.visible_order_groups.append(("__date__", date_value))
            self.legal_listbox.insert(tk.END, f"  {format_order_date_header(date_value).upper()}")
            try:
                self.legal_listbox.itemconfig(header_index, fg=FG_MUTED, bg=BG_MAIN, selectbackground=BG_MAIN)
            except tk.TclError:
                pass

            for key in sorted(date_groups[date_value], key=order_group_display_sort_key):
                request_number, client, payment_type, address = unpack_order_group_key(key)
                display_request_number = request_number or "Без номера SkladBot"
                count = len(grouped_orders[key])
                self.visible_order_groups.append(key)
                self.legal_listbox.insert(
                    tk.END,
                    f"  {display_request_number} | {client} | {payment_type} | {count} поз. | {address}",
                )
        self.update_stats_display()

    def _select_first_real_order(self):
        for index, group in enumerate(self.visible_order_groups):
            if not is_date_separator(group):
                self.legal_listbox.selection_clear(0, tk.END)
                self.legal_listbox.selection_set(index)
                self.legal_listbox.activate(index)
                return True
        return False

    def _selected_order_group(self):
        selection = self.legal_listbox.curselection()
        if not selection:
            return None
        selected_index = selection[0]
        if selected_index >= len(self.visible_order_groups):
            return None
        selected_group = self.visible_order_groups[selected_index]
        if is_date_separator(selected_group):
            self.show_error("Выберите заказ под датой, а не заголовок даты", popup=False)
            return None
        return selected_group

    def show_returns_window(self):
        dialog = tk.Toplevel(self)
        dialog.title("Возвраты TakSklad")
        dialog.geometry("640x560")
        dialog.configure(bg=BG_MAIN)
        dialog.transient(self)

        container = tk.Frame(dialog, bg=BG_CARD, padx=20, pady=18)
        container.pack(fill="both", expand=True, padx=18, pady=18)

        tk.Label(
            container,
            text="ВОЗВРАТЫ",
            bg=BG_CARD,
            fg=ACCENT,
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w")
        tk.Label(
            container,
            text="Сканируйте ШК накладной или введите номер/ID заявки SkladBot.",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(4, 14))

        lookup_var = tk.StringVar()
        lookup_row = tk.Frame(container, bg=BG_CARD)
        lookup_row.pack(fill="x", pady=(0, 12))

        lookup_entry = tk.Entry(
            lookup_row,
            textvariable=lookup_var,
            bg=BG_MAIN,
            fg=FG_TEXT,
            font=("Segoe UI", 14),
            relief="flat",
            bd=0,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            highlightthickness=1,
            insertbackground=FG_TEXT,
        )
        lookup_entry.pack(side="left", fill="x", expand=True)

        lookup_btn = AppButton(
            lookup_row,
            text="НАЙТИ",
            bg=ACCENT,
            fg="white",
            font=("Segoe UI", 10, "bold"),
        )
        lookup_btn.pack(side="right", padx=(8, 0))

        result_var = tk.StringVar(value="Заказ не выбран")
        result_label = tk.Label(
            container,
            textvariable=result_var,
            bg=BG_CARD,
            fg=FG_TEXT,
            justify="left",
            anchor="nw",
            wraplength=500,
            font=("Segoe UI", 10),
        )
        result_label.pack(fill="both", expand=True, pady=(0, 12))

        tk.Label(
            container,
            text="ПОСЛЕДНИЕ ВОЗВРАТЫ",
            bg=BG_CARD,
            fg=ACCENT,
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        returns_list = tk.Listbox(
            container,
            height=6,
            bg=BG_MAIN,
            fg=FG_TEXT,
            selectbackground=ACCENT,
            selectforeground="white",
            relief="flat",
            bd=0,
            highlightbackground=BORDER,
            highlightthickness=1,
            font=("Segoe UI", 9),
        )
        returns_list.pack(fill="x", pady=(0, 12))

        actions = tk.Frame(container, bg=BG_CARD)
        actions.pack(fill="x")

        return_btn = AppButton(
            actions,
            text="ПРИНЯТЬ ВОЗВРАТ",
            bg=ACCENT,
            fg="white",
            font=("Segoe UI", 10, "bold"),
            state="disabled",
        )
        return_btn.pack(side="left", fill="x", expand=True, padx=(0, 8))

        def show_order(order):
            self.return_lookup_result = order
            total_blocks = sum(parse_int_value(item.get("quantity_blocks")) for item in order.get("items") or [])
            total_price = sum(parse_int_value(item.get("line_total")) for item in order.get("items") or [])
            already_returned = (
                normalize_text(order.get("status")).lower() == "returned"
                or normalize_text(order.get("return_status")).lower() == "returned"
            )
            returned_at = normalize_text(order.get("returned_at"))
            return_reference = normalize_text(order.get("return_reference"))
            lines = [
                f"Заявка: {order.get('skladbot_request_number') or order.get('skladbot_request_id') or 'без номера'}",
                f"Дата отгрузки: {order.get('order_date') or ''}",
                f"Клиент: {order.get('client') or ''}",
                f"Оплата: {order.get('payment_type') or ''}",
                f"Адрес: {order.get('address') or ''}",
                f"Позиций: {len(order.get('items') or [])}",
                f"Блоков: {total_blocks}",
                f"Сумма заказа: {total_price:,} сум".replace(",", " "),
            ]
            sku_lines = [
                f"- {item.get('product') or item.get('Товары') or 'SKU не указан'}: {parse_int_value(item.get('quantity_blocks') or item.get('Кол-во блок'))} блок."
                for item in order.get("items") or []
            ]
            if sku_lines:
                lines.extend(["", "Состав возврата:", *sku_lines])
            if already_returned:
                lines.extend([
                    "",
                    "Этот возврат уже принят.",
                    f"Дата возврата: {returned_at[:19] if returned_at else 'не указана'}",
                    f"Основание: {return_reference or 'не указано'}",
                ])
            result_var.set(
                "\n".join(lines),
            )
            return_btn.config(state="disabled" if already_returned else "normal")

        def return_list_line(order):
            returned_at = normalize_text(order.get("returned_at"))
            returned_date = returned_at[:10] if returned_at else ""
            request_number = order.get("skladbot_request_number") or order.get("skladbot_request_id") or "без номера"
            total_blocks = sum(parse_int_value(item.get("quantity_blocks")) for item in order.get("items") or [])
            return " | ".join([
                returned_date or "без даты",
                request_number,
                order.get("client") or "клиент не указан",
                f"{total_blocks} блок.",
            ])

        def refresh_returns_list():
            returns_list.delete(0, tk.END)
            returns_list.insert(tk.END, "Загружаю возвраты...")

            def on_success(orders):
                returns_list.delete(0, tk.END)
                orders = orders if isinstance(orders, list) else []
                if not orders:
                    returns_list.insert(tk.END, "Возвратов пока нет")
                    return
                for order in orders[:50]:
                    returns_list.insert(tk.END, return_list_line(order))

            def on_error(exc):
                returns_list.delete(0, tk.END)
                returns_list.insert(tk.END, f"Не удалось загрузить возвраты: {exc}")

            self.run_background(
                "Не удалось загрузить список возвратов",
                lambda: self.fetch_returns_for_display(limit=50),
                on_success=on_success,
                on_error=on_error,
            )

        def do_lookup(_event=None):
            lookup = normalize_text(lookup_var.get())
            if not lookup:
                result_var.set("Введите или отсканируйте номер заявки.")
                return
            return_btn.config(state="disabled")
            result_var.set("Ищу закрытую заявку в архиве...")

            def on_success(order):
                show_order(order)

            def on_error(exc):
                self.return_lookup_result = None
                result_var.set(f"Не найдено: {exc}")

            self.run_background(
                "Не удалось найти заявку для возврата",
                lambda: self.lookup_return_for_display(lookup),
                on_success=on_success,
                on_error=on_error,
            )

        lookup_btn.config(command=do_lookup)

        def do_return():
            order = self.return_lookup_result
            if not order:
                result_var.set("Сначала найдите заявку.")
                return
            confirmed_items = self.build_return_confirmed_items(order)
            if not confirmed_items:
                result_var.set("Возврат не сохранён: в заказе нет состава для подтверждения.")
                return
            if not self.show_return_confirmation_dialog(order, confirmed_items):
                return
            return_btn.config(state="disabled")
            result_var.set("Фиксирую возврат...")

            def on_success(updated_order):
                storage_name = "Google Sheets" if normalize_text(updated_order.get("source")) == "google_sheets" else "backend"
                return_request = updated_order.get("skladbot_return_request_number") or updated_order.get("skladbot_return_request_id") or "создается в фоне"
                result_var.set(
                    "Возврат принят.\n\n"
                    f"Заявка: {updated_order.get('skladbot_request_number') or updated_order.get('id')}\n"
                    f"Возврат SkladBot: {return_request}\n"
                    f"Статус сохранён в {storage_name}."
                )
                refresh_returns_list()
                self.refresh_from_sheet()

            def on_error(exc):
                result_var.set(f"Возврат не сохранён: {exc}")
                return_btn.config(state="normal")

            self.run_background(
                "Не удалось принять возврат",
                lambda: self.mark_return_for_display(order, normalize_text(lookup_var.get()), confirmed_items=confirmed_items),
                on_success=on_success,
                on_error=on_error,
            )

        return_btn.config(command=do_return)
        AppButton(
            actions,
            text="ЗАКРЫТЬ",
            bg=FG_MUTED,
            fg="white",
            font=("Segoe UI", 10, "bold"),
            command=dialog.destroy,
        ).pack(side="right", fill="x", expand=True)

        lookup_entry.bind("<Return>", do_lookup)
        lookup_entry.focus_set()
        refresh_returns_list()

    def build_return_confirmed_items(self, order):
        confirmed = []
        is_google_order = normalize_text(order.get("source")) == "google_sheets" or order.get("_row_numbers")
        for index, item in enumerate(order.get("items") or [], start=1):
            item_id = normalize_text(item.get("id") or item.get("item_id") or item.get("order_item_id") or item.get("_backend_order_item_id"))
            if not item_id and is_google_order and not normalize_text(order.get("_backend_order_id")):
                item_id = f"google_row:{index}"
            product = normalize_text(item.get("product") or item.get("sku") or item.get("Товары"))
            quantity_blocks = parse_int_value(item.get("quantity_blocks") or item.get("Кол-во блок"))
            quantity_pieces = parse_int_value(item.get("quantity_pieces") or item.get("Кол-во ШТ"))
            if not item_id or not product or quantity_blocks <= 0:
                continue
            confirmed.append({
                "item_id": item_id,
                "product": product,
                "sku": product,
                "quantity_blocks": quantity_blocks,
                "quantity_pieces": quantity_pieces,
            })
        return confirmed

    def show_return_confirmation_dialog(self, order, confirmed_items):
        dialog = tk.Toplevel(self)
        dialog.title("Подтвердить возврат")
        dialog.geometry("620x500")
        dialog.configure(bg=BG_MAIN)
        dialog.transient(self)
        dialog.grab_set()

        container = tk.Frame(dialog, bg=BG_CARD, padx=18, pady=16)
        container.pack(fill="both", expand=True, padx=14, pady=14)

        tk.Label(
            container,
            text="ПОДТВЕРЖДЕНИЕ ВОЗВРАТА",
            bg=BG_CARD,
            fg=ACCENT,
            font=("Segoe UI", 14, "bold"),
        ).pack(anchor="w")

        details = [
            f"Исходная заявка: {order.get('skladbot_request_number') or order.get('skladbot_request_id') or 'без номера'}",
            f"Дата отгрузки: {order.get('order_date') or ''}",
            f"Юр.лицо: {order.get('client') or ''}",
            f"Тип оплаты: {order.get('payment_type') or ''}",
            f"Адрес: {order.get('address') or ''}",
        ]
        tk.Label(
            container,
            text="\n".join(details),
            bg=BG_CARD,
            fg=FG_TEXT,
            justify="left",
            anchor="w",
            wraplength=560,
            font=("Segoe UI", 10),
        ).pack(fill="x", pady=(10, 12))

        tk.Label(
            container,
            text="СОСТАВ К ВОЗВРАТУ",
            bg=BG_CARD,
            fg=FG_MUTED,
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        items_list = tk.Listbox(
            container,
            height=min(10, max(4, len(confirmed_items))),
            bg=BG_MAIN,
            fg=FG_TEXT,
            relief="flat",
            bd=0,
            highlightbackground=BORDER,
            highlightthickness=1,
            font=("Segoe UI", 10),
        )
        items_list.pack(fill="both", expand=True)
        for item in confirmed_items:
            items_list.insert(
                tk.END,
                f"{item.get('product')}: {parse_int_value(item.get('quantity_blocks'))} блок.",
            )

        result = {"confirmed": False}
        actions = tk.Frame(container, bg=BG_CARD)
        actions.pack(fill="x", pady=(14, 0))

        def confirm():
            result["confirmed"] = True
            dialog.destroy()

        AppButton(
            actions,
            text="ПОДТВЕРДИТЬ ВОЗВРАТ",
            bg=ACCENT,
            fg="white",
            font=("Segoe UI", 10, "bold"),
            command=confirm,
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))
        AppButton(
            actions,
            text="ОТМЕНА",
            bg=FG_MUTED,
            fg="white",
            font=("Segoe UI", 10, "bold"),
            command=dialog.destroy,
        ).pack(side="right", fill="x", expand=True)

        dialog.wait_window()
        return bool(result["confirmed"])

    def fetch_returns_for_display(self, limit=50):
        if backend_read_orders_enabled():
            return fetch_returned_orders(limit=limit)
        return fetch_returned_orders_from_gsheet(limit=limit)

    def lookup_return_for_display(self, lookup):
        if backend_read_orders_enabled():
            return lookup_return_order(lookup)
        return lookup_return_order_in_gsheet(lookup)

    def mark_return_for_display(self, order, return_reference, confirmed_items=None):
        is_google_order = normalize_text(order.get("source")) == "google_sheets" or order.get("_row_numbers")
        backend_order_id = normalize_text(order.get("_backend_order_id"))
        if not is_google_order:
            backend_order_id = normalize_text(order.get("id") or backend_order_id)
        backend_reads_enabled = backend_read_orders_enabled()
        if backend_order_id and backend_reads_enabled:
            return mark_order_returned(
                backend_order_id,
                return_reference=return_reference,
                returned_by=self.telegram_lock_owner_label,
                confirmed_items=confirmed_items or [],
            )

        if is_google_order:
            if backend_reads_enabled:
                raise RuntimeError("Возврат нужно провести через backend/order id: у Google-заявки нет _backend_order_id.")
            updated_order = mark_return_order_in_gsheet(
                order,
                return_reference=return_reference,
                returned_by=self.telegram_lock_owner_label,
            )
            return updated_order

        return mark_order_returned(
            order.get("id"),
            return_reference=return_reference,
            returned_by=self.telegram_lock_owner_label,
            confirmed_items=confirmed_items or [],
        )

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
        self.current_client_label.config(text="")
        self.current_product_label.config(text="")
        self.party_summary_label.config(text="Партия не выбрана")
        self.position_label.config(text="")
        self.progress_label.config(text="0 / 0")
        self.next_product_btn.config(state="disabled")
        self.finish_btn.config(state="disabled")
        self.undo_btn.config(state="disabled")
        self.last_code_label.config(text="", fg=SUCCESS)

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

    def select_legal_entity(self):
        if not self.ensure_update_allowed():
            return

        if self.operation_in_progress:
            self.show_busy_error()
            return

        if not self.today_orders:
            self.show_error("Нет доступных юридических лиц!")
            return

        selected_group = self._selected_order_group()
        if not selected_group:
            self.show_error("Выберите заказ из списка")
            return
        request_number, legal_entity, payment_type, address = unpack_order_group_key(selected_group)
        display_request_number = request_number or "Без номера SkladBot"

        self.current_legal_entity = legal_entity
        self.current_group_key = selected_group
        self.current_legal_entity_orders = [
            o for o in self.today_orders
            if order_group_key(o) == selected_group
        ]
        self.current_legal_entity_orders.sort(key=lambda order: parse_int_value(order.get("_row_number")))
        self.current_product_idx = first_incomplete_order_index(self.current_legal_entity_orders)
        self.scanned_codes = []
        self.current_legal_entity_products = [
            build_product_result(order, scanned_codes_for_order(order), self.product_catalog)
            for order in self.current_legal_entity_orders[:self.current_product_idx]
        ]
        self.update_party_summary_display()

        if self.current_product_idx >= len(self.current_legal_entity_orders):
            self.current_order = None
            self.next_product_btn.config(state="disabled")
            self.finish_btn.config(state="normal")
            self.status_var.set(f"✅ Все позиции уже сохранены: {display_request_number} | {legal_entity}")
            self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
            return

        self.load_current_product()

        self.status_var.set(f"✅ Выбран заказ: {display_request_number} | {legal_entity} | {payment_type} | {address}")
        self.scan_entry.focus_set()

    def update_party_summary_display(self):
        if not self.current_legal_entity_orders:
            self.party_summary_label.config(text="Партия не выбрана")
            return

        total_positions = len(self.current_legal_entity_orders)
        total_blocks = sum(get_plan_blocks(order) for order in self.current_legal_entity_orders)
        total_sum = sum(parse_int_value(order.get("Сумма позиции")) for order in self.current_legal_entity_orders)
        request_numbers = sorted({
            normalize_text(order.get(SKLADBOT_REQUEST_NUMBER_COLUMN))
            for order in self.current_legal_entity_orders
            if normalize_text(order.get(SKLADBOT_REQUEST_NUMBER_COLUMN))
        })
        shipment_dates = sorted({
            parse_date_to_standard(get_order_date_value(order)) or normalize_text(get_order_date_value(order))
            for order in self.current_legal_entity_orders
            if normalize_text(get_order_date_value(order))
        }, key=date_sort_key)

        request_text = ", ".join(request_numbers[:2]) if request_numbers else "без номера SkladBot"
        if len(request_numbers) > 2:
            request_text += f" +{len(request_numbers) - 2}"
        date_text = ", ".join(format_order_date_header(value) for value in shipment_dates) if shipment_dates else "дата не указана"

        self.party_summary_label.config(
            text=(
                f"Партия: {total_positions} поз. · {total_blocks} блок. · {format_money(total_sum)}\n"
                f"Дата отгрузки: {date_text} · Заявка: {request_text}"
            )
        )

    def load_current_product(self):
        if self.current_product_idx >= len(self.current_legal_entity_orders):
            return

        self.current_order = self.current_legal_entity_orders[self.current_product_idx]

        plan_blocks = get_plan_blocks(self.current_order)
        pieces_per_block = get_product_rule(self.current_order.get("Товары", ""), self.product_catalog)["pieces_per_block"]
        order_sum = parse_int_value(self.current_order.get("Сумма позиции"))
        order_sum_text = f"{order_sum:,} сум".replace(",", " ") if order_sum else "не указана"
        client_text = normalize_text(self.current_order.get("Клиент")) or "Юр.лицо не указано"
        product_text = normalize_text(self.current_order.get("Товары")) or "SKU не указан"

        info_text = f"""№ SkladBot: {self.current_order.get(SKLADBOT_REQUEST_NUMBER_COLUMN, '')}
📅 Дата отгрузки: {get_order_date_value(self.current_order) or 'не указана'}
👤 Торг.пред: {self.current_order.get('Торговый представитель', '')}
📍 Адрес: {self.current_order.get('Адрес', 'Адрес не указан')}
💳 Тип оплаты: {self.current_order.get('Тип оплаты', '')}
💰 Сумма: {order_sum_text}
📦 План: {plan_blocks} блоков (1 блок = {pieces_per_block} ШТ)"""

        self.current_info.config(text=info_text)
        self.current_client_label.config(text=f"🏢 {client_text}")
        self.current_product_label.config(text=f"📦 {product_text}")

        total_products = len(self.current_legal_entity_orders)
        self.position_label.config(text=f"Позиция {self.current_product_idx + 1} из {total_products}")

        existing_codes = self.current_order.get("_existing_scanned_codes", [])
        self.scanned_codes = existing_codes.copy()
        self.saved_codes_count = len(existing_codes)
        existing_entries = self.current_order.get("_existing_scan_entries") or scan_entries_for_order_codes(self.current_order, existing_codes)
        self.current_order["_existing_scan_entries"] = existing_entries
        scanned_blocks = scanned_blocks_for_order(self.current_order, self.scanned_codes)
        self.progress_label.config(text=f"{scanned_blocks} / {plan_blocks}")
        self.next_product_btn.config(state="disabled")
        self.finish_btn.config(state="disabled")
        self.undo_btn.config(state="normal")
        self.scan_entry.delete(0, tk.END)
        if existing_codes:
            self.last_code_label.config(text=f"Уже записано: {scanned_blocks} блоков, {len(existing_codes)} кодов", fg=SUCCESS)
        else:
            self.last_code_label.config(text="", fg=SUCCESS)
        if plan_blocks > 0 and scanned_blocks >= plan_blocks:
            if self.current_product_idx >= len(self.current_legal_entity_orders) - 1:
                self.next_product_btn.config(state="disabled")
                self.finish_btn.config(state="normal")
            else:
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
            self.show_error("Сначала выберите заказ")
            self.scan_entry.delete(0, tk.END)
            return

        is_valid, error_msg, code = validate_kiz_code(self.scan_entry.get())
        if not code:
            return

        if not is_valid:
            self.show_error(error_msg)
            self.scan_entry.delete(0, tk.END)
            return

        plan_blocks = get_plan_blocks(self.current_order)
        if plan_blocks <= 0:
            self.show_error("В заказе не указано корректное 'Кол-во блок'")
            self.scan_entry.delete(0, tk.END)
            return

        scanned_before = scanned_blocks_for_order(self.current_order, self.scanned_codes)
        if scanned_before >= plan_blocks:
            self.show_error(f"План выполнен! Нельзя сканировать больше {plan_blocks} блоков")
            self.scan_entry.delete(0, tk.END)
            return

        scan_metadata = scan_metadata_for_code(code)
        block_quantity = scan_metadata["block_quantity"]
        if scan_product_mismatch(code, self.current_order.get("Товары", "")):
            self.show_error("КИЗ не соответствует товару текущей позиции")
            self.scan_entry.delete(0, tk.END)
            return
        if scan_metadata["scan_type"] == SCAN_TYPE_AGGREGATE_BOX:
            if aggregate_product_mismatch(code, self.current_order.get("Товары", "")):
                self.show_error("Код короба не соответствует товару текущей позиции")
                self.scan_entry.delete(0, tk.END)
                return
            remaining_blocks = max(0, plan_blocks - scanned_before)
            if block_quantity > remaining_blocks:
                self.show_error(f"Короб +{block_quantity} блоков превышает остаток позиции: осталось {remaining_blocks}")
                self.scan_entry.delete(0, tk.END)
                return

        if code in self.scanned_codes:
            self.show_error("Код уже отсканирован в этой позиции")
            self.scan_entry.delete(0, tk.END)
            return

        if code in self.all_existing_codes:
            existing_order = find_code_owner_in_orders(code, self.today_orders)
            self.show_error(format_duplicate_scan_message(code, existing_order))
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
        self.current_order["_existing_scan_entries"] = scan_entries_for_order_codes(self.current_order, self.scanned_codes)
        scanned_count = scanned_blocks_for_order(self.current_order, self.scanned_codes)

        self.progress_label.config(text=f"{scanned_count} / {plan_blocks}")
        if scan_metadata["scan_type"] == SCAN_TYPE_AGGREGATE_BOX:
            self.last_code_label.config(text=f"Последний код: короб +{block_quantity}: {code[:40]}...", fg=SUCCESS)
            self.status_var.set(f"✅ Отсканирован короб +{block_quantity} ({scanned_count}/{plan_blocks})")
        else:
            self.last_code_label.config(text=f"Последний код: {code[:40]}...", fg=SUCCESS)
            self.status_var.set(f"✅ Отсканирован код ({scanned_count}/{plan_blocks})")
        self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
        self.scan_entry.delete(0, tk.END)

        if scanned_count >= plan_blocks:
            if self.current_product_idx >= len(self.current_legal_entity_orders) - 1:
                self.status_var.set("🎯 Заказ выполнен! Нажмите 'ЗАВЕРШИТЬ ЗАКАЗ'")
                self.next_product_btn.config(state="disabled")
                self.finish_btn.config(state="normal")
            else:
                self.status_var.set("🎯 Позиция выполнена! Нажмите 'Следующая позиция'")
                self.next_product_btn.config(state="normal")
                self.finish_btn.config(state="disabled")

        self.scan_entry.focus_set()

    def next_product(self, finish_after_save=False):
        if not self.ensure_update_allowed():
            return

        if self.operation_in_progress:
            self.show_busy_error()
            return

        if not self.current_order:
            return

        plan_blocks = get_plan_blocks(self.current_order)

        scanned_count = scanned_blocks_for_order(self.current_order, self.scanned_codes)

        if scanned_count != plan_blocks:
            self.show_error(f"Отсканировано {scanned_count} из {plan_blocks} блоков. Завершите позицию!")
            return

        order = self.current_order
        scanned_codes = self.scanned_codes.copy()
        self.set_busy("⏳ Сохраняю КИЗы в VDS..." if finish_after_save else "⏳ Сохраняю КИЗы...")
        self.safe_config(self.next_product_btn, state="disabled")
        self.safe_config(self.finish_btn, state="disabled")

        def work():
            if order_uses_backend_scan_path(order):
                for saved_code in unsaved_backend_scan_codes(order, scanned_codes):
                    queue_backend_scan(order, saved_code)
                backend_sync_result = sync_pending_backend_events()
                blocked_events = backend_blocked_scan_events_for_item(
                    backend_sync_result,
                    order.get("_backend_order_item_id"),
                )
                if blocked_events:
                    return {"backend_blocked": True, "blocked_events": blocked_events, "backend": True}
                blocker = backend_sync_item_blocker(
                    backend_sync_result,
                    order.get("_backend_order_item_id"),
                    load_pending_backend_events(),
                )
                if blocker:
                    raise RuntimeError(blocker)
                if not write_scan_backup("position_saved_backend", order, codes=scanned_codes):
                    raise RuntimeError("Коды сохранены в backend, но локальный backup позиции не создан")
                return {"queued": False, "message": "backend_saved", "backend": True}

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
            if result.get("backend_blocked"):
                self.clear_busy()
                if not self.apply_backend_blocked_scan_events(result.get("blocked_events") or [], order=order):
                    self.show_error(format_backend_blocked_scan_message(result.get("blocked_events") or []), popup=False)
                return

            product_result = build_product_result(order, scanned_codes, self.product_catalog)
            self.current_legal_entity_products.append(product_result)
            order["Отсканированные коды"] = "\n".join(scanned_codes)
            order[STATUS_COLUMN] = get_order_status(order)
            order["_existing_scanned_codes"] = scanned_codes.copy()
            order["_existing_scan_entries"] = scan_entries_for_order_codes(order, scanned_codes)

            completed_result = product_result.copy()
            completed_result["План блоков"] = plan_blocks
            self.completed_orders.append(completed_result)

            self.current_product_idx += 1
            self.clear_busy()

            if self.current_product_idx < len(self.current_legal_entity_orders):
                self.load_current_product()
                if result.get("queued"):
                    self.status_var.set("⚠️ Позиция сохранена локально, отправится при обновлении")
                elif result.get("backend"):
                    self.status_var.set("✅ Позиция сохранена в VDS")
                else:
                    self.status_var.set("✅ Позиция сохранена")
                self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
            else:
                self.current_order = None
                self.next_product_btn.config(state="disabled")
                if finish_after_save:
                    self.finish_btn.config(state="disabled")
                    self.status_var.set("✅ КИЗы сохранены. Готовлю завершение и печать...")
                    self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
                    self.update_stats_display()
                    self.after(0, lambda: self.finish_legal_entity(from_next_product=True))
                    return
                self.finish_btn.config(state="normal")
                if result.get("queued"):
                    self.status_var.set("⚠️ Все позиции сохранены локально. Нажмите 'ЗАВЕРШИТЬ ЗАКАЗ'")
                elif result.get("backend"):
                    self.status_var.set("✅ Все позиции сохранены в VDS. Нажмите 'ЗАВЕРШИТЬ ЗАКАЗ'")
                else:
                    self.status_var.set("✅ Все позиции сохранены. Нажмите 'ЗАВЕРШИТЬ ЗАКАЗ'")
                self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
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
            if (
                not from_next_product
                and self.current_order
                and self.current_product_idx == len(self.current_legal_entity_orders) - 1
                and scanned_blocks_for_order(self.current_order, self.scanned_codes) == get_plan_blocks(self.current_order)
            ):
                self.next_product(finish_after_save=True)
                return
            self.show_error("Сначала завершите все позиции по заказу!")
            return

        if not self.current_legal_entity_products:
            self.show_error("Нет завершённых позиций по заказу!")
            return

        finish_blocker = group_finish_blocker(self.current_legal_entity_orders, self.current_legal_entity_products)
        if finish_blocker:
            self.show_error(f"Нельзя завершить заказ: {finish_blocker}")
            self.finish_btn.config(state="disabled")
            return

        group_key = self.current_group_key
        current_orders = [order.copy() for order in self.current_legal_entity_orders]
        current_products = [product.copy() for product in self.current_legal_entity_products]
        backend_order_ids = sorted({
            normalize_text(order.get("_backend_order_id"))
            for order in current_orders
            if normalize_text(order.get("_backend_order_id"))
        })
        uses_backend_finish = bool(backend_order_ids and backend_enabled())

        if self.sheet and not uses_backend_finish:
            google_pause_remaining = google_backoff_remaining()
            if google_pause_remaining > 0:
                self.show_error(
                    f"Google Sheets временно на паузе ({google_pause_remaining} сек.). "
                    "Завершение и печать запустятся после паузы."
                )
                self.finish_btn.config(state="normal")
                return

        if not self.confirm_print_settings():
            self.show_error("Печать сводного листа отменена")
            self.finish_btn.config(state="normal")
            return

        self.set_busy("⏳ Печатаю сводный лист и завершаю заказ...")
        self.safe_config(self.finish_btn, state="disabled")
        self.safe_config(self.next_product_btn, state="disabled")

        def work():
            first_product = current_products[0]
            address = first_product.get('Адрес', 'Адрес не указан')
            summary_products = current_products
            backend_complete_result = {"completed": 0, "already_completed": 0}

            if self.sheet and not uses_backend_finish:
                sheet_products = build_summary_products_from_gsheet(
                    self.sheet,
                    group_key or order_group_key(first_product)
                )
                if sheet_products:
                    summary_products = sheet_products
                    first_product = summary_products[0]
                    address = first_product.get('Адрес', address)

            pending_print_id = add_pending_print(address, summary_products)

            try:
                printed_files = print_summary(address, summary_products)
                if not printed_files:
                    raise RuntimeError("Сводочный лист не создан или не отправлен на печать")
            except Exception as exc:
                raise RuntimeError(
                    f"Сводный лист не напечатался. Заказ не завершён в backend. Причина: {exc}"
                ) from exc

            remove_pending_print(pending_print_id)

            if uses_backend_finish:
                backend_sync_result = sync_pending_backend_events()
                order_item_ids = {
                    normalize_text(order.get("_backend_order_item_id"))
                    for order in current_orders
                    if normalize_text(order.get("_backend_order_item_id"))
                }
                blocker = backend_sync_group_blocker(
                    backend_sync_result,
                    order_item_ids,
                    set(backend_order_ids),
                    load_pending_backend_events(),
                )
                if blocker:
                    raise RuntimeError(
                        "Сводный лист напечатан, но backend не принял все КИЗы. "
                        f"{blocker}"
                    )
                backend_complete_result = complete_backend_orders_or_raise(backend_order_ids)

            if self.sheet and not (backend_order_ids and backend_enabled()):
                ok, archive_message = archive_order_group_to_gsheet(
                    self.sheet,
                    current_orders,
                )
                if not ok:
                    raise RuntimeError(archive_message)

            if not write_scan_backup(
                "address_finished",
                first_product,
                codes=[code for product in summary_products for code in product.get("Коды", [])]
            ):
                raise RuntimeError("Сводка напечатана, но backup завершения заказа не создан")

            if not (backend_order_ids and backend_enabled()):
                for order in current_orders:
                    queue_backend_scans_for_order(order)

            return {
                "first_product": first_product,
                "summary_products": summary_products,
                "finished_group": group_key or order_group_key(first_product),
                "finished_row_numbers": [
                    parse_int_value(order.get("_row_number"))
                    for order in current_orders
                    if parse_int_value(order.get("_row_number"))
                ],
            }

        def on_success(result):
            self.update_stats_display()

            finished_group = result["finished_group"]
            finished_row_numbers = set(result.get("finished_row_numbers") or [])
            if finished_row_numbers:
                self.today_orders = [
                    order
                    for order in self.today_orders
                    if parse_int_value(order.get("_row_number")) not in finished_row_numbers
                ]
            else:
                self.today_orders = [o for o in self.today_orders if order_group_key(o) != finished_group]
            self.refresh_legal_list()

            self.reset_current_selection()
            self.status_var.set("✅ Заказ завершён! Сводка отправлена на печать")
            self.status_label.config(bg=BG_MAIN, fg=FG_MUTED)
            self.sync_backend_events_async()

            self._select_first_real_order()

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
    log_startup_self_check()

    if not credentials_available() and not backend_read_orders_enabled():
        show_startup_error_message(
            "Ошибка",
            f"Не найдены учётные данные Google Sheets.\n\n"
            f"Положите credentials.json рядом с программой или перенесите его в {TAKSKLAD_DATA_FILE}",
        )
    else:
        app = ScanningApp()
        app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(run_app())
