import logging
import hashlib
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

from . import skladbot_daily_report
from .daily_report_config import (
    DailyReportConfigurationError,
    validate_daily_report_schedule_config,
    validate_production_daily_report_config,
)
from .redaction import redact_secrets
from .reconciliation_service import run_daily_reconciliation
from .skladbot_client import parse_skladbot_api_tokens
from .telegram_admin_processor import TelegramAdminProcessor
from .telegram_clients import TelegramProcessorPorts, telegram_main_reply_keyboard
from .telegram_common import (
    normalize_text,
    telegram_inline_keyboard,
    text_matches,
    parse_date_from_text,
    parse_dates_from_text,
    parse_int,
    format_money,
    iso_date_from_display,
    display_date,
)
from .telegram_manual_support import (
    TELEGRAM_MANUAL_BLOCK_PRICE,
    TELEGRAM_MANUAL_PIECES_PER_BLOCK,
    TELEGRAM_MANUAL_PRODUCTS,
    TELEGRAM_MANUAL_PAYMENT_TYPES,
    telegram_manual_menu_keyboard,
    telegram_manual_payment_keyboard,
    telegram_manual_product_keyboard,
    telegram_manual_add_next_keyboard,
    telegram_manual_delete_keyboard,
    telegram_manual_delete_confirm_keyboard,
    manual_address_and_coordinates,
    order_scanned_blocks,
    order_planned_blocks,
    manual_order_summary,
    build_manual_import_payload,
)
from .telegram_scheduled_report_processor import (
    TelegramScheduledReportProcessor,
    command_date_or_today,
    coerce_report_date,
    ensure_aware_utc,
    skladbot_reported_request_key,
    skladbot_report_version,
    mark_skladbot_daily_report_requests_reported,
    scheduled_skladbot_daily_report_blocker,
    manual_skladbot_daily_partial_warning,
    manual_skladbot_daily_partial_override_warning,
    scheduled_skladbot_daily_report_payload_key_is_safe,
    safe_scheduled_skladbot_daily_report_payload,
)
from .telegram_import_processor import (
    TelegramImportProcessor,
    safe_telegram_spreadsheet_filename,
    telegram_import_failure_message,
    telegram_import_unconfirmed_message,
    ensure_telegram_import_event_incident,
    find_existing_telegram_import_event,
    telegram_import_date_choice_keyboard,
)
from .telegram_report_processor import (
    TelegramReportProcessor,
    kiz_progress_completed,
    recent_logistics_dates_for_menu,
    kiz_dates_for_menu,
    kiz_date_range_for_menu,
    kiz_source_file_uploaded_at,
    kiz_source_file_is_telegram_upload,
    recent_kiz_source_files_for_menu,
    backend_http_error_detail,
    backend_failure_message,
    summarize_active_orders_by_date,
)
from .telegram_transfer_kiz_processor import TelegramTransferKizProcessor


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TELEGRAM_BUTTON_SHIPMENT_DATE = "Дата отгрузки"
TELEGRAM_BUTTON_LOGISTICS_REPORT = "Отчёт логистики"
TELEGRAM_BUTTON_KIZ_BY_FILES = "Выгрузка КИЗов"
TELEGRAM_BUTTON_STATUS = "Статус"
TELEGRAM_BUTTON_MENU = "Меню"
TELEGRAM_BUTTON_IMPORTS = "Последние импорты"
TELEGRAM_BUTTON_MANUAL = "Ручное управление"
TELEGRAM_LOGISTICS_DATE_PREFIX = "Логистика "
TELEGRAM_KIZ_FILE_PREFIX = "КИЗ файл "
TELEGRAM_KIZ_DATE_PREFIX = "КИЗ дата "
TELEGRAM_KIZ_RANGE_CALLBACK_PREFIX = "kiz_range:"
TELEGRAM_MENU_CALLBACK_PREFIX = "menu:"
TELEGRAM_MANUAL_CALLBACK_PREFIX = "manual:"
TELEGRAM_EXCEL_IMPORT_EVENT_TYPE = "telegram_excel_import"
TELEGRAM_NOTIFICATION_EVENT_TYPE = "telegram_notification"
TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS = "waiting_shipment_date"
TELEGRAM_EXCEL_IMPORT_WAITING_DATE_CHOICE_STATUS = "waiting_date_choice"
TELEGRAM_EXCEL_IMPORT_ACTIVE_STATUSES = ("pending",)
TELEGRAM_NOTIFICATION_ACTIVE_STATUSES = ("pending", "failed")
TELEGRAM_CHAT_STATE_EVENT_PREFIX = "telegram_chat_state:"
TELEGRAM_EXCEL_DATE_CHOICE_USE_EXCEL_PREFIX = "excel_date:use_excel:"
TELEGRAM_EXCEL_DATE_CHOICE_CANCEL_PREFIX = "excel_date:cancel:"
SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE = "skladbot_daily_report_send"
SKLADBOT_DAILY_REPORTED_REQUEST_EVENT_TYPE = "skladbot_daily_reported_request"
SKLADBOT_DAILY_REPORT_STALE_TTL_MINUTES_ENV = "SKLADBOT_DAILY_REPORT_STALE_TTL_MINUTES"
SKLADBOT_DAILY_REPORT_STALE_FAILED_ERROR = "STUCK_PROCESSING_AFTER_TTL"
SKLADBOT_DAILY_REPORT_COVERAGE_FAILED_ERROR = "SKLADBOT_DAILY_REPORT_COVERAGE_NOT_COMPLETE"
SCHEDULED_DAILY_PAYLOAD_SECRET_KEY_PARTS = (
    "chat",
    "token",
    "secret",
    "password",
    "authorization",
    "credential",
    "api_key",
    "apikey",
    "jwt",
    "raw",
    "payload",
)
TELEGRAM_DATE_MENU_RECENT_LIMIT = 7




def parse_chat_ids(value):
    return {
        part.strip()
        for part in str(value or "").replace(";", ",").split(",")
        if part.strip()
    }


class TelegramConfigurationError(RuntimeError):
    def __init__(self, setting_names):
        self.setting_names = tuple(sorted({str(name) for name in setting_names if str(name)}))
        super().__init__("Invalid Telegram configuration: " + ", ".join(self.setting_names))


def validate_telegram_worker_config(
    token,
    allowed_chat_ids,
    admin_chat_ids,
    scheduled_chat_ids=(),
    reconciliation_chat_ids=(),
    *,
    environment="",
    daily_report_enabled=False,
    skladbot_api_tokens=(),
    daily_report_environ=None,
    automation_alert_chat_id="",
):
    production = normalize_text(environment).casefold() == "production"
    if not normalize_text(token) and not production:
        return True
    errors = []
    allowed = {str(value) for value in allowed_chat_ids or ()}
    admins = {str(value) for value in admin_chat_ids or ()}
    scheduled = {str(value) for value in scheduled_chat_ids or ()}
    reconciliation = {str(value) for value in reconciliation_chat_ids or ()}
    automation_alert = normalize_text(automation_alert_chat_id)
    if production:
        contract_environ = daily_report_environ or {
            "TAKSKLAD_ENV": environment,
            "TAKSKLAD_TIMEZONE": "Asia/Tashkent",
            "TELEGRAM_BOT_TOKEN": token,
            "TELEGRAM_ALLOWED_CHAT_IDS": ",".join(sorted(allowed)),
            "SKLADBOT_DAILY_REPORT_ENABLED": "true" if daily_report_enabled else "false",
            "SKLADBOT_DAILY_REPORT_CHAT_IDS": ",".join(sorted(scheduled)),
            "SKLADBOT_API_TOKENS": ",".join(tuple(skladbot_api_tokens or ())),
        }
        try:
            validate_production_daily_report_config(contract_environ)
        except DailyReportConfigurationError as exc:
            errors.extend(exc.setting_names)
    if not allowed:
        if normalize_text(token) or production:
            errors.append("TELEGRAM_ALLOWED_CHAT_IDS")
    for setting_name, values in (
        ("TELEGRAM_ALLOWED_CHAT_IDS", allowed),
        ("TELEGRAM_ADMIN_CHAT_IDS", admins),
        ("SKLADBOT_DAILY_REPORT_CHAT_IDS", scheduled),
        ("TAKSKLAD_DAILY_RECONCILIATION_CHAT_IDS", reconciliation),
    ):
        if any(not value or not value.lstrip("-").isdigit() or int(value) == 0 for value in values):
            errors.append(setting_name)
    if not admins.issubset(allowed):
        errors.append("TELEGRAM_ADMIN_CHAT_IDS")
    if not scheduled.issubset(allowed):
        errors.append("SKLADBOT_DAILY_REPORT_CHAT_IDS")
    if not reconciliation.issubset(admins):
        errors.append("TAKSKLAD_DAILY_RECONCILIATION_CHAT_IDS")
    if automation_alert and (
        not automation_alert.isdigit()
        or int(automation_alert) <= 0
        or automation_alert not in admins
    ):
        errors.append("TAKSKLAD_AUTOMATION_ALERT_CHAT_ID")
    if production and not automation_alert:
        errors.append("TAKSKLAD_AUTOMATION_ALERT_CHAT_ID")
    if errors:
        raise TelegramConfigurationError(errors)
    return True


def parse_bool_flag(value, default=False):
    text = normalize_text(value).casefold()
    if not text:
        return default
    return text in {"1", "true", "yes", "on", "да"}




















































































def json_dumps(value):
    return json.dumps(value, ensure_ascii=False)




class TelegramWorker:
    def __init__(
        self,
        *,
        backend_token=None,
        telegram_api_client=None,
        backend_api_client=None,
        session_factory=None,
        excel_import_parser=None,
        skladbot_report_module=None,
        daily_reconciliation_callback=None,
    ):
        self.token = normalize_text(os.environ.get("TELEGRAM_BOT_TOKEN"))
        self.allowed_chat_ids = parse_chat_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS"))
        self.admin_chat_ids = parse_chat_ids(os.environ.get("TELEGRAM_ADMIN_CHAT_IDS"))
        self.automation_alert_chat_id = normalize_text(
            os.environ.get("TAKSKLAD_AUTOMATION_ALERT_CHAT_ID")
        )
        self.backend_url = normalize_text(os.environ.get("TAKSKLAD_BACKEND_INTERNAL_URL")) or "http://backend-api:8000"
        self.backend_token = normalize_text(backend_token) or normalize_text(
            os.environ.get("TAKSKLAD_API_TOKEN")
        )
        self.timeout = int(os.environ.get("TELEGRAM_WORKER_TIMEOUT_SECONDS", "20") or "20")
        self.import_timeout = int(os.environ.get("TELEGRAM_WORKER_IMPORT_TIMEOUT_SECONDS", "120") or "120")
        self.file_timeout = int(os.environ.get("TELEGRAM_WORKER_FILE_TIMEOUT_SECONDS", "120") or "120")
        self.poll_timeout = int(os.environ.get("TELEGRAM_WORKER_POLL_TIMEOUT_SECONDS", "15") or "15")
        self.max_file_size = int(os.environ.get("TELEGRAM_WORKER_MAX_FILE_BYTES", str(20 * 1024 * 1024)) or 0)
        self.environment = normalize_text(os.environ.get("TAKSKLAD_ENV")) or "local"
        try:
            daily_report_schedule = validate_daily_report_schedule_config(os.environ)
        except DailyReportConfigurationError as exc:
            raise TelegramConfigurationError(exc.setting_names) from exc
        self.skladbot_daily_report_enabled = parse_bool_flag(os.environ.get("SKLADBOT_DAILY_REPORT_ENABLED"))
        self.skladbot_daily_report_chat_ids = parse_chat_ids(os.environ.get("SKLADBOT_DAILY_REPORT_CHAT_IDS"))
        self.skladbot_daily_report_hour = daily_report_schedule.hour
        self.skladbot_daily_report_minute = daily_report_schedule.minute
        self.skladbot_daily_report_retry_minutes = daily_report_schedule.retry_minutes
        self.skladbot_daily_report_max_attempts = daily_report_schedule.max_attempts
        self.skladbot_daily_report_lookback_days = daily_report_schedule.lookback_days
        self.daily_reconciliation_enabled = parse_bool_flag(os.environ.get("TAKSKLAD_DAILY_RECONCILIATION_ENABLED"), default=True)
        self.daily_reconciliation_chat_ids = parse_chat_ids(os.environ.get("TAKSKLAD_DAILY_RECONCILIATION_CHAT_IDS"))
        validate_telegram_worker_config(
            self.token,
            self.allowed_chat_ids,
            self.admin_chat_ids,
            self.skladbot_daily_report_chat_ids,
            self.daily_reconciliation_chat_ids,
            environment=self.environment,
            daily_report_enabled=self.skladbot_daily_report_enabled,
            skladbot_api_tokens=parse_skladbot_api_tokens(),
            daily_report_environ=os.environ,
            automation_alert_chat_id=self.automation_alert_chat_id,
        )
        self._initialize_processors(
            telegram_api_client=telegram_api_client,
            backend_api_client=backend_api_client,
            session_factory=session_factory,
            excel_import_parser=excel_import_parser,
            skladbot_report_module=skladbot_report_module,
            daily_reconciliation_callback=daily_reconciliation_callback,
        )
        self.offset = self.load_offset() or int(os.environ.get("TELEGRAM_WORKER_INITIAL_OFFSET", "0") or "0")
        self.bot_menu_ready = False
        self._processor_ports.bot_menu_ready = False
        self.manual_flow_cache = {}
        self._poll_processors_ready = True

    @property
    def configured(self):
        return bool(getattr(self, "token", ""))

    def _initialize_processors(self, **dependencies):
        ports = TelegramProcessorPorts(
            telegram_api_client=dependencies.get("telegram_api_client", self.__dict__.get("telegram_api_client")),
            backend_api_client=dependencies.get("backend_api_client", self.__dict__.get("backend_api_client")),
            session_factory=dependencies.get("session_factory", self.__dict__.get("session_factory")),
            excel_import_parser=dependencies.get("excel_import_parser", self.__dict__.get("excel_import_parser")),
            skladbot_report_module=dependencies.get("skladbot_report_module", self.__dict__.get("skladbot_report_module")),
            daily_reconciliation_callback=dependencies.get(
                "daily_reconciliation_callback", self.__dict__.get("daily_reconciliation_callback"),
            ),
            token=self.__dict__.get("token", ""),
            backend_url=self.__dict__.get("backend_url", "http://backend-api:8000"),
            backend_token=self.__dict__.get("backend_token", ""),
            timeout=self.__dict__.get("timeout", 20),
            import_timeout=self.__dict__.get("import_timeout", 120),
            file_timeout=self.__dict__.get("file_timeout", 120),
            max_file_size=self.__dict__.get("max_file_size", 20 * 1024 * 1024),
            allowed_chat_ids=self.__dict__.get("allowed_chat_ids", set()),
            admin_chat_ids=self.__dict__.get("admin_chat_ids", set()),
            owner=self,
        )
        ports.bot_menu_ready = bool(self.__dict__.get("bot_menu_ready", False))
        scheduled = TelegramScheduledReportProcessor(ports=ports, owner=self)
        admin = TelegramAdminProcessor(ports=ports, owner=self)
        importer = TelegramImportProcessor(ports=ports, owner=self)
        report = TelegramReportProcessor(ports=ports, owner=self)
        ports.get_chat_state = admin.get_chat_state
        ports.save_chat_state = admin.save_chat_state
        ports.get_chat_shipment_date = admin.get_chat_shipment_date
        self._processor_ports = ports
        transfer_kiz = TelegramTransferKizProcessor(ports=ports, owner=self)
        self._processors = (scheduled, admin, importer, report, transfer_kiz)

    def __getattr__(self, name):
        if name.startswith("_processor"):
            raise AttributeError(name)
        if "_processors" not in self.__dict__:
            self._initialize_processors()
        for processor in self._processors:
            if name in type(processor).__dict__:
                return getattr(processor, name)
        if hasattr(TelegramProcessorPorts, name) or name in self._processor_ports.__dict__:
            return getattr(self._processor_ports, name)
        raise AttributeError(name)























































































    def poll_once(self):
        if not self.configured:
            logging.info("Telegram worker disabled: TELEGRAM_BOT_TOKEN is not configured")
            return

        self.ensure_bot_menu()
        poll_timeout = max(1, min(self.poll_timeout, max(1, self.timeout - 5)))
        try:
            updates = self.poll_updates(self.offset, poll_timeout)
        except RuntimeError as exc:
            if "getUpdates" not in normalize_text(exc) or "HTTP 409" not in normalize_text(exc):
                raise
            logging.warning("Telegram worker: getUpdates conflict, scheduled jobs will still run")
            updates = []
        for update in updates:
            self.offset = max(self.offset, int(update.get("update_id") or 0))
            try:
                self.handle_update(update)
            except Exception as exc:
                logging.exception("Telegram worker: update handling failed")
                self.notify_update_error(update, exc)
        if updates:
            self.save_offset()
        if self.__dict__.get("_poll_processors_ready"):
            self.process_pending_transfer_kiz_completions()
            self.process_pending_transfer_kiz_deliveries()
        self.process_queued_telegram_imports()
        self.process_pending_telegram_notifications()
        self.send_due_skladbot_daily_reports()

    def notify_update_error(self, update, exc):
        callback_query = update.get("callback_query") or {}
        message = update.get("message") or callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if not chat_id:
            return
        if not self.is_admin_chat(chat_id):
            return
        reason = redact_secrets(normalize_text(exc))
        if len(reason) > 500:
            reason = reason[:500] + "..."
        self.safe_send_message(
            chat_id,
            "\n".join([
                "Не удалось выполнить действие Telegram.",
                "",
                f"Причина: {reason or exc.__class__.__name__}",
                "",
                "Попробуйте повторить действие. Если ошибка повторится, скачайте диагностику командой /logs.",
            ]),
        )

    def handle_update(self, update):
        callback_query = update.get("callback_query") or {}
        if callback_query:
            self.handle_callback_query(callback_query)
            return

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if not self.is_allowed_chat(chat_id):
            logging.warning("Telegram worker denied unauthorized chat")
            return
        if not self.is_admin_chat(chat_id):
            logging.info("Telegram worker ignored inbound message from outbound-only chat")
            return

        text = normalize_text(message.get("text"))
        if text_matches(text, "/start", "/help", "/menu", TELEGRAM_BUTTON_MENU, "меню"):
            self.send_main_menu(
                chat_id,
                "\n".join([
                    "TakSklad backend online.",
                    "",
                    "Выберите действие кнопкой ниже или командой Telegram.",
                ]),
            )
            return
        if text_matches(text, TELEGRAM_BUTTON_MANUAL, "/manual"):
            self.show_manual_menu(chat_id)
            return
        if text_matches(text, TELEGRAM_BUTTON_SHIPMENT_DATE, "/date"):
            self.send_date_help(chat_id)
            return
        if text.startswith("/date ") or parse_date_from_text(text) == text:
            if text and self.handle_manual_text(chat_id, text):
                return
            shipment_date = parse_date_from_text(text)
            if shipment_date:
                if self.confirm_waiting_telegram_import_shipment_date(chat_id, shipment_date):
                    return
                if not self.ensure_admin_chat(chat_id):
                    return
                self.set_chat_shipment_date(chat_id, shipment_date)
                self.send_message(
                    chat_id,
                    "\n".join([
                        f"Дата сохранена: {shipment_date}",
                        "Для Excel-импорта бот всё равно спросит дату после загрузки файла.",
                    ]),
                )
                return
        if text_matches(text, "/logistics", TELEGRAM_BUTTON_LOGISTICS_REPORT):
            self.show_logistics_dates(chat_id)
            return
        if text.startswith(TELEGRAM_LOGISTICS_DATE_PREFIX):
            self.send_logistics_report(chat_id, text.replace(TELEGRAM_LOGISTICS_DATE_PREFIX, "", 1).strip())
            return
        if text_matches(
            text,
            "/kiz_files",
            "/kiz",
            TELEGRAM_BUTTON_KIZ_BY_FILES,
            "Скачать сканы за сегодня",
            "Документы по импорту",
        ):
            self.show_kiz_export_menu(chat_id)
            return
        if text.startswith(TELEGRAM_KIZ_DATE_PREFIX):
            self.send_kiz_date_by_index(chat_id, text)
            return
        if text.startswith(TELEGRAM_KIZ_FILE_PREFIX):
            self.send_kiz_source_file_by_index(chat_id, text)
            return
        if normalize_text(text).casefold().startswith(("/kiz", "киз")):
            dates = parse_dates_from_text(text)
            if len(dates) >= 2:
                self.send_kiz_range_report(chat_id, dates[0], dates[1])
                return
            if len(dates) == 1:
                self.send_kiz_date_report(chat_id, dates[0])
                return
            self.show_kiz_export_menu(chat_id)
            return
        if text_matches(text, "/status", TELEGRAM_BUTTON_STATUS):
            self.send_status_report(chat_id)
            return
        if text_matches(text, "/health"):
            if not self.ensure_admin_chat(chat_id):
                return
            payload = self.backend_get("/health")
            self.send_message(chat_id, f"Backend: {payload.get('status')} / {payload.get('version')}")
            return
        if text_matches(text, "/imports"):
            if not self.ensure_admin_chat(chat_id):
                return
            self.send_imports_report(chat_id)
            return
        if text_matches(text, "/logs"):
            if not self.ensure_admin_chat(chat_id):
                return
            self.send_backend_diagnostics_log(chat_id)
            return
        if normalize_text(text).casefold().startswith(("/skladbot_daily", "/skladbot_report")):
            if not self.ensure_admin_chat(chat_id):
                return
            command_text = normalize_text(text)
            command_parts = command_text.split(maxsplit=1)
            if len(command_parts) > 1 and not parse_dates_from_text(command_parts[1]):
                self.safe_send_message(chat_id, "Неверная дата отчета. Используйте формат ДД.ММ.ГГГГ, например 09.06.2026.")
                return
            allow_partial = "--allow-partial" in {part.casefold() for part in command_text.split()}
            self.send_skladbot_daily_report(
                chat_id,
                report_date=command_date_or_today(text),
                allow_partial=allow_partial,
            )
            return

        document = message.get("document") or {}
        if document:
            self.enqueue_telegram_document(chat_id, document, update_id=update.get("update_id"), shipment_date="")
            return

        if text and self.handle_manual_text(chat_id, text):
            return

        if text and self.confirm_waiting_telegram_import_shipment_date(chat_id, text):
            return

        self.send_main_menu(chat_id, "Команда не распознана. Выберите действие в меню:")

    def handle_callback_query(self, callback_query):
        callback_id = normalize_text(callback_query.get("id"))
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if not self.is_allowed_chat(chat_id):
            logging.warning("Telegram worker denied unauthorized callback")
            return
        if not self.is_admin_chat(chat_id):
            logging.info("Telegram worker ignored callback from outbound-only chat")
            return

        data = normalize_text(callback_query.get("data"))
        self.answer_callback_query(callback_id)
        if data == f"{TELEGRAM_MENU_CALLBACK_PREFIX}root":
            self.send_main_menu(chat_id)
            return
        if data == f"{TELEGRAM_MENU_CALLBACK_PREFIX}date":
            self.send_date_help(chat_id)
            return
        if data == f"{TELEGRAM_MENU_CALLBACK_PREFIX}logistics":
            self.show_logistics_dates(chat_id)
            return
        if data == f"{TELEGRAM_MENU_CALLBACK_PREFIX}kiz":
            self.show_kiz_export_menu(chat_id)
            return
        if data == f"{TELEGRAM_MENU_CALLBACK_PREFIX}status":
            self.send_status_report(chat_id)
            return
        if data == f"{TELEGRAM_MENU_CALLBACK_PREFIX}imports":
            if not self.ensure_admin_chat(chat_id):
                return
            self.send_imports_report(chat_id)
            return
        if data == f"{TELEGRAM_MENU_CALLBACK_PREFIX}manual":
            self.show_manual_menu(chat_id)
            return
        if data.startswith(TELEGRAM_MANUAL_CALLBACK_PREFIX):
            self.handle_manual_callback(chat_id, data)
            return
        if data.startswith(TELEGRAM_EXCEL_DATE_CHOICE_USE_EXCEL_PREFIX):
            self.confirm_telegram_import_excel_date(
                chat_id,
                data.replace(TELEGRAM_EXCEL_DATE_CHOICE_USE_EXCEL_PREFIX, "", 1),
            )
            return
        if data.startswith(TELEGRAM_EXCEL_DATE_CHOICE_CANCEL_PREFIX):
            self.cancel_telegram_import_date_choice(
                chat_id,
                data.replace(TELEGRAM_EXCEL_DATE_CHOICE_CANCEL_PREFIX, "", 1),
            )
            return
        if data.startswith("logistics:"):
            self.send_logistics_report(chat_id, data.split(":", 1)[1])
            return
        if data == "kiz_mode:dates":
            self.show_kiz_dates(chat_id)
            return
        if data == "kiz_mode:files":
            self.show_kiz_source_files(chat_id)
            return
        if data.startswith(TELEGRAM_KIZ_RANGE_CALLBACK_PREFIX):
            date_from, _, date_to = data.replace(TELEGRAM_KIZ_RANGE_CALLBACK_PREFIX, "", 1).partition(":")
            self.send_kiz_range_report(chat_id, date_from, date_to)
            return
        if data.startswith("kiz_date:"):
            self.send_kiz_date_report(chat_id, data.split(":", 1)[1])
            return
        if data.startswith("kiz_file:"):
            self.send_kiz_source_file_by_index(chat_id, data.split(":", 1)[1])
            return
        self.send_main_menu(chat_id, "Кнопка устарела. Выберите действие заново:")




def main(*, backend_token=None):
    from .worker_observability import observed_worker_cycle

    try:
        worker = TelegramWorker(backend_token=backend_token)
    except TelegramConfigurationError as exc:
        logging.error("Telegram worker configuration invalid: %s", ", ".join(exc.setting_names))
        return 2
    if not worker.configured:
        while True:
            try:
                with observed_worker_cycle("telegram", 300):
                    logging.info("Telegram worker waiting for configuration")
            except Exception:
                logging.exception("Telegram worker heartbeat failed")
            time.sleep(300)

    while True:
        try:
            with observed_worker_cycle("telegram", max(1, worker.poll_timeout)):
                worker.poll_once()
        except Exception:
            logging.exception("Telegram worker failed")
            time.sleep(10)


if __name__ == "__main__":
    raise SystemExit(main())
