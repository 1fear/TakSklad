import logging
import json
import os
import re
import tempfile
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from sqlalchemy import select

from .db import SessionLocal
from .excel_importer import excel_file_to_import_payload, is_supported_excel_file_name
from .models import PendingEvent


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


TELEGRAM_BUTTON_SHIPMENT_DATE = "Дата отгрузки"
TELEGRAM_BUTTON_LOGISTICS_REPORT = "Отчёт логистики"
TELEGRAM_BUTTON_KIZ_BY_FILES = "Выгрузка КИЗов"
TELEGRAM_BUTTON_STATUS = "Статус"
TELEGRAM_LOGISTICS_DATE_PREFIX = "Логистика "
TELEGRAM_KIZ_FILE_PREFIX = "КИЗ файл "
TELEGRAM_EXCEL_IMPORT_EVENT_TYPE = "telegram_excel_import"
TELEGRAM_EXCEL_IMPORT_ACTIVE_STATUSES = ("pending", "processing")
TELEGRAM_CHAT_STATE_EVENT_PREFIX = "telegram_chat_state:"
DATE_PATTERN = re.compile(r"(?<!\d)(\d{1,2})[._/-](\d{1,2})[._/-](\d{2,4})(?!\d)")


def normalize_text(value):
    return str(value or "").strip()


def parse_chat_ids(value):
    result = set()
    for part in str(value or "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            result.add(part)
    return result


def telegram_bot_commands():
    return [
        {"command": "date", "description": TELEGRAM_BUTTON_SHIPMENT_DATE},
        {"command": "logistics", "description": TELEGRAM_BUTTON_LOGISTICS_REPORT},
        {"command": "kiz_files", "description": TELEGRAM_BUTTON_KIZ_BY_FILES},
        {"command": "status", "description": TELEGRAM_BUTTON_STATUS},
    ]


def telegram_inline_keyboard(button_rows):
    return {"inline_keyboard": button_rows}


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


def backend_http_error_detail(exc):
    response = getattr(exc, "response", None)
    if response is None:
        return normalize_text(exc)
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if isinstance(payload, dict) and normalize_text(payload.get("detail")):
        return normalize_text(payload.get("detail"))
    text = normalize_text(getattr(response, "text", ""))
    return text[:300] or f"HTTP {getattr(response, 'status_code', '')}".strip()


def json_dumps(value):
    return json.dumps(value, ensure_ascii=False)


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

    @property
    def configured(self):
        return bool(self.token)

    def telegram_request(self, method, payload=None, timeout=None):
        with httpx.Client(timeout=timeout or self.timeout) as client:
            try:
                response = client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload or {})
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text[:300] if exc.response is not None else ""
                raise RuntimeError(f"Telegram API request failed: {method}: HTTP {exc.response.status_code} {detail}") from None
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Telegram API request failed: {method}: {exc.__class__.__name__}") from None
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(data)
            return data.get("result")

    def ensure_bot_menu(self):
        if getattr(self, "bot_menu_ready", False):
            return
        try:
            self.telegram_request("setMyCommands", {"commands": telegram_bot_commands()})
            self.telegram_request("setChatMenuButton", {"menu_button": {"type": "commands"}})
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
            response = client.post(f"https://api.telegram.org/bot{self.token}/sendDocument", data=data, files=files)
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(payload)
            return payload.get("result")

    def safe_send_document(self, chat_id, content, filename, caption=""):
        try:
            return self.send_document(chat_id, content, filename, caption=caption)
        except Exception:
            logging.warning("Telegram worker: failed to send document", exc_info=True)
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
            source_file = normalize_text(item.get("source_file")) or f"Файл {index}"
            text = source_file if len(source_file) <= 40 else source_file[:37] + "..."
            rows.append([{
                "text": f"{index}. {text}",
                "callback_data": f"kiz_file:{index}",
            }])
        return telegram_inline_keyboard(rows)

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
        dates = self.backend_get("/api/v1/logistics/dates")
        dates = dates if isinstance(dates, list) else []
        if not dates:
            self.safe_send_message(chat_id, "Нет доступных дат отгрузки для отчёта логистики.")
            return
        if len(dates) == 1:
            self.send_logistics_report(chat_id, dates[0])
            return
        self.safe_send_message(chat_id, "Выберите дату отгрузки для отчёта логистики:", reply_markup=self.logistics_date_keyboard(dates))

    def show_kiz_source_files(self, chat_id):
        files = self.backend_get("/api/v1/reports/kiz/source-files")
        files = files if isinstance(files, list) else []
        if not files:
            self.safe_send_message(chat_id, "Нет полностью завершённых исходных файлов для выгрузки КИЗов.")
            return
        if len(files) == 1:
            self.send_kiz_source_file_report(chat_id, files[0].get("source_file") or "", files[0].get("source_key") or "")
            return

        state = self.get_chat_state(chat_id)
        state["kiz_files"] = [
            {
                "index": index,
                "source_file": item.get("source_file") or "",
                "source_key": item.get("source_key") or "",
            }
            for index, item in enumerate(files, start=1)
        ]
        self.save_chat_state(chat_id, state)
        lines = ["Выберите исходный файл для выгрузки КИЗов:"]
        for index, item in enumerate(files, start=1):
            dates = ", ".join(display_date(value) for value in item.get("dates") or []) or "без даты"
            lines.append(
                f"{index}. {item.get('source_file')} - {dates}, "
                f"{item.get('scanned_blocks', 0)}/{item.get('planned_blocks', 0)} блоков"
            )
        self.safe_send_message(chat_id, "\n".join(lines), reply_markup=self.kiz_files_keyboard(files))

    def send_kiz_source_file_report(self, chat_id, source_file, source_key=""):
        source_file = normalize_text(source_file)
        source_key = normalize_text(source_key)
        if not source_file:
            self.safe_send_message(chat_id, "Не выбран исходный файл для выгрузки КИЗов.")
            return False
        params = {"source_file": source_file}
        if source_key:
            params["source_key"] = source_key
        content, headers = self.backend_get_bytes("/api/v1/reports/kiz/source-file", params=params)
        filename_header = urllib.parse.unquote(headers.get("X-TakSklad-Filename") or "")
        filename = filename_header or f"TakSklad_КИЗ_{source_file}.xlsx"
        self.safe_send_document(
            chat_id,
            content,
            filename,
            caption=f"КИЗы по исходному файлу: {source_file}",
        )
        return True

    def send_status_report(self, chat_id):
        payload = self.backend_get("/api/v1/reports/day")
        totals = payload.get("totals") or {}
        report_date = display_date(payload.get("report_date")) or "сегодня"
        lines = [
            f"Статус TakSklad за {report_date}",
            "",
            f"Заказов: {totals.get('orders', 0)}",
            f"Выполнено заказов: {totals.get('completed_orders', 0)}",
            f"Активных заказов: {totals.get('active_orders', 0)}",
            f"Блоков: {totals.get('scanned_blocks', 0)} / {totals.get('planned_blocks', 0)}",
            f"Осталось блоков: {totals.get('remaining_blocks', 0)}",
            f"КИЗов: {totals.get('scan_codes', 0)}",
            f"Сумма: {format_money(totals.get('total_price', 0))} сум",
        ]
        self.safe_send_message(chat_id, "\n".join(lines))
        return True

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

    def import_telegram_document(self, chat_id, document, shipment_date=""):
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
            )
            meta = import_payload.pop("meta", {})
            result = self.backend_post("/api/v1/imports", import_payload)
            warnings = meta.get("warnings") or []
            lines = [
                "TakSklad: Excel импортирован через Telegram",
                "",
                f"Файл: {file_name}",
                f"Строк в файле: {meta.get('source_rows_count', 0)}",
                f"Строк отправлено в backend: {len(import_payload.get('rows') or [])}",
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
        except Exception as exc:
            logging.exception("Telegram worker: Excel import failed")
            self.safe_send_message(
                chat_id,
                "\n".join([
                    "Не удалось импортировать Excel-файл.",
                    "",
                    f"Файл: {file_name}",
                    f"Причина: {exc}",
                    "",
                    "Подробности записаны в лог Telegram worker.",
                ]),
            )
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
            event = PendingEvent(
                event_type=TELEGRAM_EXCEL_IMPORT_EVENT_TYPE,
                status="pending",
                payload={
                    "chat_id": normalize_text(chat_id),
                    "document": document,
                    "file_name": file_name,
                    "update_id": update_id,
                    "shipment_date": shipment_date,
                    "queued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
            db.add(event)
            db.commit()

        self.safe_send_message(
            chat_id,
            "\n".join([
                "Excel-файл поставлен в очередь импорта.",
                "",
                f"Файл: {file_name}",
                f"Дата отгрузки: {shipment_date or 'не задана'}",
                "Если отправить несколько файлов подряд, они будут обработаны по очереди.",
            ]),
        )
        return True

    def take_next_telegram_import_event(self):
        with SessionLocal() as db:
            event = db.execute(
                select(PendingEvent)
                .where(PendingEvent.event_type == TELEGRAM_EXCEL_IMPORT_EVENT_TYPE)
                .where(PendingEvent.status.in_(TELEGRAM_EXCEL_IMPORT_ACTIVE_STATUSES))
                .order_by(PendingEvent.created_at, PendingEvent.id)
            ).scalars().first()
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
            db.commit()

    def process_queued_telegram_imports(self):
        processed = 0
        while True:
            event = self.take_next_telegram_import_event()
            if not event:
                break
            payload = event.get("payload") or {}
            chat_id = normalize_text(payload.get("chat_id"))
            document = payload.get("document") or {}
            result = self.import_telegram_document(chat_id, document, shipment_date=payload.get("shipment_date") or "")
            success, error = result if isinstance(result, tuple) else (False, "telegram_import_failed")
            self.finish_telegram_import_event(event["id"], success, error)
            processed += 1
        return processed

    def poll_once(self):
        if not self.configured:
            logging.info("Telegram worker disabled: TELEGRAM_BOT_TOKEN is not configured")
            return

        self.ensure_bot_menu()
        poll_timeout = max(1, min(self.poll_timeout, max(1, self.timeout - 5)))
        updates = self.telegram_request("getUpdates", {
            "offset": self.offset + 1 if self.offset else None,
            "timeout": poll_timeout,
            "allowed_updates": ["message", "callback_query"],
        }, timeout=poll_timeout + 5) or []
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

    def notify_update_error(self, update, exc):
        callback_query = update.get("callback_query") or {}
        message = update.get("message") or callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if not chat_id:
            return
        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            return
        reason = normalize_text(exc)
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
        if text_matches(text, "/start", "/help"):
            self.send_message(
                chat_id,
                "\n".join([
                    "TakSklad backend online.",
                    "",
                    "Используйте нижнее меню Telegram:",
                    f"- {TELEGRAM_BUTTON_SHIPMENT_DATE} - задать дату отгрузки для следующих Excel-файлов;",
                    f"- {TELEGRAM_BUTTON_LOGISTICS_REPORT} - выгрузить общий файл для логистики по выбранной дате;",
                    f"- {TELEGRAM_BUTTON_KIZ_BY_FILES} - выгрузить КИЗы по завершённым исходным файлам;",
                    f"- {TELEGRAM_BUTTON_STATUS} - показать общий статус по заказам и КИЗам;",
                    "",
                    "Excel-файлы можно просто отправлять или пересылать в этот чат. Если отправить несколько файлов подряд, они попадут в очередь и обработаются по порядку.",
                    "Дату можно отправить отдельным сообщением в формате 29.05.2026 или указать в подписи к Excel-файлу.",
                ]),
            )
            return
        if text_matches(text, TELEGRAM_BUTTON_SHIPMENT_DATE, "/date"):
            current_date = self.get_chat_shipment_date(chat_id)
            self.send_message(
                chat_id,
                "\n".join([
                    "Отправьте дату отгрузки сообщением в формате 29.05.2026.",
                    f"Текущая дата: {current_date or 'не задана'}",
                ]),
            )
            return
        if text.startswith("/date ") or parse_date_from_text(text) == text:
            shipment_date = parse_date_from_text(text)
            if shipment_date:
                self.set_chat_shipment_date(chat_id, shipment_date)
                self.send_message(chat_id, f"Дата отгрузки для следующих Excel-файлов: {shipment_date}")
                return
        if text_matches(text, "/logistics", TELEGRAM_BUTTON_LOGISTICS_REPORT):
            self.show_logistics_dates(chat_id)
            return
        if text.startswith(TELEGRAM_LOGISTICS_DATE_PREFIX):
            self.send_logistics_report(chat_id, text.replace(TELEGRAM_LOGISTICS_DATE_PREFIX, "", 1).strip())
            return
        if text_matches(text, "/kiz_files", TELEGRAM_BUTTON_KIZ_BY_FILES):
            self.show_kiz_source_files(chat_id)
            return
        if text.startswith(TELEGRAM_KIZ_FILE_PREFIX):
            self.send_kiz_source_file_by_index(chat_id, text)
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
            payload = self.backend_get("/api/v1/imports")
            imports = payload if isinstance(payload, list) else []
            if not imports:
                self.send_message(chat_id, "История импортов пока пустая.")
                return
            lines = ["Последние импорты TakSklad:"]
            for index, item in enumerate(imports[:10], start=1):
                raw_payload = item.get("raw_payload") or {}
                filename = normalize_text(raw_payload.get("filename")) or "без файла"
                lines.append(
                    f"{index}. {filename}: {item.get('status')} "
                    f"{item.get('rows_imported', 0)}/{item.get('rows_total', 0)}"
                )
            self.send_message(chat_id, "\n".join(lines))
            return
        if text_matches(text, "/logs"):
            if not self.ensure_admin_chat(chat_id):
                return
            self.send_backend_diagnostics_log(chat_id)
            return

        document = message.get("document") or {}
        if document:
            caption_date = parse_date_from_text(message.get("caption"))
            shipment_date = caption_date or self.get_chat_shipment_date(chat_id)
            self.enqueue_telegram_document(chat_id, document, update_id=update.get("update_id"), shipment_date=shipment_date)
            return

        self.send_message(chat_id, "Команда не распознана. Используйте нижнее меню Telegram или отправьте Excel-файл.")

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
        if data.startswith("logistics:"):
            self.send_logistics_report(chat_id, data.split(":", 1)[1])
            return
        if data.startswith("kiz_file:"):
            self.send_kiz_source_file_by_index(chat_id, data.split(":", 1)[1])
            return
        self.safe_send_message(chat_id, "Кнопка устарела. Откройте меню Telegram и повторите действие.")

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
