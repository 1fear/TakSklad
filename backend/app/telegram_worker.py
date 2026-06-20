import logging
import json
import os
import re
import tempfile
import time
import urllib.parse
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import select

from . import skladbot_daily_report
from .db import SessionLocal
from .event_queue_service import reset_stale_processing_events
from .excel_importer import ExcelDateConflictError, excel_file_to_import_payload, is_supported_excel_file_name
from .models import AuditLog, Incident, PendingEvent
from .redaction import redact_secrets
from .reconciliation_service import run_daily_reconciliation


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
TELEGRAM_KIZ_MENU_RECENT_LIMIT = 7
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


def telegram_import_failure_message(file_name, reason):
    reason_text = redact_secrets(normalize_text(reason)) or "неизвестная ошибка"
    return "\n".join([
        "Не удалось импортировать Excel-файл.",
        "",
        f"Файл: {normalize_text(file_name) or 'telegram_import.xlsx'}",
        f"Причина: {reason_text}",
        "",
        "Что сделать: исправьте файл и отправьте его заново. Если файл уже в очереди, проверьте Инциденты в web-панели.",
        "Заказы и заявки SkladBot не созданы.",
    ])


def ensure_telegram_import_event_incident(db, event, error):
    if event.event_type != TELEGRAM_EXCEL_IMPORT_EVENT_TYPE:
        return None
    existing = db.execute(
        select(Incident).where(Incident.pending_event_id == event.id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    payload = dict(event.payload or {})
    document = payload.get("document") if isinstance(payload.get("document"), dict) else {}
    file_name = normalize_text(payload.get("file_name")) or normalize_text(document.get("file_name")) or "telegram_import.xlsx"
    incident = Incident(
        source="telegram_import",
        severity="critical",
        status="open",
        title="Telegram Excel import failed",
        message=normalize_text(error),
        entity_type="pending_event",
        entity_id=str(event.id),
        pending_event_id=event.id,
        raw_payload={
            "event_type": event.event_type,
            "event_status": event.status,
            "file_name": file_name,
            "attempts": int(event.attempts or 0),
            "error": normalize_text(error),
        },
    )
    db.add(incident)
    db.add(AuditLog(
        action="telegram_import_incident_created",
        entity_type="pending_event",
        entity_id=str(event.id),
        payload={
            "event_type": event.event_type,
            "status": event.status,
            "file_name": file_name,
            "attempts": int(event.attempts or 0),
        },
    ))
    return incident


def find_existing_telegram_import_event(db, document, update_id=None):
    file_id = normalize_text((document or {}).get("file_id"))
    update_id = normalize_text(update_id)
    if not file_id and not update_id:
        return None
    candidates = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == TELEGRAM_EXCEL_IMPORT_EVENT_TYPE)
        .where(PendingEvent.status.in_((
            "pending",
            "processing",
            TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS,
            TELEGRAM_EXCEL_IMPORT_WAITING_DATE_CHOICE_STATUS,
            "completed",
            "failed",
        )))
        .order_by(PendingEvent.created_at.desc(), PendingEvent.id.desc())
    ).scalars().all()
    for event in candidates:
        payload = event.payload or {}
        payload_document = payload.get("document") or {}
        if update_id and normalize_text(payload.get("update_id")) == update_id:
            return event
        if file_id and normalize_text(payload_document.get("file_id")) == file_id:
            return event
    return None


def parse_chat_ids(value):
    result = set()
    for part in str(value or "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            result.add(part)
    return result


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


def telegram_import_date_choice_keyboard(event_id, excel_date):
    event_id = normalize_text(event_id)
    return telegram_inline_keyboard([
        [{
            "text": f"Использовать дату Excel: {excel_date}",
            "callback_data": f"{TELEGRAM_EXCEL_DATE_CHOICE_USE_EXCEL_PREFIX}{event_id}",
        }],
        [{
            "text": "Отменить импорт",
            "callback_data": f"{TELEGRAM_EXCEL_DATE_CHOICE_CANCEL_PREFIX}{event_id}",
        }],
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


def command_date_or_today(text):
    dates = parse_dates_from_text(text)
    if dates:
        return datetime.strptime(dates[0], "%Y-%m-%d").date()
    return skladbot_daily_report.business_today()


def coerce_report_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    iso_date = iso_date_from_display(value)
    if iso_date:
        return datetime.strptime(iso_date, "%Y-%m-%d").date()
    return command_date_or_today(str(value))


def parse_iso_date(value):
    text = normalize_text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def ensure_aware_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def skladbot_reported_request_key(request_id):
    return f"skladbot_daily_reported_request:{parse_int(request_id)}"


def load_skladbot_daily_reported_request_ids_before(report_date):
    report_date = coerce_report_date(report_date)
    result = set()
    with SessionLocal() as db:
        events = db.execute(
            select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_DAILY_REPORTED_REQUEST_EVENT_TYPE)
        ).scalars().all()
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        request_id = parse_int(payload.get("request_id"))
        reported_date = parse_iso_date(payload.get("reported_date"))
        if request_id > 0 and reported_date and reported_date < report_date:
            result.add(request_id)
    return result


def mark_skladbot_daily_report_requests_reported(report, chat_id=None):
    report_date = coerce_report_date(report.get("report_date") or skladbot_daily_report.business_today())
    rows = []
    for request in report.get("requests") or []:
        request_id = parse_int(request.get("id"))
        if request_id <= 0:
            continue
        rows.append({
            "request_id": request_id,
            "request_number": normalize_text(request.get("number")),
            "category": normalize_text(request.get("category")),
            "reported_date": report_date.isoformat(),
            "chat_id": normalize_text(chat_id),
            "include_reasons": list(request.get("include_reasons") or []),
        })
    if not rows:
        return 0
    saved = 0
    with SessionLocal() as db:
        for row in rows:
            key = skladbot_reported_request_key(row["request_id"])
            existing = db.execute(
                select(PendingEvent).where(PendingEvent.idempotency_key == key)
            ).scalar_one_or_none()
            if existing is not None:
                continue
            db.add(PendingEvent(
                event_type=SKLADBOT_DAILY_REPORTED_REQUEST_EVENT_TYPE,
                idempotency_key=key,
                status="completed",
                attempts=1,
                payload=row,
            ))
            saved += 1
        db.commit()
    return saved


def kiz_progress_completed(item):
    item = item or {}
    if "completed" in item:
        return bool(item.get("completed"))
    return parse_int(item.get("scanned_blocks")) >= parse_int(item.get("planned_blocks"))


def recent_kiz_dates_for_menu(dates, limit=TELEGRAM_KIZ_MENU_RECENT_LIMIT):
    dates = list(dates or [])
    if len(dates) <= limit:
        return dates
    return sorted(
        dates,
        key=lambda item: iso_date_from_display((item or {}).get("date") or ""),
    )[-limit:]


def kiz_source_file_latest_date(item):
    dates = [
        iso_date_from_display(value)
        for value in ((item or {}).get("dates") or [])
        if iso_date_from_display(value)
    ]
    return max(dates) if dates else ""


def recent_kiz_source_files_for_menu(files, limit=TELEGRAM_KIZ_MENU_RECENT_LIMIT):
    files = list(files or [])
    if len(files) <= limit:
        return files
    return sorted(
        files,
        key=lambda item: (
            kiz_source_file_latest_date(item),
            normalize_text((item or {}).get("source_file")),
            normalize_text((item or {}).get("source_key")),
        ),
    )[-limit:]


def backend_http_error_detail(exc):
    response = getattr(exc, "response", None)
    if response is None:
        return redact_secrets(normalize_text(exc))[:300]
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if isinstance(payload, dict) and normalize_text(payload.get("detail")):
        return redact_secrets(normalize_text(payload.get("detail")))[:300]
    text = redact_secrets(normalize_text(getattr(response, "text", "")))
    return text[:300] or f"HTTP {getattr(response, 'status_code', '')}".strip()


def backend_failure_message(action, exc):
    action = normalize_text(action) or "Действие не выполнено"
    if isinstance(exc, httpx.HTTPStatusError):
        detail = backend_http_error_detail(exc)
        return f"{action}: {detail or 'backend вернул ошибку'}"
    if isinstance(exc, httpx.HTTPError):
        return f"{action}: backend временно недоступен ({exc.__class__.__name__})"
    detail = redact_secrets(normalize_text(exc))[:300]
    return f"{action}: {detail or exc.__class__.__name__}"


def json_dumps(value):
    return json.dumps(value, ensure_ascii=False)


def summarize_active_orders_by_date(orders):
    summary = {}
    for order in orders or []:
        date_key = normalize_text(order.get("order_date")) or "Без даты"
        bucket = summary.setdefault(date_key, {
            "orders": 0,
            "items": 0,
            "planned_blocks": 0,
            "scanned_blocks": 0,
            "remaining_blocks": 0,
            "missing_skladbot": 0,
            "total_price": 0,
        })
        bucket["orders"] += 1
        if not normalize_text(order.get("skladbot_request_number")) and not normalize_text(order.get("skladbot_request_id")):
            bucket["missing_skladbot"] += 1
        for item in order.get("items") or []:
            planned = parse_int(item.get("quantity_blocks"))
            scanned = parse_int(item.get("scanned_blocks"))
            bucket["items"] += 1
            bucket["planned_blocks"] += planned
            bucket["scanned_blocks"] += scanned
            bucket["remaining_blocks"] += max(0, planned - scanned)
            bucket["total_price"] += parse_int(item.get("line_total"))
    return summary


class TelegramWorker:
    def __init__(self):
        self.token = normalize_text(os.environ.get("TELEGRAM_BOT_TOKEN"))
        self.allowed_chat_ids = parse_chat_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS"))
        self.admin_chat_ids = parse_chat_ids(os.environ.get("TELEGRAM_ADMIN_CHAT_IDS"))
        self.backend_url = normalize_text(os.environ.get("TAKSKLAD_BACKEND_INTERNAL_URL")) or "http://backend-api:8000"
        self.backend_token = normalize_text(os.environ.get("TAKSKLAD_API_TOKEN"))
        self.timeout = int(os.environ.get("TELEGRAM_WORKER_TIMEOUT_SECONDS", "20") or "20")
        self.file_timeout = int(os.environ.get("TELEGRAM_WORKER_FILE_TIMEOUT_SECONDS", "120") or "120")
        self.poll_timeout = int(os.environ.get("TELEGRAM_WORKER_POLL_TIMEOUT_SECONDS", "15") or "15")
        self.max_file_size = int(os.environ.get("TELEGRAM_WORKER_MAX_FILE_BYTES", str(20 * 1024 * 1024)) or 0)
        self.offset = self.load_offset() or int(os.environ.get("TELEGRAM_WORKER_INITIAL_OFFSET", "0") or "0")
        self.bot_menu_ready = False
        self.skladbot_daily_report_enabled = parse_bool_flag(os.environ.get("SKLADBOT_DAILY_REPORT_ENABLED"))
        self.skladbot_daily_report_chat_ids = parse_chat_ids(os.environ.get("SKLADBOT_DAILY_REPORT_CHAT_IDS"))
        self.skladbot_daily_report_hour = max(0, min(23, parse_int(os.environ.get("SKLADBOT_DAILY_REPORT_HOUR") or "22")))
        self.skladbot_daily_report_minute = max(0, min(59, parse_int(os.environ.get("SKLADBOT_DAILY_REPORT_MINUTE") or "0")))
        self.skladbot_daily_report_retry_minutes = max(1, parse_int(os.environ.get("SKLADBOT_DAILY_REPORT_RETRY_MINUTES") or "15"))
        self.daily_reconciliation_enabled = parse_bool_flag(os.environ.get("TAKSKLAD_DAILY_RECONCILIATION_ENABLED"), default=True)
        self.daily_reconciliation_chat_ids = parse_chat_ids(os.environ.get("TAKSKLAD_DAILY_RECONCILIATION_CHAT_IDS"))
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
        with httpx.Client(timeout=self.timeout) as client:
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

    def chat_state_event_type(self, chat_id):
        return f"{TELEGRAM_CHAT_STATE_EVENT_PREFIX}{chat_id}"

    def is_admin_chat(self, chat_id):
        admin_chat_ids = getattr(self, "admin_chat_ids", set())
        return not admin_chat_ids or str(chat_id) in admin_chat_ids

    def ensure_admin_chat(self, chat_id):
        if self.is_admin_chat(chat_id):
            return True
        self.send_message(chat_id, "Команда доступна только администратору.")
        logging.warning("Telegram worker denied admin command for chat_id=%s", chat_id)
        return False

    def get_chat_state(self, chat_id):
        with SessionLocal() as db:
            state = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == self.chat_state_event_type(chat_id))
            ).scalars().first()
            return dict(state.payload or {}) if state else {}

    def save_chat_state(self, chat_id, payload):
        with SessionLocal() as db:
            state = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == self.chat_state_event_type(chat_id))
            ).scalars().first()
            if state is None:
                state = PendingEvent(event_type=self.chat_state_event_type(chat_id), status="active", payload={})
                db.add(state)
            state.payload = payload
            db.commit()

    def get_chat_shipment_date(self, chat_id):
        return normalize_text(self.get_chat_state(chat_id).get("shipment_date"))

    def set_chat_shipment_date(self, chat_id, shipment_date):
        state = self.get_chat_state(chat_id)
        state["shipment_date"] = shipment_date
        state["shipment_date_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.save_chat_state(chat_id, state)

    def take_waiting_telegram_import_for_date(self, chat_id):
        with SessionLocal() as db:
            stmt = (
                select(PendingEvent)
                .where(PendingEvent.event_type == TELEGRAM_EXCEL_IMPORT_EVENT_TYPE)
                .where(PendingEvent.status == TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS)
                .order_by(PendingEvent.created_at, PendingEvent.id)
            )
            events = db.execute(stmt).scalars().all()
            for event in events:
                payload = event.payload or {}
                if normalize_text(payload.get("chat_id")) == normalize_text(chat_id):
                    return str(event.id), dict(payload)
        return "", {}

    def confirm_waiting_telegram_import_shipment_date(self, chat_id, shipment_date):
        parsed_date = parse_date_from_text(shipment_date)
        event_id, payload = self.take_waiting_telegram_import_for_date(chat_id)
        if not event_id:
            return False
        file_name = normalize_text(payload.get("file_name")) or "Excel-файл"
        if not parsed_date:
            self.safe_send_message(
                chat_id,
                "\n".join([
                    "Ожидаю дату отгрузки для Excel-файла.",
                    "",
                    f"Файл: {file_name}",
                    "Отправьте дату одним сообщением в формате ДД.ММ.ГГГГ.",
                    "Пример: 09.06.2026",
                ]),
            )
            return True

        event_uuid = self.parse_telegram_import_event_id(event_id)
        if event_uuid is None:
            self.safe_send_message(chat_id, "Не удалось найти ожидающий импорт. Отправьте Excel-файл заново.")
            return True

        with SessionLocal() as db:
            event = db.get(PendingEvent, event_uuid)
            if event is None or event.event_type != TELEGRAM_EXCEL_IMPORT_EVENT_TYPE:
                self.safe_send_message(chat_id, "Ожидающий импорт не найден. Отправьте Excel-файл заново.")
                return True
            if event.status != TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS:
                self.safe_send_message(chat_id, "Этот Excel-файл уже обработан или находится в очереди.")
                return True
            payload = dict(event.payload or {})
            payload["shipment_date"] = parsed_date
            payload["shipment_date_source"] = "telegram_manual_input"
            payload["shipment_date_confirmed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            event.payload = payload
            event.status = "pending"
            event.last_error = ""
            db.commit()

        self.safe_send_message(
            chat_id,
            "\n".join([
                "Дата принята. Excel-файл поставлен в очередь импорта.",
                "",
                f"Файл: {file_name}",
                f"Дата отгрузки: {parsed_date}",
            ]),
        )
        self.process_queued_telegram_imports()
        return True

    def parse_telegram_import_event_id(self, event_id):
        try:
            return uuid.UUID(normalize_text(event_id))
        except (TypeError, ValueError):
            return None

    def send_telegram_import_date_conflict_choice(self, chat_id, file_name, event_id, conflict):
        self.safe_send_message(
            chat_id,
            "\n".join([
                "Найден конфликт дат при импорте Excel.",
                "",
                f"Файл: {file_name}",
                f"Дата в Excel: {conflict.excel_date}",
                f"Дата в Telegram: {conflict.telegram_date}",
                "",
                "Заказы и заявки SkladBot ещё не созданы. Выберите дату Excel или отмените импорт.",
            ]),
            reply_markup=telegram_import_date_choice_keyboard(event_id, conflict.excel_date),
        )

    def mark_telegram_import_waiting_date_choice(self, event_id, conflict):
        event_uuid = self.parse_telegram_import_event_id(event_id)
        if event_uuid is None:
            return False
        with SessionLocal() as db:
            event = db.get(PendingEvent, event_uuid)
            if event is None:
                return False
            payload = dict(event.payload or {})
            payload["date_conflict"] = {
                "telegram_date": conflict.telegram_date,
                "excel_date": conflict.excel_date,
                "detected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            event.payload = payload
            event.status = TELEGRAM_EXCEL_IMPORT_WAITING_DATE_CHOICE_STATUS
            event.last_error = "date_choice_required"
            db.commit()
            return True

    def resolve_telegram_import_date_choice(self, chat_id, event_id, action):
        event_uuid = self.parse_telegram_import_event_id(event_id)
        if event_uuid is None:
            return False, {}, "Кнопка устарела: некорректный ID импорта."
        with SessionLocal() as db:
            event = db.get(PendingEvent, event_uuid)
            if event is None or event.event_type != TELEGRAM_EXCEL_IMPORT_EVENT_TYPE:
                return False, {}, "Кнопка устарела: импорт не найден."
            payload = dict(event.payload or {})
            if normalize_text(payload.get("chat_id")) != normalize_text(chat_id):
                return False, {}, "Нет доступа к этому импорту."
            if event.status != TELEGRAM_EXCEL_IMPORT_WAITING_DATE_CHOICE_STATUS:
                return False, payload, "Этот импорт уже обработан или отменён."

            resolution = {
                "action": action,
                "resolved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            if action == "use_excel":
                payload["shipment_date"] = ""
                event.status = "pending"
                event.last_error = ""
            elif action == "cancel":
                event.status = "cancelled"
                event.last_error = "cancelled_by_user"
            else:
                return False, payload, "Неизвестный выбор даты."
            payload["date_choice_resolution"] = resolution
            event.payload = payload
            db.commit()
            return True, payload, ""

    def confirm_telegram_import_excel_date(self, chat_id, event_id):
        success, payload, error = self.resolve_telegram_import_date_choice(chat_id, event_id, "use_excel")
        if not success:
            self.safe_send_message(chat_id, error)
            return False
        file_name = normalize_text(payload.get("file_name")) or "Excel-файл"
        excel_date = normalize_text((payload.get("date_conflict") or {}).get("excel_date"))
        self.safe_send_message(
            chat_id,
            "\n".join([
                "Принято. Импорт будет выполнен по дате из Excel.",
                "",
                f"Файл: {file_name}",
                f"Дата отгрузки: {excel_date or 'из Excel'}",
            ]),
        )
        self.process_queued_telegram_imports()
        return True

    def cancel_telegram_import_date_choice(self, chat_id, event_id):
        success, payload, error = self.resolve_telegram_import_date_choice(chat_id, event_id, "cancel")
        if not success:
            self.safe_send_message(chat_id, error)
            return False
        file_name = normalize_text(payload.get("file_name")) or "Excel-файл"
        self.safe_send_message(
            chat_id,
            "\n".join([
                "Импорт отменён.",
                "",
                f"Файл: {file_name}",
                "Заказы и заявки SkladBot не созданы.",
            ]),
        )
        return True

    def logistics_date_keyboard(self, dates):
        rows = []
        for date_value in dates:
            iso_date = iso_date_from_display(date_value)
            if not iso_date:
                continue
            rows.append([{
                "text": display_date(date_value),
                "callback_data": f"logistics:{iso_date}",
            }])
        return telegram_inline_keyboard(rows)

    def kiz_files_keyboard(self, files):
        rows = []
        for index, item in enumerate(files, start=1):
            if not kiz_progress_completed(item):
                continue
            source_file = normalize_text(item.get("source_file")) or f"Файл {index}"
            text = source_file if len(source_file) <= 40 else source_file[:37] + "..."
            rows.append([{
                "text": f"{index}. {text}",
                "callback_data": f"kiz_file:{index}",
            }])
        return telegram_inline_keyboard(rows)

    def kiz_dates_keyboard(self, dates):
        rows = []
        for index, item in enumerate(dates, start=1):
            date_value = normalize_text(item.get("date"))
            iso_date = iso_date_from_display(date_value)
            if not iso_date:
                continue
            rows.append([{
                "text": f"{index}. {display_date(iso_date)}",
                "callback_data": f"kiz_date:{iso_date}",
            }])
        return telegram_inline_keyboard(rows)

    def kiz_export_mode_keyboard(self):
        return telegram_inline_keyboard([
            [{"text": "По датам отгрузки", "callback_data": "kiz_mode:dates"}],
            [{"text": "По загруженным Excel-файлам", "callback_data": "kiz_mode:files"}],
        ])

    def send_logistics_report(self, chat_id, shipment_date):
        iso_date = iso_date_from_display(shipment_date)
        if not iso_date:
            self.safe_send_message(chat_id, "Не понял дату. Используйте формат 29.05.2026.")
            return False
        report_date = datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d.%m.%Y")
        try:
            content, headers = self.backend_get_bytes("/api/v1/logistics/report", params={"shipment_date": iso_date})
        except httpx.HTTPStatusError as exc:
            self.safe_send_message(
                chat_id,
                f"Не удалось выгрузить отчёт логистики за {report_date}: {backend_http_error_detail(exc)}",
            )
            return False
        except httpx.HTTPError as exc:
            self.safe_send_message(
                chat_id,
                f"Не удалось выгрузить отчёт логистики за {report_date}: backend временно недоступен ({exc.__class__.__name__})",
            )
            return False
        filename = f"TakSklad_логистика_{report_date}.xlsx"
        self.safe_send_document(
            chat_id,
            content,
            filename,
            caption=f"Отчёт логистики за {report_date}",
        )
        return True

    def show_logistics_dates(self, chat_id):
        try:
            dates = self.backend_get("/api/v1/logistics/dates")
        except (httpx.HTTPError, Exception) as exc:
            self.safe_send_message(chat_id, backend_failure_message("Не удалось получить даты логистики", exc))
            return
        dates = dates if isinstance(dates, list) else []
        if not dates:
            self.safe_send_message(chat_id, "Нет доступных дат отгрузки для отчёта логистики.")
            return
        if len(dates) == 1:
            self.send_logistics_report(chat_id, dates[0])
            return
        self.safe_send_message(chat_id, "Выберите дату отгрузки для отчёта логистики:", reply_markup=self.logistics_date_keyboard(dates))

    def show_kiz_export_menu(self, chat_id):
        self.safe_send_message(
            chat_id,
            "Как выгрузить КИЗы?",
            reply_markup=self.kiz_export_mode_keyboard(),
        )

    def show_kiz_dates(self, chat_id):
        try:
            dates = self.backend_get("/api/v1/reports/kiz/dates")
        except (httpx.HTTPError, Exception) as exc:
            self.safe_send_message(chat_id, backend_failure_message("Не удалось получить даты КИЗов", exc))
            return
        dates = dates if isinstance(dates, list) else []
        dates = recent_kiz_dates_for_menu(dates)
        if not dates:
            self.safe_send_message(chat_id, "Нет дат отгрузки с отсканированными КИЗами.")
            return
        if len(dates) == 1:
            self.send_kiz_date_report(chat_id, dates[0].get("date") or "")
            return

        state = self.get_chat_state(chat_id)
        state["kiz_dates"] = [
            {
                "index": index,
                "date": item.get("date") or "",
            }
            for index, item in enumerate(dates, start=1)
        ]
        self.save_chat_state(chat_id, state)
        lines = ["Выберите дату отгрузки для выгрузки КИЗов:"]
        for index, item in enumerate(dates, start=1):
            completed = kiz_progress_completed(item)
            status = "готово" if completed else f"частично, осталось {item.get('remaining_blocks', 0)}"
            lines.append(
                f"{index}. {display_date(item.get('date'))} - "
                f"{item.get('scanned_blocks', 0)}/{item.get('planned_blocks', 0)} блоков, {status}"
            )
        self.safe_send_message(chat_id, "\n".join(lines), reply_markup=self.kiz_dates_keyboard(dates))

    def show_kiz_source_files(self, chat_id):
        try:
            files = self.backend_get("/api/v1/reports/kiz/source-files")
        except (httpx.HTTPError, Exception) as exc:
            self.safe_send_message(chat_id, backend_failure_message("Не удалось получить список Excel-файлов для КИЗов", exc))
            return
        files = files if isinstance(files, list) else []
        files = recent_kiz_source_files_for_menu(files)
        if not files:
            self.safe_send_message(chat_id, "Нет загруженных Excel-файлов для выгрузки КИЗов.")
            return

        state = self.get_chat_state(chat_id)
        state["kiz_files"] = [
            {
                "index": index,
                "source_file": item.get("source_file") or "",
                "source_key": item.get("source_key") or "",
                "completed": kiz_progress_completed(item),
            }
            for index, item in enumerate(files, start=1)
        ]
        self.save_chat_state(chat_id, state)

        ready_lines = []
        pending_lines = []
        for index, item in enumerate(files, start=1):
            dates = ", ".join(display_date(value) for value in item.get("dates") or [])
            completed = kiz_progress_completed(item)
            status = "готов к выгрузке" if completed else f"не готов, осталось {item.get('remaining_blocks', 0)}"
            date_suffix = f" | даты: {dates}" if dates else ""
            target = ready_lines if completed else pending_lines
            target.append(
                f"{index}. {item.get('source_file') or 'без файла'} - "
                f"{item.get('scanned_blocks', 0)}/{item.get('planned_blocks', 0)} блоков, {status}{date_suffix}"
            )
        lines = ["Загруженные Excel-файлы:"]
        if ready_lines:
            lines.extend(["", "Готово к выгрузке:", *ready_lines])
        if pending_lines:
            lines.extend(["", "Ещё не готово:", *pending_lines])

        keyboard = self.kiz_files_keyboard(files)
        self.safe_send_message(
            chat_id,
            "\n".join(lines),
            reply_markup=keyboard if keyboard.get("inline_keyboard") else None,
        )

    def send_kiz_date_report(self, chat_id, shipment_date):
        iso_date = iso_date_from_display(shipment_date)
        if not iso_date:
            self.safe_send_message(chat_id, "Не понял дату. Используйте формат 05.06.2026.")
            return False
        report_date = datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d.%m.%Y")
        try:
            content, headers = self.backend_get_bytes("/api/v1/reports/kiz/date", params={"shipment_date": iso_date})
        except httpx.HTTPStatusError as exc:
            self.safe_send_message(
                chat_id,
                f"Не удалось выгрузить КИЗы за {report_date}: {backend_http_error_detail(exc)}",
            )
            return False
        except httpx.HTTPError as exc:
            self.safe_send_message(
                chat_id,
                f"Не удалось выгрузить КИЗы за {report_date}: backend временно недоступен ({exc.__class__.__name__})",
            )
            return False
        filename_header = urllib.parse.unquote(headers.get("X-TakSklad-Filename") or "")
        filename = filename_header or f"TakSklad_КИЗ_{report_date}.xlsx"
        self.safe_send_document(
            chat_id,
            content,
            filename,
            caption=f"КИЗы за дату отгрузки {report_date}",
        )
        return True

    def send_kiz_range_report(self, chat_id, date_from, date_to):
        iso_from = iso_date_from_display(date_from)
        iso_to = iso_date_from_display(date_to)
        if not iso_from or not iso_to:
            self.safe_send_message(chat_id, "Не понял период. Используйте формат: /kiz 04.06.2026 05.06.2026.")
            return False
        display_from = datetime.strptime(iso_from, "%Y-%m-%d").strftime("%d.%m.%Y")
        display_to = datetime.strptime(iso_to, "%Y-%m-%d").strftime("%d.%m.%Y")
        try:
            content, headers = self.backend_get_bytes(
                "/api/v1/reports/kiz/range",
                params={"date_from": iso_from, "date_to": iso_to},
            )
        except httpx.HTTPStatusError as exc:
            self.safe_send_message(
                chat_id,
                f"Не удалось выгрузить КИЗы за период {display_from}-{display_to}: {backend_http_error_detail(exc)}",
            )
            return False
        except httpx.HTTPError as exc:
            self.safe_send_message(
                chat_id,
                f"Не удалось выгрузить КИЗы за период {display_from}-{display_to}: backend временно недоступен ({exc.__class__.__name__})",
            )
            return False
        filename_header = urllib.parse.unquote(headers.get("X-TakSklad-Filename") or "")
        filename = filename_header or f"TakSklad_КИЗ_{display_from}-{display_to}.xlsx"
        self.safe_send_document(
            chat_id,
            content,
            filename,
            caption=f"КИЗы за период {display_from}-{display_to}",
        )
        return True

    def send_kiz_source_file_report(self, chat_id, source_file, source_key=""):
        source_file = normalize_text(source_file)
        source_key = normalize_text(source_key)
        if not source_file:
            self.safe_send_message(chat_id, "Не выбран исходный файл для выгрузки КИЗов.")
            return False
        params = {"source_file": source_file}
        if source_key:
            params["source_key"] = source_key
        try:
            content, headers = self.backend_get_bytes("/api/v1/reports/kiz/source-file", params=params)
        except (httpx.HTTPError, Exception) as exc:
            self.safe_send_message(chat_id, backend_failure_message(f"Не удалось выгрузить КИЗы по файлу {source_file}", exc))
            return False
        filename_header = urllib.parse.unquote(headers.get("X-TakSklad-Filename") or "")
        filename = filename_header or f"TakSklad_КИЗ_{source_file}.xlsx"
        self.safe_send_document(
            chat_id,
            content,
            filename,
            caption=f"КИЗы по исходному файлу: {source_file}",
        )
        return True

    def send_imports_report(self, chat_id):
        try:
            payload = self.backend_get("/api/v1/imports")
        except (httpx.HTTPError, Exception) as exc:
            self.safe_send_message(chat_id, backend_failure_message("Не удалось получить историю импортов", exc))
            return False
        imports = payload if isinstance(payload, list) else []
        if not imports:
            self.safe_send_message(chat_id, "История импортов пока пустая.")
            return True
        lines = ["Последние импорты TakSklad:"]
        for index, item in enumerate(imports[:10], start=1):
            raw_payload = item.get("raw_payload") or {}
            filename = normalize_text(raw_payload.get("filename")) or "без файла"
            lines.append(
                f"{index}. {filename}: {item.get('status')} "
                f"{item.get('rows_imported', 0)}/{item.get('rows_total', 0)}"
            )
        self.safe_send_message(chat_id, "\n".join(lines))
        return True

    def send_status_report(self, chat_id):
        try:
            payload = self.backend_get("/api/v1/reports/day")
            active_orders = self.backend_get("/api/v1/orders/active")
        except (httpx.HTTPError, Exception) as exc:
            self.safe_send_message(chat_id, backend_failure_message("Не удалось получить статус TakSklad", exc))
            return False
        totals = payload.get("totals") or {}
        report_date = display_date(payload.get("report_date")) or "сегодня"
        active_summary = summarize_active_orders_by_date(active_orders if isinstance(active_orders, list) else [])
        lines = [
            f"Статус TakSklad за {report_date}",
            "",
            f"Сегодня выполнено заказов: {totals.get('completed_orders', 0)}",
            f"КИЗов сегодня: {totals.get('scanned_today', 0)}",
            f"Всего КИЗов в отчёте: {totals.get('scan_codes', 0)}",
        ]
        if not active_summary:
            lines.extend(["", "Активных заказов для КИЗов нет."])
            self.safe_send_message(chat_id, "\n".join(lines))
            return True

        total_active = {
            "orders": 0,
            "items": 0,
            "planned_blocks": 0,
            "scanned_blocks": 0,
            "remaining_blocks": 0,
            "missing_skladbot": 0,
            "total_price": 0,
        }
        lines.extend(["", "Активные заказы для КИЗов:"])
        for date_key, values in sorted(active_summary.items()):
            for key in total_active:
                total_active[key] += values[key]
            lines.append(
                f"- {display_date(date_key) or date_key}: "
                f"{values['orders']} заказов, "
                f"{values['scanned_blocks']}/{values['planned_blocks']} блоков, "
                f"осталось {values['remaining_blocks']}, "
                f"без SkladBot {values['missing_skladbot']}, "
                f"{format_money(values['total_price'])} сум"
            )

        lines.extend([
            "",
            "Итого активно:",
            f"Заказов: {total_active['orders']}",
            f"Позиций: {total_active['items']}",
            f"Блоков: {total_active['scanned_blocks']} / {total_active['planned_blocks']}",
            f"Осталось блоков: {total_active['remaining_blocks']}",
            f"Без номера SkladBot: {total_active['missing_skladbot']}",
            f"Сумма активных заказов: {format_money(total_active['total_price'])} сум",
        ])
        self.safe_send_message(chat_id, "\n".join(lines))
        return True

    def show_manual_menu(self, chat_id):
        if not self.ensure_admin_chat(chat_id):
            return False
        self.safe_send_message(
            chat_id,
            "\n".join([
                "Ручное управление TakSklad",
                "",
                "Удалять можно только активные заказы без сканов КИЗов.",
                "Если склад уже начал обрабатывать заказ, бот его не удалит.",
            ]),
            reply_markup=telegram_manual_menu_keyboard(),
        )
        return True

    def clear_manual_flow(self, chat_id):
        state = self.get_chat_state(chat_id)
        state["manual_flow"] = {}
        self.save_chat_state(chat_id, state)
        cache = getattr(self, "manual_flow_cache", None)
        if isinstance(cache, dict):
            cache[str(chat_id)] = {}

    def save_manual_flow(self, chat_id, flow):
        state = self.get_chat_state(chat_id)
        state["manual_flow"] = flow or {}
        self.save_chat_state(chat_id, state)
        cache = getattr(self, "manual_flow_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self.manual_flow_cache = cache
        cache[str(chat_id)] = flow or {}

    def start_manual_add_order(self, chat_id):
        if not self.ensure_admin_chat(chat_id):
            return False
        flow = {
            "mode": "add_order",
            "step": "order_date",
            "data": {
                "manual_id": str(uuid.uuid4()),
                "items": [],
            },
        }
        self.save_manual_flow(chat_id, flow)
        self.safe_send_message(
            chat_id,
            "Введите дату отгрузки в формате ДД.ММ.ГГГГ.",
        )
        return True

    def handle_manual_text(self, chat_id, text):
        cache = getattr(self, "manual_flow_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self.manual_flow_cache = cache
        flow = cache.get(str(chat_id)) or {}
        if not flow and self.configured:
            try:
                state = self.get_chat_state(chat_id)
            except Exception:
                logging.warning("Telegram worker: failed to load manual flow state", exc_info=True)
                state = {}
            flow = (state.get("manual_flow") if isinstance(state, dict) else {}) or {}
            if flow:
                cache[str(chat_id)] = flow
        if not flow:
            return False
        if flow.get("mode") == "add_order" and not self.ensure_admin_chat(chat_id):
            self.clear_manual_flow(chat_id)
            return True
        if text_matches(text, "/cancel", "отмена", "cancel"):
            self.clear_manual_flow(chat_id)
            self.safe_send_message(chat_id, "Ручное действие отменено.")
            return True
        if flow.get("mode") == "add_order":
            return self.handle_manual_add_text(chat_id, text, flow)
        self.clear_manual_flow(chat_id)
        self.safe_send_message(chat_id, "Ручное действие устарело. Начните заново через меню.")
        return True

    def handle_manual_add_text(self, chat_id, text, flow):
        data = flow.setdefault("data", {})
        step = normalize_text(flow.get("step"))
        if step == "order_date":
            order_date = parse_date_from_text(text)
            if not order_date:
                self.safe_send_message(chat_id, "Дата не распознана. Введите дату в формате ДД.ММ.ГГГГ.")
                return True
            data["order_date"] = order_date
            flow["step"] = "payment_type"
            self.save_manual_flow(chat_id, flow)
            self.safe_send_message(chat_id, "Выберите тип оплаты:", reply_markup=telegram_manual_payment_keyboard())
            return True
        if step == "payment_type":
            payment_type = self.manual_payment_type_from_text(text)
            if not payment_type:
                self.safe_send_message(chat_id, "Выберите тип оплаты кнопкой.")
                return True
            data["payment_type"] = payment_type
            flow["step"] = "client"
            self.save_manual_flow(chat_id, flow)
            self.safe_send_message(chat_id, "Введите юрлицо клиента.")
            return True
        if step == "client":
            if not text:
                self.safe_send_message(chat_id, "Юрлицо не может быть пустым.")
                return True
            data["client"] = text
            flow["step"] = "address"
            self.save_manual_flow(chat_id, flow)
            self.safe_send_message(chat_id, "Введите адрес или координаты. Если самовывоз, напишите: Самовывоз со склада.")
            return True
        if step == "address":
            if not text:
                self.safe_send_message(chat_id, "Адрес не может быть пустым.")
                return True
            address, coordinates = manual_address_and_coordinates(text)
            data["address"] = address
            data["coordinates"] = coordinates
            flow["step"] = "representative"
            self.save_manual_flow(chat_id, flow)
            self.safe_send_message(chat_id, "Введите торгового представителя.")
            return True
        if step == "representative":
            if not text:
                self.safe_send_message(chat_id, "Торговый представитель не может быть пустым.")
                return True
            data["representative"] = text
            flow["step"] = "product"
            self.save_manual_flow(chat_id, flow)
            self.safe_send_message(chat_id, "Выберите SKU:", reply_markup=telegram_manual_product_keyboard())
            return True
        if step == "blocks":
            blocks = parse_int(text)
            if blocks <= 0:
                self.safe_send_message(chat_id, "Введите количество блоков числом больше 0.")
                return True
            product_key = normalize_text(data.get("selected_product_key"))
            product = TELEGRAM_MANUAL_PRODUCTS.get(product_key)
            if not product:
                flow["step"] = "product"
                self.save_manual_flow(chat_id, flow)
                self.safe_send_message(chat_id, "SKU не выбран. Выберите SKU:", reply_markup=telegram_manual_product_keyboard())
                return True
            data.setdefault("items", []).append({"product_key": product_key, "product": product, "blocks": blocks})
            data.pop("selected_product_key", None)
            flow["step"] = "review"
            self.save_manual_flow(chat_id, flow)
            self.safe_send_message(chat_id, manual_order_summary(flow), reply_markup=telegram_manual_add_next_keyboard())
            return True
        self.safe_send_message(chat_id, "Используйте кнопки под сообщением.")
        return True

    def manual_payment_type_from_text(self, value):
        text = normalize_text(value).casefold()
        for key, label in TELEGRAM_MANUAL_PAYMENT_TYPES.items():
            if text in {key.casefold(), label.casefold()}:
                return label
        return ""

    def set_manual_payment_type(self, chat_id, key):
        state = self.get_chat_state(chat_id)
        flow = state.get("manual_flow") or {}
        if flow.get("mode") != "add_order" or flow.get("step") != "payment_type":
            self.safe_send_message(chat_id, "Выбор типа оплаты устарел. Начните заново через меню.")
            return False
        payment_type = TELEGRAM_MANUAL_PAYMENT_TYPES.get(key)
        if not payment_type:
            self.safe_send_message(chat_id, "Неизвестный тип оплаты. Выберите заново.")
            return False
        flow.setdefault("data", {})["payment_type"] = payment_type
        flow["step"] = "client"
        self.save_manual_flow(chat_id, flow)
        self.safe_send_message(chat_id, "Введите юрлицо клиента.")
        return True

    def set_manual_product(self, chat_id, key):
        state = self.get_chat_state(chat_id)
        flow = state.get("manual_flow") or {}
        if flow.get("mode") != "add_order" or flow.get("step") not in {"product", "review"}:
            self.safe_send_message(chat_id, "Выбор SKU устарел. Начните заново через меню.")
            return False
        product = TELEGRAM_MANUAL_PRODUCTS.get(key)
        if not product:
            self.safe_send_message(chat_id, "Неизвестный SKU. Выберите заново.")
            return False
        flow.setdefault("data", {})["selected_product_key"] = key
        flow["step"] = "blocks"
        self.save_manual_flow(chat_id, flow)
        self.safe_send_message(chat_id, f"Введите количество блоков для {product}.")
        return True

    def show_manual_product_choice(self, chat_id):
        state = self.get_chat_state(chat_id)
        flow = state.get("manual_flow") or {}
        if flow.get("mode") != "add_order":
            self.safe_send_message(chat_id, "Ручной заказ не найден. Начните заново через меню.")
            return False
        flow["step"] = "product"
        self.save_manual_flow(chat_id, flow)
        self.safe_send_message(chat_id, "Выберите SKU:", reply_markup=telegram_manual_product_keyboard())
        return True

    def create_manual_order(self, chat_id):
        if not self.ensure_admin_chat(chat_id):
            return False
        state = self.get_chat_state(chat_id)
        flow = state.get("manual_flow") or {}
        data = flow.get("data") or {}
        if flow.get("mode") != "add_order" or not data.get("items"):
            self.safe_send_message(chat_id, "В ручном заказе нет позиций. Добавьте SKU и количество.")
            return False
        required_fields = ["order_date", "payment_type", "client", "address", "representative"]
        missing = [field for field in required_fields if not normalize_text(data.get(field))]
        if missing:
            self.safe_send_message(chat_id, "Ручной заказ заполнен не полностью. Начните заново через меню.")
            return False
        payload = build_manual_import_payload(chat_id, flow)
        try:
            result = self.backend_post("/api/v1/imports", payload)
        except httpx.HTTPStatusError as exc:
            detail = backend_http_error_detail(exc)
            self.safe_send_message(chat_id, f"Не удалось создать ручной заказ: {detail or exc}")
            return False
        except httpx.HTTPError as exc:
            self.safe_send_message(chat_id, f"Не удалось создать ручной заказ: {exc.__class__.__name__}")
            return False
        self.clear_manual_flow(chat_id)
        self.safe_send_message(
            chat_id,
            "\n".join([
                "Заказ создан в TakSklad.",
                f"Заказов добавлено: {result.get('orders_created', 0)}",
                f"Позиций добавлено: {result.get('items_created', 0)}",
                f"SkladBot: {result.get('skladbot_dry_run_status') or 'queued'}",
            ]),
        )
        return True

    def show_manual_delete_orders(self, chat_id):
        if not self.ensure_admin_chat(chat_id):
            return False
        orders = self.backend_get("/api/v1/orders/active")
        orders = orders if isinstance(orders, list) else []
        state = self.get_chat_state(chat_id)
        state["manual_delete_orders"] = orders[:20]
        self.save_chat_state(chat_id, state)
        if not orders:
            self.safe_send_message(chat_id, "Активных заказов для удаления нет.")
            return True
        lines = [
            "Выберите активный заказ для удаления.",
            "",
            "Важно: если в заказе есть хотя бы один скан КИЗа, удалить его через бот нельзя.",
        ]
        for index, order in enumerate(orders[:20], start=1):
            lines.append(
                f"{index}. {display_date(order.get('order_date')) or 'без даты'} | "
                f"{normalize_text(order.get('client')) or 'без клиента'} | "
                f"{order_scanned_blocks(order)}/{order_planned_blocks(order)} блок."
            )
        self.safe_send_message(chat_id, "\n".join(lines), reply_markup=telegram_manual_delete_keyboard(orders[:20]))
        return True

    def select_manual_delete_order(self, chat_id, index):
        state = self.get_chat_state(chat_id)
        orders = state.get("manual_delete_orders") or []
        if index < 1 or index > len(orders):
            self.safe_send_message(chat_id, "Заказ из списка не найден. Откройте список заново.")
            return False
        order = orders[index - 1]
        if order_scanned_blocks(order) > 0:
            self.safe_send_message(
                chat_id,
                "\n".join([
                    "Склад уже начал обрабатывать заказ: есть сканы КИЗов.",
                    "Через Telegram удалить нельзя, чтобы не потерять данные.",
                ]),
            )
            return False
        order_id = normalize_text(order.get("id"))
        lines = [
            "Подтвердите удаление активного заказа:",
            "",
            f"Дата: {display_date(order.get('order_date')) or 'без даты'}",
            f"Клиент: {normalize_text(order.get('client')) or 'без клиента'}",
            f"SkladBot: {normalize_text(order.get('skladbot_request_number')) or 'нет'}",
            f"Блоков: {order_planned_blocks(order)}",
            "",
            "Из SkladBot бот удалить не может. Если заявка там создана, её нужно удалить вручную.",
        ]
        self.safe_send_message(chat_id, "\n".join(lines), reply_markup=telegram_manual_delete_confirm_keyboard(order_id))
        return True

    def confirm_manual_delete_order(self, chat_id, order_id):
        if not self.ensure_admin_chat(chat_id):
            return False
        order_id = normalize_text(order_id)
        if not order_id:
            self.safe_send_message(chat_id, "ID заказа не найден. Откройте список заново.")
            return False
        order, error = self.find_manual_delete_order_for_confirmation(chat_id, order_id)
        if error:
            self.safe_send_message(chat_id, error)
            return False
        if order and order_scanned_blocks(order) > 0:
            self.safe_send_message(
                chat_id,
                "\n".join([
                    "Склад уже начал обрабатывать заказ: есть сканы КИЗов.",
                    "Через Telegram удалить нельзя, чтобы не потерять данные.",
                ]),
            )
            return False
        payload = {
            "reason": "Удалено вручную через Telegram",
            "actor": "telegram",
            "source": "telegram",
            "idempotency_key": f"telegram:manual_delete:{chat_id}:{order_id}",
        }
        try:
            result = self.backend_post(f"/api/v1/admin/orders/{order_id}/delete-active", payload)
        except httpx.HTTPStatusError as exc:
            detail = backend_http_error_detail(exc)
            self.safe_send_message(chat_id, f"Заказ не удалён: {detail or exc}")
            return False
        except httpx.HTTPError as exc:
            self.safe_send_message(chat_id, f"Заказ не удалён: {exc.__class__.__name__}")
            return False
        state = self.get_chat_state(chat_id)
        state["manual_delete_orders"] = []
        self.save_chat_state(chat_id, state)
        lines = ["Заказ удалён из TakSklad и поставлен на удаление из Google Sheets."]
        skladbot_number = normalize_text(result.get("skladbot_request_number"))
        if skladbot_number:
            lines.append(f"В SkladBot заявка {skladbot_number} осталась, её нужно удалить вручную.")
        else:
            lines.append("SkladBot-заявки у заказа не было.")
        self.safe_send_message(chat_id, "\n".join(lines))
        return True

    def find_manual_delete_order_for_confirmation(self, chat_id, order_id):
        state = self.get_chat_state(chat_id)
        orders = state.get("manual_delete_orders") or []
        for order in orders:
            if normalize_text(order.get("id")) == order_id:
                return order, ""
        try:
            active_orders = self.backend_get("/api/v1/orders/active")
        except (httpx.HTTPError, Exception) as exc:
            return None, backend_failure_message("Не удалось проверить заказ перед удалением", exc)
        for order in active_orders if isinstance(active_orders, list) else []:
            if normalize_text(order.get("id")) == order_id:
                return order, ""
        return None, "Список удаления устарел. Откройте активные заказы заново."

    def handle_manual_callback(self, chat_id, data):
        action = normalize_text(data).replace(TELEGRAM_MANUAL_CALLBACK_PREFIX, "", 1)
        if action == "cancel":
            self.clear_manual_flow(chat_id)
            state = self.get_chat_state(chat_id)
            state["manual_delete_orders"] = []
            self.save_chat_state(chat_id, state)
            self.safe_send_message(chat_id, "Ручное действие отменено.")
            return True
        if not self.ensure_admin_chat(chat_id):
            return False
        if action == "add":
            return self.start_manual_add_order(chat_id)
        if action == "delete":
            return self.show_manual_delete_orders(chat_id)
        if action.startswith("payment:"):
            return self.set_manual_payment_type(chat_id, action.split(":", 1)[1])
        if action.startswith("product:"):
            return self.set_manual_product(chat_id, action.split(":", 1)[1])
        if action == "add_more":
            return self.show_manual_product_choice(chat_id)
        if action == "create":
            return self.create_manual_order(chat_id)
        if action.startswith("delete_confirm:"):
            return self.confirm_manual_delete_order(chat_id, action.split(":", 1)[1])
        if action.startswith("delete:"):
            return self.select_manual_delete_order(chat_id, parse_int(action.split(":", 1)[1]))
        self.safe_send_message(chat_id, "Ручное действие устарело. Начните заново через меню.")
        return False

    def send_skladbot_daily_report(self, chat_id, report_date=None, scheduled=False):
        report_date = coerce_report_date(report_date or skladbot_daily_report.business_today())
        report_date_text = report_date.strftime("%d.%m.%Y")
        if not scheduled:
            self.safe_send_message(chat_id, f"Собираю SkladBot отчет за {report_date_text}.")
        reported_request_ids = (
            load_skladbot_daily_reported_request_ids_before(report_date)
            if scheduled
            else set()
        )
        report = skladbot_daily_report.collect_skladbot_daily_report(
            report_date=report_date,
            reported_request_ids=reported_request_ids,
        )
        content, filename = skladbot_daily_report.build_skladbot_daily_report_xlsx(report)
        self.safe_send_message(chat_id, skladbot_daily_report.build_skladbot_daily_report_message(report))
        document = self.safe_send_document(
            chat_id,
            content,
            filename,
            caption=f"SkladBot отчет за {report_date_text}",
        )
        if document is not None and scheduled:
            mark_skladbot_daily_report_requests_reported(report, chat_id=chat_id)
        return document is not None

    def scheduled_skladbot_daily_report_is_due(self, now=None):
        if not getattr(self, "skladbot_daily_report_enabled", False):
            return False
        if not getattr(self, "skladbot_daily_report_chat_ids", set()):
            return False
        now = now or datetime.now(skladbot_daily_report.business_timezone())
        if now.tzinfo is None:
            now = now.replace(tzinfo=skladbot_daily_report.business_timezone())
        scheduled_minutes = getattr(self, "skladbot_daily_report_hour", 22) * 60 + getattr(self, "skladbot_daily_report_minute", 0)
        current_minutes = now.hour * 60 + now.minute
        return current_minutes >= scheduled_minutes

    def skladbot_daily_report_idempotency_key(self, chat_id, report_date):
        return f"skladbot_daily_report:{report_date.isoformat()}:{chat_id}"

    def claim_scheduled_skladbot_daily_report(self, chat_id, report_date, now=None):
        now = now or datetime.now(skladbot_daily_report.business_timezone())
        now_utc = ensure_aware_utc(now.astimezone(timezone.utc) if now.tzinfo else now)
        idempotency_key = self.skladbot_daily_report_idempotency_key(chat_id, report_date)
        with SessionLocal() as db:
            reset_stale_processing_events(
                db,
                event_types=(SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,),
                action="skladbot_daily_report_stale_reset",
                last_error="stale SkladBot daily report reset",
                now=now_utc,
            )
            event = db.execute(
                select(PendingEvent).where(PendingEvent.idempotency_key == idempotency_key)
            ).scalars().first()
            if event is not None and event.status in {"completed", "processing"}:
                return ""
            if event is not None and event.status == "failed":
                updated_at = ensure_aware_utc(event.updated_at)
                retry_minutes = getattr(self, "skladbot_daily_report_retry_minutes", 15)
                if updated_at and now_utc and now_utc - updated_at < timedelta(minutes=retry_minutes):
                    return ""
            payload = {
                "chat_id": str(chat_id),
                "report_date": report_date.isoformat(),
                "scheduled_at": f"{getattr(self, 'skladbot_daily_report_hour', 22):02d}:{getattr(self, 'skladbot_daily_report_minute', 0):02d}",
                "claimed_at": now_utc.isoformat() if now_utc else "",
            }
            if event is None:
                event = PendingEvent(
                    event_type=SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
                    idempotency_key=idempotency_key,
                    status="processing",
                    attempts=1,
                    payload=payload,
                    last_error=None,
                )
                db.add(event)
            else:
                event.status = "processing"
                event.attempts = (event.attempts or 0) + 1
                event.payload = {**(event.payload or {}), **payload}
                event.last_error = None
            db.commit()
            return str(event.id)

    def finish_scheduled_skladbot_daily_report(self, event_id, success, error=""):
        if not event_id:
            return
        with SessionLocal() as db:
            event = db.get(PendingEvent, event_id if isinstance(event_id, uuid.UUID) else uuid.UUID(str(event_id)))
            if event is None:
                return
            event.status = "completed" if success else "failed"
            event.last_error = "" if success else normalize_text(error)
            payload = dict(event.payload or {})
            payload["finished_at"] = datetime.now(timezone.utc).isoformat()
            payload["success"] = bool(success)
            if error:
                payload["error"] = normalize_text(error)
            event.payload = payload
            db.commit()

    def run_scheduled_daily_reconciliation(self, chat_id, report_date):
        if not getattr(self, "daily_reconciliation_enabled", False):
            return None
        alert_chat_ids = sorted(getattr(self, "daily_reconciliation_chat_ids", set()) or {str(chat_id)})
        try:
            return run_daily_reconciliation(report_date=report_date, alert_chat_ids=alert_chat_ids)
        except Exception as exc:
            logging.exception("Telegram worker: scheduled daily reconciliation failed")
            return {
                "status": "failed",
                "error": normalize_text(exc) or exc.__class__.__name__,
            }

    def send_due_skladbot_daily_reports(self, now=None):
        now = now or datetime.now(skladbot_daily_report.business_timezone())
        if not self.scheduled_skladbot_daily_report_is_due(now):
            return 0
        report_date = now.date()
        sent = 0
        for chat_id in sorted(getattr(self, "skladbot_daily_report_chat_ids", set())):
            event_id = self.claim_scheduled_skladbot_daily_report(chat_id, report_date, now=now)
            if not event_id:
                continue
            try:
                success = self.send_skladbot_daily_report(chat_id, report_date=report_date, scheduled=True)
            except Exception as exc:
                error = normalize_text(exc) or exc.__class__.__name__
                logging.exception("Telegram worker: scheduled SkladBot daily report failed")
                self.safe_send_message(chat_id, f"Не удалось отправить ежедневный SkladBot отчет: {error[:500]}")
                self.finish_scheduled_skladbot_daily_report(event_id, False, error)
                continue
            self.finish_scheduled_skladbot_daily_report(event_id, success, "" if success else "telegram_send_failed")
            if success:
                self.run_scheduled_daily_reconciliation(chat_id, report_date)
                sent += 1
        return sent

    def send_kiz_source_file_by_index(self, chat_id, text):
        index = parse_int(text.replace(TELEGRAM_KIZ_FILE_PREFIX, "", 1))
        state = self.get_chat_state(chat_id)
        files = state.get("kiz_files") or []
        selected = next((item for item in files if parse_int(item.get("index")) == index), None)
        if not selected:
            self.safe_send_message(chat_id, f"Не нашёл выбранный файл. Нажмите «{TELEGRAM_BUTTON_KIZ_BY_FILES}» ещё раз.")
            return False
        return self.send_kiz_source_file_report(
            chat_id,
            selected.get("source_file") or "",
            selected.get("source_key") or "",
        )

    def send_kiz_date_by_index(self, chat_id, text):
        index = parse_int(text.replace(TELEGRAM_KIZ_DATE_PREFIX, "", 1))
        state = self.get_chat_state(chat_id)
        dates = state.get("kiz_dates") or []
        selected = next((item for item in dates if parse_int(item.get("index")) == index), None)
        if not selected:
            self.safe_send_message(chat_id, f"Не нашёл выбранную дату. Нажмите «{TELEGRAM_BUTTON_KIZ_BY_FILES}» ещё раз.")
            return False
        return self.send_kiz_date_report(chat_id, selected.get("date") or "")

    def send_backend_diagnostics_log(self, chat_id):
        content, headers = self.backend_get_bytes("/api/v1/diagnostics/logs", params={"limit": 100})
        filename_header = urllib.parse.unquote(headers.get("X-TakSklad-Filename") or "")
        filename = filename_header or "TakSklad_backend_diagnostics.txt"
        self.safe_send_document(
            chat_id,
            content,
            filename,
            caption="TakSklad: критичные backend-события и ошибки очередей",
        )
        return True

    def telegram_file_info(self, file_id):
        file_id = normalize_text(file_id)
        if not file_id:
            raise ValueError("Telegram не передал file_id документа")
        result = self.telegram_request("getFile", {"file_id": file_id})
        if not isinstance(result, dict) or not normalize_text(result.get("file_path")):
            raise RuntimeError("Telegram не вернул путь к файлу")
        return result

    def download_telegram_document(self, document, destination_path):
        file_info = self.telegram_file_info(document.get("file_id"))
        file_path = normalize_text(file_info.get("file_path"))
        quoted_path = urllib.parse.quote(file_path, safe="/")
        url = f"https://api.telegram.org/file/bot{self.token}/{quoted_path}"
        with httpx.Client(timeout=self.file_timeout, follow_redirects=True) as client:
            try:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    total = 0
                    with open(destination_path, "wb") as output:
                        for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if self.max_file_size and total > self.max_file_size:
                                raise ValueError("Файл слишком большой для Telegram import")
                            output.write(chunk)
            except httpx.HTTPError:
                raise RuntimeError("Не удалось скачать файл из Telegram") from None

    def import_telegram_document(self, chat_id, document, shipment_date="", event_id=None):
        file_name = normalize_text(document.get("file_name")) or "telegram_import.xlsx"
        if not is_supported_excel_file_name(file_name):
            self.safe_send_message(chat_id, "Файл не импортирован. Отправьте Excel-файл в формате .xlsx или .xlsm.")
            return False, "unsupported_file_type"

        suffix = Path(file_name).suffix.lower() or ".xlsx"
        temp_file = tempfile.NamedTemporaryFile(prefix="taksklad_telegram_import_", suffix=suffix, delete=False)
        temp_path = temp_file.name
        temp_file.close()

        try:
            self.safe_send_message(chat_id, f"Начинаю импорт Excel-файла из очереди: {file_name}")
            self.download_telegram_document(document, temp_path)
            import_payload = excel_file_to_import_payload(
                temp_path,
                file_name=file_name,
                source="telegram",
                shipment_date=shipment_date,
                force_shipment_date=bool(parse_date_from_text(shipment_date)),
            )
            meta = import_payload.pop("meta", {})
            rows = import_payload.get("rows") or []
            imported_blocks = sum(parse_int(row.get("Кол-во блок")) for row in rows if isinstance(row, dict))
            import_payload["telegram_chat_id"] = normalize_text(chat_id)
            if event_id:
                import_payload["telegram_event_id"] = normalize_text(event_id)
            result = self.backend_post("/api/v1/imports", import_payload)
            result_status = normalize_text(result.get("status"))
            if result_status == "failed":
                errors = result.get("errors") or []
                reason = normalize_text(errors[0] if errors else "") or "backend import status failed"
                self.safe_send_message(chat_id, telegram_import_failure_message(file_name, reason))
                return False, reason
            warnings = meta.get("warnings") or []
            lines = [
                "TakSklad: Excel импортирован через Telegram",
                "",
                f"Файл: {file_name}",
                f"Строк в файле: {meta.get('source_rows_count', 0)}",
                f"Строк отправлено в backend: {len(rows)}",
                f"Блоков импортировано: {imported_blocks}",
                f"Дата отгрузки: {meta.get('shipment_date') or shipment_date or 'не задана'}",
                f"Позиции добавлены: {result.get('items_created', 0)}",
                f"Заказы добавлены: {result.get('orders_created', 0)}",
                f"Адреса в backend обновлены: {result.get('backend_address_updates', 0)}",
                f"Повторы пропущены: {result.get('duplicate_rows', 0)}",
                f"Ошибочные строки: {result.get('invalid_rows', 0)}",
                f"Статус: {result.get('status', '')}",
            ]
            google_sheets_status = normalize_text(result.get("google_sheets_status"))
            if google_sheets_status == "completed":
                lines.append(
                    f"Google Sheets: записано {result.get('google_sheets_imported', 0)}, "
                    f"повторы {result.get('google_sheets_duplicates', 0)}, "
                    f"адреса обновлены {result.get('google_sheets_updated', 0)}"
                )
            elif google_sheets_status == "skipped":
                lines.append("Google Sheets: новых строк нет")
            elif google_sheets_status == "disabled":
                lines.append("Google Sheets: экспорт отключён на backend")
            elif google_sheets_status == "error":
                error_text = normalize_text(result.get("google_sheets_error")) or "подробности в логе backend"
                lines.append(f"Google Sheets: ошибка, строки не записаны ({error_text})")
            errors = result.get("errors") or []
            if warnings:
                lines.extend(["", "Предупреждения:", "\n".join(warnings[:5])])
            if errors:
                lines.extend(["", "Ошибки:", "\n".join(errors[:5])])
            self.safe_send_message(chat_id, "\n".join(lines))
            return True, ""
        except ExcelDateConflictError as exc:
            logging.warning("Telegram worker: Excel import date conflict: %s", exc)
            if event_id:
                self.mark_telegram_import_waiting_date_choice(event_id, exc)
                self.send_telegram_import_date_conflict_choice(chat_id, file_name, event_id, exc)
                return None, "date_choice_required"
            self.safe_send_message(
                chat_id,
                telegram_import_failure_message(file_name, exc),
            )
            return False, str(exc)
        except Exception as exc:
            logging.exception("Telegram worker: Excel import failed")
            self.safe_send_message(chat_id, telegram_import_failure_message(file_name, exc))
            return False, str(exc)
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    def enqueue_telegram_document(self, chat_id, document, update_id=None, shipment_date=""):
        file_name = normalize_text(document.get("file_name")) or "telegram_import.xlsx"
        if not is_supported_excel_file_name(file_name):
            self.safe_send_message(chat_id, "Файл не импортирован. Отправьте Excel-файл в формате .xlsx или .xlsm.")
            return False

        with SessionLocal() as db:
            existing_event = find_existing_telegram_import_event(db, document, update_id)
            if existing_event is not None:
                if existing_event.status == "failed":
                    payload = dict(existing_event.payload or {})
                    payload["shipment_date"] = ""
                    payload["shipment_date_source"] = ""
                    payload["requeued_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    existing_event.payload = payload
                    existing_event.status = TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS
                    existing_event.last_error = ""
                    existing_event.attempts = 0
                    db.commit()
                if existing_event.status == TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS:
                    self.safe_send_message(
                        chat_id,
                        "\n".join([
                            "Excel-файл уже получен и ждёт дату отгрузки.",
                            "",
                            f"Файл: {file_name}",
                            "Отправьте дату одним сообщением в формате ДД.ММ.ГГГГ.",
                            "Пример: 09.06.2026",
                        ]),
                    )
                    return True
                if existing_event.status == TELEGRAM_EXCEL_IMPORT_WAITING_DATE_CHOICE_STATUS:
                    payload = existing_event.payload or {}
                    conflict_payload = payload.get("date_conflict") or {}
                    telegram_date = normalize_text(conflict_payload.get("telegram_date"))
                    excel_date = normalize_text(conflict_payload.get("excel_date"))
                    if telegram_date and excel_date:
                        conflict = ExcelDateConflictError(telegram_date, excel_date)
                        self.send_telegram_import_date_conflict_choice(
                            chat_id,
                            file_name,
                            str(existing_event.id),
                            conflict,
                        )
                        return True
                self.safe_send_message(
                    chat_id,
                    "\n".join([
                        "Excel-файл уже есть в очереди импорта.",
                        "",
                        f"Файл: {file_name}",
                        "Дата отгрузки: уже задана или импорт завершён",
                    ]),
                )
                return True

            event = PendingEvent(
                event_type=TELEGRAM_EXCEL_IMPORT_EVENT_TYPE,
                status=TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS,
                payload={
                    "chat_id": normalize_text(chat_id),
                    "document": document,
                    "file_name": file_name,
                    "update_id": update_id,
                    "shipment_date": "",
                    "shipment_date_source": "",
                    "queued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
            db.add(event)
            db.commit()

        self.safe_send_message(
            chat_id,
            "\n".join([
                "Excel-файл получен.",
                "",
                f"Файл: {file_name}",
                "Укажите дату отгрузки одним сообщением в формате ДД.ММ.ГГГГ.",
                "Пример: 09.06.2026",
                "Заказы и заявки SkladBot не будут созданы до ввода даты.",
            ]),
        )
        return True

    def take_next_telegram_import_event(self):
        with SessionLocal() as db:
            stmt = (
                select(PendingEvent)
                .where(PendingEvent.event_type == TELEGRAM_EXCEL_IMPORT_EVENT_TYPE)
                .where(PendingEvent.status.in_(TELEGRAM_EXCEL_IMPORT_ACTIVE_STATUSES))
                .order_by(PendingEvent.created_at, PendingEvent.id)
            )
            if db.bind.dialect.name == "postgresql":
                stmt = stmt.with_for_update(skip_locked=True)
            event = db.execute(stmt).scalars().first()
            if event is None:
                return None

            event.status = "processing"
            event.attempts = (event.attempts or 0) + 1
            payload = event.payload or {}
            event_id = event.id
            db.commit()
            return {"id": event_id, "payload": payload}

    def finish_telegram_import_event(self, event_id, success, error=""):
        with SessionLocal() as db:
            event = db.get(PendingEvent, event_id if isinstance(event_id, uuid.UUID) else uuid.UUID(str(event_id)))
            if event is None:
                return
            event.status = "completed" if success else "failed"
            event.last_error = "" if success else normalize_text(error)
            if not success:
                ensure_telegram_import_event_incident(db, event, error)
            db.commit()

    def take_next_telegram_notification_event(self):
        with SessionLocal() as db:
            stmt = (
                select(PendingEvent)
                .where(PendingEvent.event_type == TELEGRAM_NOTIFICATION_EVENT_TYPE)
                .where(PendingEvent.status.in_(TELEGRAM_NOTIFICATION_ACTIVE_STATUSES))
                .order_by(PendingEvent.created_at, PendingEvent.id)
            )
            if db.bind.dialect.name == "postgresql":
                stmt = stmt.with_for_update(skip_locked=True)
            event = db.execute(stmt).scalars().first()
            if event is None:
                return None
            event.status = "processing"
            event.attempts = (event.attempts or 0) + 1
            payload = event.payload or {}
            event_id = event.id
            db.commit()
            return {"id": event_id, "payload": payload}

    def finish_telegram_notification_event(self, event_id, success, error=""):
        with SessionLocal() as db:
            event = db.get(PendingEvent, event_id if isinstance(event_id, uuid.UUID) else uuid.UUID(str(event_id)))
            if event is None:
                return
            event.status = "completed" if success else "failed"
            event.last_error = "" if success else normalize_text(error)
            db.commit()

    def reset_stale_telegram_notification_events(self):
        with SessionLocal() as db:
            return reset_stale_processing_events(
                db,
                event_types=(TELEGRAM_NOTIFICATION_EVENT_TYPE,),
                action="telegram_notification_stale_reset",
                last_error="stale Telegram notification reset",
            )

    def telegram_notification_targets(self, payload):
        chat_id = normalize_text((payload or {}).get("chat_id"))
        if chat_id:
            return [chat_id]
        fallback = sorted(getattr(self, "admin_chat_ids", set()) or getattr(self, "allowed_chat_ids", set()))
        return [normalize_text(value) for value in fallback if normalize_text(value)]

    def process_pending_telegram_notifications(self):
        self.reset_stale_telegram_notification_events()
        processed = 0
        while True:
            event = self.take_next_telegram_notification_event()
            if not event:
                break
            payload = event.get("payload") or {}
            text = normalize_text(payload.get("text"))
            targets = self.telegram_notification_targets(payload)
            if not text:
                self.finish_telegram_notification_event(event["id"], False, "telegram notification text is empty")
                processed += 1
                continue
            if not targets:
                self.finish_telegram_notification_event(event["id"], False, "telegram notification target chat is empty")
                processed += 1
                continue
            try:
                for chat_id in targets:
                    self.send_message(chat_id, text)
                self.finish_telegram_notification_event(event["id"], True, "")
            except Exception as exc:
                logging.exception("Telegram worker: queued notification failed")
                self.finish_telegram_notification_event(event["id"], False, str(exc))
            processed += 1
        return processed

    def reset_stale_telegram_import_events(self):
        with SessionLocal() as db:
            return reset_stale_processing_events(
                db,
                event_types=(TELEGRAM_EXCEL_IMPORT_EVENT_TYPE,),
                action="telegram_excel_import_stale_reset",
                last_error="stale Telegram Excel import reset",
            )

    def process_queued_telegram_imports(self):
        self.reset_stale_telegram_import_events()
        processed = 0
        while True:
            event = self.take_next_telegram_import_event()
            if not event:
                break
            payload = event.get("payload") or {}
            chat_id = normalize_text(payload.get("chat_id"))
            document = payload.get("document") or {}
            result = self.import_telegram_document(
                chat_id,
                document,
                shipment_date=payload.get("shipment_date") or "",
                event_id=str(event.get("id") or ""),
            )
            success, error = result if isinstance(result, tuple) else (False, "telegram_import_failed")
            if success is None:
                processed += 1
                continue
            self.finish_telegram_import_event(event["id"], success, error)
            processed += 1
        return processed

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
        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
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
        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            logging.warning("Telegram worker denied chat_id=%s", chat_id)
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
            command_parts = normalize_text(text).split(maxsplit=1)
            if len(command_parts) > 1 and not parse_dates_from_text(command_parts[1]):
                self.safe_send_message(chat_id, "Неверная дата отчета. Используйте формат ДД.ММ.ГГГГ, например 09.06.2026.")
                return
            self.send_skladbot_daily_report(chat_id, report_date=command_date_or_today(text))
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
        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            logging.warning("Telegram worker denied callback chat_id=%s", chat_id)
            self.answer_callback_query(callback_id, "Нет доступа")
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
        if data.startswith("kiz_date:"):
            self.send_kiz_date_report(chat_id, data.split(":", 1)[1])
            return
        if data.startswith("kiz_file:"):
            self.send_kiz_source_file_by_index(chat_id, data.split(":", 1)[1])
            return
        self.send_main_menu(chat_id, "Кнопка устарела. Выберите действие заново:")

    def load_offset(self):
        try:
            with SessionLocal() as db:
                state = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == "telegram_worker_state")
                ).scalars().first()
                return int((state.payload or {}).get("offset") or 0) if state else 0
        except Exception:
            logging.info("Telegram worker: offset not loaded from database", exc_info=True)
            return 0

    def save_offset(self):
        try:
            with SessionLocal() as db:
                state = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == "telegram_worker_state")
                ).scalars().first()
                if state is None:
                    state = PendingEvent(event_type="telegram_worker_state", status="active", payload={})
                    db.add(state)
                state.payload = {"offset": self.offset}
                db.commit()
        except Exception:
            logging.info("Telegram worker: offset not saved to database", exc_info=True)


def main():
    worker = TelegramWorker()
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
    main()
