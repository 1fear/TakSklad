import hashlib
import json
import logging
import os
import re
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime

from .config import (
    APP_NAME,
    APP_VERSION,
    BACKUP_DIR,
    CREDENTIALS_FILE,
    DAILY_REPORT_AUTO_SEND_HOUR,
    DAILY_REPORT_AUTO_SEND_MINUTE,
    LOG_FILE,
    PENDING_PRINTS_FILE,
    PENDING_SAVES_FILE,
    PENDING_TELEGRAM_FILE,
    REPORTS_DIR,
    TAKSKLAD_DATA_FILE,
    TELEGRAM_FILE_DOWNLOAD_TIMEOUT_SECONDS,
    TELEGRAM_SETTINGS_FILE,
    TELEGRAM_SINGLE_LISTENER_LOCK_ENABLED,
    YANDEX_GEOCODER_KEY_FILE,
)
from .http_client import open_https_url
from .reports import (
    parse_report_date,
    report_date_display,
    report_date_key,
    scan_backup_path_for_date,
    truncate_middle,
)
from .storage import load_data_section, save_data_section
from .utils import (
    clean_file_name,
    is_supported_excel_file_name,
    make_hash,
    normalize_text,
    parse_int_value,
)


def load_telegram_settings():
    defaults = {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
        "chat_ids": [],
        "send_reports": True,
        "send_scan_backups": False,
        "send_pending_files": False,
        "send_error_log": True,
    }
    settings = load_data_section("telegram_settings", {})
    if isinstance(settings, dict):
        defaults.update({key: value for key, value in settings.items() if value is not None})
    return defaults

def get_telegram_chat_ids(settings=None):
    settings = settings or load_telegram_settings()
    chat_ids = []
    raw_chat_ids = settings.get("chat_ids", [])
    if isinstance(raw_chat_ids, list):
        chat_ids.extend(normalize_text(chat_id) for chat_id in raw_chat_ids)
    else:
        chat_ids.extend(
            normalize_text(chat_id)
            for chat_id in str(raw_chat_ids).split(",")
        )

    legacy_chat_id = normalize_text(settings.get("chat_id"))
    if legacy_chat_id:
        chat_ids.append(legacy_chat_id)

    unique_chat_ids = []
    seen = set()
    for chat_id in chat_ids:
        if chat_id and chat_id not in seen:
            unique_chat_ids.append(chat_id)
            seen.add(chat_id)
    return unique_chat_ids

def telegram_is_configured(settings=None):
    settings = settings or load_telegram_settings()
    return bool(settings.get("enabled") and normalize_text(settings.get("bot_token")) and get_telegram_chat_ids(settings))

def safe_telegram_document_path(path):
    if not path:
        return False
    normalized_path = os.path.abspath(path)
    blocked = {
        os.path.abspath(CREDENTIALS_FILE),
        os.path.abspath(TELEGRAM_SETTINGS_FILE),
        os.path.abspath(TAKSKLAD_DATA_FILE),
        os.path.abspath(YANDEX_GEOCODER_KEY_FILE),
    }
    return os.path.exists(normalized_path) and normalized_path not in blocked

TELEGRAM_CALLBACK_TODAY_SCANS = "today_scans"
TELEGRAM_CALLBACK_TODAY_LOG = "today_log"
TELEGRAM_CALLBACK_DOCUMENTS = "documents"
TELEGRAM_CALLBACK_DOCUMENT_PREFIX = "doc:"

def load_telegram_state():
    state = load_data_section("telegram_state", {})
    return state if isinstance(state, dict) else {}

def save_telegram_state(state):
    return save_data_section("telegram_state", state)

def telegram_reports_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "Скачать сканы за сегодня", "callback_data": TELEGRAM_CALLBACK_TODAY_SCANS}],
            [{"text": "Документы по импорту", "callback_data": TELEGRAM_CALLBACK_DOCUMENTS}],
            [{"text": "Скачать сегодняшний лог", "callback_data": TELEGRAM_CALLBACK_TODAY_LOG}],
        ]
    }

def telegram_documents_keyboard(document_summaries):
    keyboard = []
    for document in document_summaries:
        plan = document.get("plan_blocks", 0)
        scanned = document.get("scanned_blocks", 0)
        source_file = truncate_middle(document.get("source_file", "Документ"), 32)
        button_text = f"{source_file} | {scanned}/{plan}"
        keyboard.append([{
            "text": button_text,
            "callback_data": TELEGRAM_CALLBACK_DOCUMENT_PREFIX + document["key"],
        }])
    keyboard.append([{"text": "Назад", "callback_data": "menu"}])
    return {"inline_keyboard": keyboard}

def telegram_api_request(token, method_name, payload=None, timeout=30):
    payload = payload or {}
    encoded_payload = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method_name}",
        data=encoded_payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with open_https_url(request, timeout=timeout) as response:
        result = json.load(response)
    if result.get("ok"):
        return result.get("result")
    raise RuntimeError(normalize_text(result.get("description")) or "Telegram вернул ошибку")

def send_telegram_message_to_chat(chat_id, text, token, reply_markup=None):
    fields = {
        "chat_id": normalize_text(chat_id),
        "text": normalize_text(text)[:4096],
    }
    if reply_markup:
        fields["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    telegram_api_request(token, "sendMessage", fields, timeout=30)
    return True, "Отправлено в Telegram"

def send_telegram_message(text, reply_markup=None):
    settings = load_telegram_settings()
    if not telegram_is_configured(settings):
        return False, "Telegram не настроен"

    token = normalize_text(settings.get("bot_token"))
    errors = []
    sent = 0
    for chat_id in get_telegram_chat_ids(settings):
        try:
            send_telegram_message_to_chat(chat_id, text, token, reply_markup=reply_markup)
            sent += 1
        except Exception as exc:
            logging.exception("Не удалось отправить сообщение в Telegram")
            errors.append(f"{chat_id}: {exc}")

    if errors:
        return False, "; ".join(errors)
    return True, f"Отправлено получателям: {sent}"

def answer_telegram_callback_query(token, callback_query_id, text=""):
    if not callback_query_id:
        return
    fields = {
        "callback_query_id": callback_query_id,
        "text": normalize_text(text)[:200],
    }
    telegram_api_request(token, "answerCallbackQuery", fields, timeout=15)

def fetch_telegram_updates(token, offset=None):
    fields = {
        "timeout": 0,
        "allowed_updates": json.dumps(["message", "callback_query"]),
    }
    if offset:
        fields["offset"] = offset
    result = telegram_api_request(token, "getUpdates", fields, timeout=20)
    return result if isinstance(result, list) else []

def telegram_chat_is_authorized(chat_id, settings=None):
    return normalize_text(chat_id) in set(get_telegram_chat_ids(settings))

def telegram_single_listener_lock_enabled(settings=None):
    if not TELEGRAM_SINGLE_LISTENER_LOCK_ENABLED:
        return False
    settings = settings or load_telegram_settings()
    value = settings.get("single_listener_lock", True) if isinstance(settings, dict) else True
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "off", "no", "нет")
    return bool(value)

def telegram_document_file_name(document):
    return clean_file_name(document.get("file_name"), "telegram_import")

def telegram_document_is_supported_excel(document):
    return is_supported_excel_file_name(telegram_document_file_name(document))

def get_telegram_file_info(token, file_id):
    file_id = normalize_text(file_id)
    if not file_id:
        raise ValueError("Telegram не передал file_id документа")
    result = telegram_api_request(token, "getFile", {"file_id": file_id}, timeout=30)
    if not isinstance(result, dict) or not normalize_text(result.get("file_path")):
        raise RuntimeError("Telegram не вернул путь к файлу")
    return result

def download_telegram_file(token, file_path, destination_path):
    quoted_path = urllib.parse.quote(normalize_text(file_path), safe="/")
    request = urllib.request.Request(
        f"https://api.telegram.org/file/bot{token}/{quoted_path}",
        headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
    )
    with open_https_url(request, timeout=TELEGRAM_FILE_DOWNLOAD_TIMEOUT_SECONDS) as response:
        with open(destination_path, "wb") as output_file:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output_file.write(chunk)

def download_telegram_document_to_temp(token, document):
    file_name = telegram_document_file_name(document)
    if not telegram_document_is_supported_excel(document):
        raise ValueError("Поддерживаются только Excel-файлы .xlsx и .xlsm")

    suffix = os.path.splitext(file_name)[1].lower() or ".xlsx"
    temp_file = tempfile.NamedTemporaryFile(prefix=f"{APP_NAME}_telegram_import_", suffix=suffix, delete=False)
    temp_path = temp_file.name
    temp_file.close()

    try:
        file_info = get_telegram_file_info(token, document.get("file_id"))
        download_telegram_file(token, file_info.get("file_path"), temp_path)
        return temp_path, file_name
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise

def create_today_log_file():
    os.makedirs(REPORTS_DIR, exist_ok=True)
    today_prefix = datetime.now().strftime("%Y-%m-%d")
    output_path = os.path.join(REPORTS_DIR, f"{APP_NAME}_log_{today_prefix}.txt")
    entry_start_re = re.compile(r"^\d{4}-\d{2}-\d{2} ")
    selected_lines = []

    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as log_file:
            current_entry = []
            current_matches_today = False
            for line in log_file:
                if entry_start_re.match(line):
                    if current_entry and current_matches_today:
                        selected_lines.extend(current_entry)
                    current_entry = [line]
                    current_matches_today = line.startswith(today_prefix)
                else:
                    current_entry.append(line)
            if current_entry and current_matches_today:
                selected_lines.extend(current_entry)

    if not selected_lines:
        selected_lines = [f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [INFO] Ошибок за сегодня в логе нет\n"]

    with open(output_path, "w", encoding="utf-8") as output_file:
        output_file.writelines(selected_lines)
    return output_path

def telegram_multipart_body(fields, file_field, file_path):
    boundary = f"----{APP_NAME}Boundary" + hashlib.sha1(str(datetime.now().timestamp()).encode("utf-8")).hexdigest()
    chunks = []
    for key, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")

    filename = os.path.basename(file_path)
    chunks.append(f"--{boundary}\r\n".encode("utf-8"))
    chunks.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode("utf-8")
    )
    chunks.append(b"Content-Type: application/octet-stream\r\n\r\n")
    with open(file_path, "rb") as file_obj:
        chunks.append(file_obj.read())
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return boundary, b"".join(chunks)

def send_telegram_document_to_chat(file_path, chat_id, caption, token):
    fields = {
        "chat_id": normalize_text(chat_id),
        "caption": normalize_text(caption)[:1024],
    }
    boundary, body = telegram_multipart_body(fields, "document", file_path)
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    with open_https_url(request, timeout=30) as response:
        result = json.load(response)
    if result.get("ok"):
        return True, "Отправлено в Telegram"
    return False, normalize_text(result.get("description")) or "Telegram вернул ошибку"

def send_telegram_document(file_path, caption=""):
    settings = load_telegram_settings()
    if not telegram_is_configured(settings):
        return False, "Telegram не настроен"
    if not safe_telegram_document_path(file_path):
        return False, "Файл не найден или запрещён к отправке"

    token = normalize_text(settings.get("bot_token"))
    errors = []
    sent = 0
    for chat_id in get_telegram_chat_ids(settings):
        try:
            ok, message = send_telegram_document_to_chat(file_path, chat_id, caption, token)
            if ok:
                sent += 1
            else:
                errors.append(f"{chat_id}: {message}")
        except Exception as exc:
            logging.exception("Не удалось отправить документ в Telegram")
            errors.append(f"{chat_id}: {exc}")

    if errors:
        return False, "; ".join(errors)
    return True, f"Отправлено получателям: {sent}"

def load_pending_telegram():
    data = load_data_section("pending_telegram", [])
    return data if isinstance(data, list) else []

def save_pending_telegram(items):
    return save_data_section("pending_telegram", items)

def make_pending_telegram_id(file_path, caption):
    payload = {
        "path": os.path.abspath(file_path),
        "caption": normalize_text(caption),
    }
    return make_hash(payload)

def add_pending_telegram(file_path, caption, reason):
    if not safe_telegram_document_path(file_path):
        return None
    pending = load_pending_telegram()
    pending_id = make_pending_telegram_id(file_path, caption)
    for item in pending:
        if item.get("id") == pending_id:
            item["last_error"] = reason
            item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            item["attempts"] = parse_int_value(item.get("attempts")) + 1
            save_pending_telegram(pending)
            return pending_id

    pending.append({
        "id": pending_id,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "path": os.path.abspath(file_path),
        "caption": normalize_text(caption),
        "attempts": 1,
        "last_error": reason,
    })
    save_pending_telegram(pending)
    return pending_id

def send_or_queue_telegram_document(file_path, caption):
    settings = load_telegram_settings()
    if not telegram_is_configured(settings):
        return False, "Telegram не настроен"
    ok, message = send_telegram_document(file_path, caption)
    if not ok:
        add_pending_telegram(file_path, caption, message)
    return ok, message

def sync_pending_telegram():
    if not telegram_is_configured():
        return {"sent": 0, "failed": 0, "remaining": len(load_pending_telegram())}

    pending = load_pending_telegram()
    sent = 0
    failed = 0
    remaining = []
    for item in pending:
        file_path = item.get("path")
        caption = item.get("caption", "")
        if not safe_telegram_document_path(file_path):
            continue
        ok, message = send_telegram_document(file_path, caption)
        if ok:
            sent += 1
            continue
        failed += 1
        item["last_error"] = message
        item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        item["attempts"] = parse_int_value(item.get("attempts")) + 1
        remaining.append(item)
    save_pending_telegram(remaining)
    return {"sent": sent, "failed": failed, "remaining": len(remaining)}

def today_scan_backup_path():
    return scan_backup_path_for_date()

def load_daily_report_state():
    state = load_data_section("daily_report_state", {})
    return state if isinstance(state, dict) else {}

def save_daily_report_state(state):
    return save_data_section("daily_report_state", state)

def daily_report_state_entry(report_date=None):
    return load_daily_report_state().get(report_date_key(report_date), {})

def daily_report_already_handled(report_date=None):
    entry = daily_report_state_entry(report_date)
    return normalize_text(entry.get("status")) in {"sent", "queued", "empty"}

def mark_daily_report_status(report_date, status, filename="", message="", total_rows=0):
    state = load_daily_report_state()
    key = report_date_key(report_date)
    state[key] = {
        "date": key,
        "display_date": report_date_display(report_date),
        "status": normalize_text(status),
        "filename": os.path.abspath(filename) if filename else "",
        "message": normalize_text(message),
        "total_rows": total_rows,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_daily_report_state(state)
    return state[key]

def scan_backup_report_dates():
    dates = []
    if not os.path.isdir(BACKUP_DIR):
        return dates
    pattern = re.compile(r"^scan_backup_(\d{2}\.\d{2}\.\d{4})\.jsonl$")
    for file_name in os.listdir(BACKUP_DIR):
        match = pattern.match(file_name)
        if not match:
            continue
        path = os.path.join(BACKUP_DIR, file_name)
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            continue
        dates.append(parse_report_date(match.group(1)))
    return sorted(set(dates))

def should_send_today_daily_report(now=None):
    now = now or datetime.now()
    send_at = now.replace(
        hour=DAILY_REPORT_AUTO_SEND_HOUR,
        minute=DAILY_REPORT_AUTO_SEND_MINUTE,
        second=0,
        microsecond=0,
    )
    return now >= send_at

def due_daily_report_dates(now=None):
    now = now or datetime.now()
    today = now.date()
    dates = [date for date in scan_backup_report_dates() if date < today]
    if should_send_today_daily_report(now) and os.path.exists(scan_backup_path_for_date(today)):
        dates.append(today)
    return [date for date in sorted(set(dates)) if not daily_report_already_handled(date)]

def daily_report_caption(result, reason=""):
    date_label = result.get("shipment_date_display") or result.get("report_date_display")
    part_number = result.get("part_number")
    part_text = f", часть {part_number}" if part_number else ""
    lines = [
        f"{APP_NAME}: КИЗ-отчёт за {date_label}{part_text}",
        f"Всего КИЗов: {result.get('total_report_rows', 0)}",
        f"Терминал: {result.get('terminal_count', 0)}",
        f"Перечисление: {result.get('transfer_count', 0)}",
        f"Не распознано: {result.get('unknown_count', 0)}",
    ]
    if reason:
        lines.extend(["", reason])
    return "\n".join(lines)

def send_daily_report_result_to_telegram(result, reason=""):
    filename = result.get("filename")
    if not filename:
        return False, "Файл отчёта не создан", "failed"
    ok, message = send_or_queue_telegram_document(filename, daily_report_caption(result, reason=reason))
    if ok:
        status = "sent"
    elif telegram_is_configured():
        status = "queued"
    else:
        status = "failed"
    mark_daily_report_status(
        result.get("report_date"),
        status,
        filename=filename,
        message=message,
        total_rows=result.get("total_report_rows", 0),
    )
    return ok, message, status

def collect_operational_documents(
    include_report=None,
    include_error_log=False,
    include_scan_backup=False,
    include_pending_files=False,
):
    settings = load_telegram_settings()
    documents = []
    if include_report and settings.get("send_reports"):
        documents.append((include_report, f"{APP_NAME}: Excel-отчёт за день"))
    if include_scan_backup and settings.get("send_scan_backups"):
        backup_path = today_scan_backup_path()
        if safe_telegram_document_path(backup_path):
            documents.append((backup_path, f"{APP_NAME}: backup сканирования за день"))
    if include_pending_files and settings.get("send_pending_files"):
        for path, caption in (
            (PENDING_SAVES_FILE, f"{APP_NAME}: очередь записи в Google Sheets"),
            (PENDING_PRINTS_FILE, f"{APP_NAME}: очередь печати сводок"),
            (PENDING_TELEGRAM_FILE, f"{APP_NAME}: очередь отправки в Telegram"),
        ):
            if safe_telegram_document_path(path):
                documents.append((path, caption))
    if include_error_log and settings.get("send_error_log") and safe_telegram_document_path(LOG_FILE):
        documents.append((LOG_FILE, f"{APP_NAME}: журнал ошибок"))
    return documents
