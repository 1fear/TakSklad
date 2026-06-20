import os
import sys
import socket
import uuid

import tkinter as tk

from .catalog import load_product_catalog
from .config import *
from .app_catalog import CatalogActionsMixin
from .app_control_panel import ControlPanelMixin
from .app_day_end import DayEndActionsMixin
from .app_data_loading import DataLoadingMixin
from .app_finish import FinishActionsMixin
from .app_imports import ImportActionsMixin
from .app_layout import LayoutMixin
from .app_order_display import OrderDisplayMixin
from .app_printing import PrintingActionsMixin
from .app_returns import ReturnsActionsMixin
from .app_runtime import (
    AppRuntimeMixin,
    global_exception_handler,
    show_startup_error_message,
)
from .app_scanning import ScanningActionsMixin
from .app_skladbot import SkladBotActionsMixin
from .app_telegram import TelegramActionsMixin
from .app_updates import UpdateMixin
from .backend_client import backend_read_orders_enabled
from .desktop_refresh_service import (
    fetch_google_sheet_data,
    fetch_sheet_data,
    fetch_sheet_data_with_sync,
    format_refresh_error_message,
    backend_skladbot_sync_result,
)
from .backend_flow import (
    backend_blocked_scan_events_for_item,
    backend_sync_group_blocker,
    backend_sync_item_blocker,
    complete_backend_orders_or_raise,
    format_backend_blocked_scan_message,
    format_print_failure_after_backend_complete,
)
from .desktop_scan_rules import (
    build_product_result,
    find_code_owner_in_orders,
    first_incomplete_order_index,
    format_duplicate_scan_message,
    format_scan_product_mismatch_message,
    group_finish_blocker,
    is_terminal_scan_state,
)
from .pending_store import load_pending_saves
from .reports import create_day_report_excel
from .update_service import ensure_windows_desktop_shortcut, maybe_rename_windows_executable
from .storage import (
    credentials_available,
    migrate_legacy_json_files_to_app_data,
)
from .startup_check import log_startup_self_check
from .logging_setup import configure_app_logging

configure_app_logging(LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT)

STATUS_NOTICE_TIMEOUT_MS = 5000
PRODUCT_PHOTO_SIZE = 170
PRODUCT_PHOTO_BG = "#fffaf0"
PRODUCT_PHOTO_SHELL_BG = "#f3ead8"
UI_FONT = "Segoe UI"
TITLE_FONT = (UI_FONT, 22, "bold")
DATE_FONT = (UI_FONT, 11)
CARD_TITLE_FONT = (UI_FONT, 11, "bold")
LIST_TITLE_FONT = (UI_FONT, 13, "bold")
BODY_FONT = (UI_FONT, 10)
BODY_FONT_BOLD = (UI_FONT, 10, "bold")
SMALL_FONT = (UI_FONT, 9)
SMALL_FONT_BOLD = (UI_FONT, 9, "bold")
ENTRY_FONT = (UI_FONT, 13)
PRIMARY_LABEL_FONT = (UI_FONT, 15, "bold")
PRODUCT_LABEL_FONT = (UI_FONT, 15, "bold")
PROGRESS_FONT = (UI_FONT, 16, "bold")
KPI_FONT = (UI_FONT, 18, "bold")
KPI_LABEL_FONT = (UI_FONT, 9)
PRIMARY_BUTTON_FONT = (UI_FONT, 11, "bold")
ACTION_BUTTON_FONT = (UI_FONT, 10, "bold")


sys.excepthook = global_exception_handler


class ScanningApp(
    DataLoadingMixin,
    AppRuntimeMixin,
    UpdateMixin,
    TelegramActionsMixin,
    ImportActionsMixin,
    CatalogActionsMixin,
    ControlPanelMixin,
    PrintingActionsMixin,
    ReturnsActionsMixin,
    OrderDisplayMixin,
    ScanningActionsMixin,
    FinishActionsMixin,
    LayoutMixin,
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
        self.product_photo_image = None
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
