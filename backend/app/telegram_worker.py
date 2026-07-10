import logging
import hashlib
import json
import os
import re
import time
import urllib.parse
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx

from . import skladbot_daily_report
from . import telegram_runtime_dependencies
from .event_leases import claim_event_leases, event_leases_enabled, finalize_event_leases
from .event_queue_service import reset_stale_processing_events
from .excel_importer import excel_file_to_import_payload
from .redaction import redact_secrets
from .reconciliation_service import run_daily_reconciliation
from .telegram_admin_processor import TelegramAdminProcessor
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


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

SessionLocal = telegram_runtime_dependencies.SessionLocal


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
TELEGRAM_MANUAL_BLOCK_PRICE = 240000
TELEGRAM_MANUAL_PIECES_PER_BLOCK = 10
TELEGRAM_MANUAL_PRODUCTS = {
    "brown_op": "Chapman Brown OP 20",
    "brown_ssl": "Chapman Brown SSL 100`20",
    "red_op": "Chapman RED OP 20",
    "red_ssl": "Chapman RED SSL 100 20",
    "gold_ssl": "Chapman Gold SSL 100`20",
    "green_op": "Chapman Green OP 20",
}
TELEGRAM_MANUAL_PAYMENT_TYPES = {
    "terminal": "Терминал",
    "transfer": "Перечисление",
}
DATE_PATTERN = re.compile(r"(?<!\d)(\d{1,2})[._/-](\d{1,2})[._/-](\d{2,4})(?!\d)")
COORDINATES_PATTERN = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,;]\s*(-?\d+(?:\.\d+)?)\s*$")


def normalize_text(value):
    return str(value or "").strip()


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
):
    if not normalize_text(token):
        return True
    errors = []
    allowed = {str(value) for value in allowed_chat_ids or ()}
    admins = {str(value) for value in admin_chat_ids or ()}
    scheduled = {str(value) for value in scheduled_chat_ids or ()}
    reconciliation = {str(value) for value in reconciliation_chat_ids or ()}
    if not allowed:
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
    if not reconciliation.issubset(allowed):
        errors.append("TAKSKLAD_DAILY_RECONCILIATION_CHAT_IDS")
    if errors:
        raise TelegramConfigurationError(errors)
    return True


def parse_bool_flag(value, default=False):
    text = normalize_text(value).casefold()
    if not text:
        return default
    return text in {"1", "true", "yes", "on", "да"}


def telegram_inline_keyboard(button_rows):
    return {"inline_keyboard": button_rows}


def telegram_main_reply_keyboard():
    return {
        "keyboard": [
            [
                {"text": TELEGRAM_BUTTON_LOGISTICS_REPORT},
                {"text": TELEGRAM_BUTTON_KIZ_BY_FILES},
            ],
            [
                {"text": TELEGRAM_BUTTON_STATUS},
                {"text": TELEGRAM_BUTTON_IMPORTS},
            ],
            [
                {"text": TELEGRAM_BUTTON_SHIPMENT_DATE},
                {"text": TELEGRAM_BUTTON_MANUAL},
            ],
        ],
        "resize_keyboard": True,
    }


def telegram_manual_menu_keyboard():
    return telegram_inline_keyboard([
        [{"text": "Добавить заказ вручную", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}add"}],
        [{"text": "Удалить активный заказ", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}delete"}],
        [{"text": "Отмена", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}cancel"}],
    ])


def telegram_manual_payment_keyboard():
    return telegram_inline_keyboard([
        [{"text": label, "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}payment:{key}"}]
        for key, label in TELEGRAM_MANUAL_PAYMENT_TYPES.items()
    ])


def telegram_manual_product_keyboard():
    rows = [
        [{"text": label, "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}product:{key}"}]
        for key, label in TELEGRAM_MANUAL_PRODUCTS.items()
    ]
    rows.append([{"text": "Отмена", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}cancel"}])
    return telegram_inline_keyboard(rows)


def telegram_manual_add_next_keyboard():
    return telegram_inline_keyboard([
        [{"text": "Добавить ещё позицию", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}add_more"}],
        [{"text": "Создать заказ", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}create"}],
        [{"text": "Отмена", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}cancel"}],
    ])


def telegram_manual_delete_keyboard(orders):
    rows = []
    for index, order in enumerate(orders, start=1):
        client = normalize_text(order.get("client")) or "без клиента"
        text = f"{index}. {display_date(order.get('order_date')) or 'без даты'} | {client}"
        if len(text) > 58:
            text = text[:55] + "..."
        rows.append([{"text": text, "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}delete:{index}"}])
    rows.append([{"text": "Отмена", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}cancel"}])
    return telegram_inline_keyboard(rows)


def telegram_manual_delete_confirm_keyboard(order_id):
    return telegram_inline_keyboard([
        [{"text": "Удалить из TakSklad", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}delete_confirm:{order_id}"}],
        [{"text": "Отмена", "callback_data": f"{TELEGRAM_MANUAL_CALLBACK_PREFIX}cancel"}],
    ])




def text_matches(value, *variants):
    normalized = normalize_text(value).casefold()
    return normalized in {normalize_text(variant).casefold() for variant in variants}


def parse_date_from_text(value):
    text = normalize_text(value)
    match = DATE_PATTERN.search(text)
    if not match:
        return ""
    day, month, year = match.groups()
    if len(year) == 2:
        year = "20" + year
    try:
        parsed = datetime.strptime(f"{int(day):02d}.{int(month):02d}.{year}", "%d.%m.%Y")
    except ValueError:
        return ""
    return parsed.strftime("%d.%m.%Y")


def parse_dates_from_text(value):
    result = []
    for match in DATE_PATTERN.finditer(normalize_text(value)):
        day, month, year = match.groups()
        if len(year) == 2:
            year = "20" + year
        try:
            parsed = datetime.strptime(f"{int(day):02d}.{int(month):02d}.{year}", "%d.%m.%Y")
        except ValueError:
            continue
        iso = parsed.strftime("%Y-%m-%d")
        if iso not in result:
            result.append(iso)
    return result


def parse_int(value):
    text = normalize_text(value).replace(" ", "").replace(",", ".")
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def format_money(value):
    return f"{parse_int(value):,}".replace(",", " ")


def iso_date_from_display(value):
    parsed = parse_date_from_text(value)
    if parsed:
        return datetime.strptime(parsed, "%d.%m.%Y").strftime("%Y-%m-%d")
    text = normalize_text(value)
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def display_date(value):
    text = normalize_text(value)
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        pass
    parsed = parse_date_from_text(text)
    return parsed or text


def manual_address_and_coordinates(value):
    text = normalize_text(value)
    match = COORDINATES_PATTERN.match(text)
    if not match:
        return text, ""
    lat, lng = match.groups()
    return "Адрес не указан", f"{lat}, {lng}"


def order_scanned_blocks(order):
    total = 0
    for item in (order or {}).get("items") or []:
        total += max(parse_int(item.get("scanned_blocks")), len(item.get("scan_codes") or []))
    return total


def order_planned_blocks(order):
    return sum(parse_int(item.get("quantity_blocks")) for item in ((order or {}).get("items") or []))


def manual_order_summary(flow):
    data = (flow or {}).get("data") or {}
    lines = [
        "Проверьте ручной заказ:",
        "",
        f"Дата отгрузки: {data.get('order_date') or ''}",
        f"Тип оплаты: {data.get('payment_type') or ''}",
        f"Клиент: {data.get('client') or ''}",
        f"Адрес: {data.get('address') or ''}",
    ]
    if data.get("coordinates"):
        lines.append(f"Координаты: {data.get('coordinates')}")
    lines.append(f"Торг.пред: {data.get('representative') or ''}")
    lines.extend(["", "Позиции:"])
    for item in data.get("items") or []:
        lines.append(f"- {item.get('product')}: {item.get('blocks')} блок.")
    return "\n".join(lines)


def build_manual_import_payload(chat_id, flow):
    data = (flow or {}).get("data") or {}
    manual_id = normalize_text(data.get("manual_id")) or str(uuid.uuid4())
    source_file = f"telegram-manual-{manual_id}.xlsx"
    rows = []
    for index, item in enumerate(data.get("items") or [], start=1):
        blocks = parse_int(item.get("blocks"))
        rows.append({
            "Дата отгрузки": data.get("order_date") or "",
            "Тип оплаты": data.get("payment_type") or "",
            "Клиент": data.get("client") or "",
            "Адрес": data.get("address") or "",
            "Координаты": data.get("coordinates") or "",
            "Торговый представитель": data.get("representative") or "",
            "Товары": item.get("product") or "",
            "Кол-во ШТ": blocks * TELEGRAM_MANUAL_PIECES_PER_BLOCK,
            "Кол-во блок": blocks,
            "Цена за блок": TELEGRAM_MANUAL_BLOCK_PRICE,
            "Сумма позиции": blocks * TELEGRAM_MANUAL_BLOCK_PRICE,
            "Источник файла": source_file,
            "ID заказа": f"telegram-manual-{manual_id}",
            "ID импорта": f"telegram-manual-{manual_id}:{index}",
        })
    return {
        "source": "telegram_manual",
        "filename": source_file,
        "telegram_chat_id": normalize_text(chat_id),
        "rows": rows,
    }










































def json_dumps(value):
    return json.dumps(value, ensure_ascii=False)




class TelegramWorker(
    TelegramScheduledReportProcessor,
    TelegramAdminProcessor,
    TelegramImportProcessor,
    TelegramReportProcessor,
):
    def __init__(self):
        self.token = normalize_text(os.environ.get("TELEGRAM_BOT_TOKEN"))
        self.allowed_chat_ids = parse_chat_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS"))
        self.admin_chat_ids = parse_chat_ids(os.environ.get("TELEGRAM_ADMIN_CHAT_IDS"))
        self.backend_url = normalize_text(os.environ.get("TAKSKLAD_BACKEND_INTERNAL_URL")) or "http://backend-api:8000"
        self.backend_token = normalize_text(os.environ.get("TAKSKLAD_API_TOKEN"))
        self.timeout = int(os.environ.get("TELEGRAM_WORKER_TIMEOUT_SECONDS", "20") or "20")
        self.import_timeout = int(os.environ.get("TELEGRAM_WORKER_IMPORT_TIMEOUT_SECONDS", "120") or "120")
        self.file_timeout = int(os.environ.get("TELEGRAM_WORKER_FILE_TIMEOUT_SECONDS", "120") or "120")
        self.poll_timeout = int(os.environ.get("TELEGRAM_WORKER_POLL_TIMEOUT_SECONDS", "15") or "15")
        self.max_file_size = int(os.environ.get("TELEGRAM_WORKER_MAX_FILE_BYTES", str(20 * 1024 * 1024)) or 0)
        self.skladbot_daily_report_enabled = parse_bool_flag(os.environ.get("SKLADBOT_DAILY_REPORT_ENABLED"))
        self.skladbot_daily_report_chat_ids = parse_chat_ids(os.environ.get("SKLADBOT_DAILY_REPORT_CHAT_IDS"))
        self.skladbot_daily_report_hour = max(0, min(23, parse_int(os.environ.get("SKLADBOT_DAILY_REPORT_HOUR") or "22")))
        self.skladbot_daily_report_minute = max(0, min(59, parse_int(os.environ.get("SKLADBOT_DAILY_REPORT_MINUTE") or "0")))
        self.skladbot_daily_report_retry_minutes = max(1, parse_int(os.environ.get("SKLADBOT_DAILY_REPORT_RETRY_MINUTES") or "15"))
        self.daily_reconciliation_enabled = parse_bool_flag(os.environ.get("TAKSKLAD_DAILY_RECONCILIATION_ENABLED"), default=True)
        self.daily_reconciliation_chat_ids = parse_chat_ids(os.environ.get("TAKSKLAD_DAILY_RECONCILIATION_CHAT_IDS"))
        validate_telegram_worker_config(
            self.token,
            self.allowed_chat_ids,
            self.admin_chat_ids,
            self.skladbot_daily_report_chat_ids,
            self.daily_reconciliation_chat_ids,
        )
        self.offset = self.load_offset() or int(os.environ.get("TELEGRAM_WORKER_INITIAL_OFFSET", "0") or "0")
        self.bot_menu_ready = False
        self.manual_flow_cache = {}

    @property
    def configured(self):
        return bool(getattr(self, "token", ""))

    def telegram_request(self, method, payload=None, timeout=None):
        with httpx.Client(timeout=timeout or self.timeout) as client:
            try:
                response = client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload or {})
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = redact_secrets(exc.response.text[:300] if exc.response is not None else "")
                raise RuntimeError(f"Telegram API request failed: {method}: HTTP {exc.response.status_code} {detail}") from None
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Telegram API request failed: {method}: {exc.__class__.__name__}") from None
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(redact_secrets(data))
            return data.get("result")

    def ensure_bot_menu(self):
        if getattr(self, "bot_menu_ready", False):
            return
        try:
            self.telegram_request("deleteMyCommands", {})
            self.telegram_request("setChatMenuButton", {"menu_button": {"type": "default"}})
            self.bot_menu_ready = True
        except Exception:
            logging.warning("Telegram worker: failed to configure bot menu", exc_info=True)

    def backend_get(self, path, params=None):
        headers = {}
        if self.backend_token:
            headers["Authorization"] = f"Bearer {self.backend_token}"
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.backend_url}{path}", params=params or {}, headers=headers)
            response.raise_for_status()
            return response.json()

    def backend_get_bytes(self, path, params=None):
        headers = {}
        if self.backend_token:
            headers["Authorization"] = f"Bearer {self.backend_token}"
        with httpx.Client(timeout=self.file_timeout) as client:
            response = client.get(f"{self.backend_url}{path}", params=params or {}, headers=headers)
            response.raise_for_status()
            return response.content, response.headers

    def backend_post(self, path, payload=None):
        headers = {}
        if self.backend_token:
            headers["Authorization"] = f"Bearer {self.backend_token}"
        timeout = getattr(self, "import_timeout", self.timeout) if path == "/api/v1/imports" else self.timeout
        with httpx.Client(timeout=timeout) as client:
            response = client.post(f"{self.backend_url}{path}", json=payload or {}, headers=headers)
            response.raise_for_status()
            return response.json()

    def send_message(self, chat_id, text, reply_markup=None):
        payload = {
            "chat_id": chat_id,
            "text": text[:3900],
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.telegram_request("sendMessage", payload)

    def send_document(self, chat_id, content, filename, caption=""):
        with httpx.Client(timeout=self.file_timeout) as client:
            files = {"document": (filename, content)}
            data = {"chat_id": chat_id, "caption": caption[:1000]}
            try:
                response = client.post(f"https://api.telegram.org/bot{self.token}/sendDocument", data=data, files=files)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code if exc.response is not None else ""
                detail = redact_secrets(exc.response.text[:300] if exc.response is not None else "")
                raise RuntimeError(f"Telegram API request failed: sendDocument: HTTP {status_code} {detail}") from None
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Telegram API request failed: sendDocument: {exc.__class__.__name__}") from None
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(redact_secrets(payload))
            return payload.get("result")

    def safe_send_document(self, chat_id, content, filename, caption=""):
        try:
            return self.send_document(chat_id, content, filename, caption=caption)
        except Exception as exc:
            logging.warning("Telegram worker: failed to send document: %s", redact_secrets(exc))
            self.safe_send_message(chat_id, f"Не удалось отправить файл: {filename}")
            return None

    def safe_send_message(self, chat_id, text, reply_markup=None):
        try:
            if reply_markup is None:
                return self.send_message(chat_id, text)
            return self.send_message(chat_id, text, reply_markup=reply_markup)
        except Exception:
            logging.warning("Telegram worker: failed to send message", exc_info=True)
            return None

    def send_main_menu(self, chat_id, text=""):
        lines = [
            normalize_text(text) or "Меню TakSklad",
            "",
            "Excel-файл можно просто отправить в этот чат. Бот попросит дату отгрузки перед импортом.",
        ]
        self.safe_send_message(chat_id, "\n".join(lines), reply_markup=telegram_main_reply_keyboard())

    def send_date_help(self, chat_id):
        current_date = self.get_chat_shipment_date(chat_id)
        self.safe_send_message(
            chat_id,
            "\n".join([
                "Дата отгрузки задаётся после загрузки каждого Excel-файла.",
                "Отправьте дату одним сообщением в формате ДД.ММ.ГГГГ.",
                "Пример: 09.06.2026",
                f"Сохранённая дата чата: {current_date or 'не задана'}",
            ]),
        )

    def answer_callback_query(self, callback_query_id, text=""):
        callback_query_id = normalize_text(callback_query_id)
        if not callback_query_id:
            return None
        payload = {"callback_query_id": callback_query_id}
        if normalize_text(text):
            payload["text"] = normalize_text(text)[:200]
        return self.telegram_request("answerCallbackQuery", payload)











































































    def poll_once(self):
        if not self.configured:
            logging.info("Telegram worker disabled: TELEGRAM_BOT_TOKEN is not configured")
            return

        self.ensure_bot_menu()
        poll_timeout = max(1, min(self.poll_timeout, max(1, self.timeout - 5)))
        try:
            updates = self.telegram_request("getUpdates", {
                "offset": self.offset + 1 if self.offset else None,
                "timeout": poll_timeout,
                "allowed_updates": ["message", "callback_query"],
            }, timeout=poll_timeout + 5) or []
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
        if not self.is_allowed_chat(chat_id):
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
            if not self.ensure_admin_chat(chat_id):
                return
            if text and self.handle_manual_text(chat_id, text):
                return
            shipment_date = parse_date_from_text(text)
            if shipment_date:
                if self.confirm_waiting_telegram_import_shipment_date(chat_id, shipment_date):
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

        if text and self.is_admin_chat(chat_id) and self.confirm_waiting_telegram_import_shipment_date(chat_id, text):
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
            if not self.ensure_admin_chat(chat_id):
                return
            self.confirm_telegram_import_excel_date(
                chat_id,
                data.replace(TELEGRAM_EXCEL_DATE_CHOICE_USE_EXCEL_PREFIX, "", 1),
            )
            return
        if data.startswith(TELEGRAM_EXCEL_DATE_CHOICE_CANCEL_PREFIX):
            if not self.ensure_admin_chat(chat_id):
                return
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




def main():
    try:
        worker = TelegramWorker()
    except TelegramConfigurationError as exc:
        logging.error("Telegram worker configuration invalid: %s", ", ".join(exc.setting_names))
        return 2
    if not worker.configured:
        while True:
            logging.info("Telegram worker waiting for TELEGRAM_BOT_TOKEN")
            time.sleep(300)

    while True:
        try:
            worker.poll_once()
        except Exception:
            logging.exception("Telegram worker failed")
            time.sleep(10)


if __name__ == "__main__":
    raise SystemExit(main())
