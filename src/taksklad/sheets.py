import logging
import os
import time
from datetime import datetime

import gspread
from gspread.exceptions import WorksheetNotFound
from gspread.http_client import HTTPClient
from oauth2client.service_account import ServiceAccountCredentials

from .config import (
    ARCHIVE_SHEET_NAME,
    CREDENTIALS_FILE,
    GOOGLE_API_TIMEOUT_SECONDS,
    GOOGLE_BACKOFF_LOG_INTERVAL_SECONDS,
    GOOGLE_RETRY_COOLDOWN_SECONDS,
    LEGACY_ORDER_DATE_COLUMN,
    ORDER_DATE_COLUMN,
    REQUIRED_COLUMNS,
    RETURNS_SHEET_NAME,
    SERVICE_COLUMNS,
    SERVICE_COLUMN_START_INDEX,
    SHEET_NAME,
    SKLADBOT_REQUEST_NUMBER_COLUMN,
    SPREADSHEET_ID,
    STATUS_COLUMN,
    STATUS_COMPLETED,
    STATUS_NOT_COMPLETED,
    TELEGRAM_LOCK_KEY,
    TELEGRAM_LOCK_SHEET_NAME,
    TELEGRAM_LOCK_TTL_SECONDS,
    WORKING_COLUMNS,
)
from .orders import (
    get_order_date_header_index,
    get_order_date_value,
    get_order_status,
    is_completed_status,
    is_order_active,
    make_order_duplicate_key,
    row_matches_order,
)
from .storage import load_credentials_data, load_data_section
from .utils import (
    column_index_to_letter,
    get_cell,
    get_header_index,
    get_header_indices,
    normalize_header_name,
    normalize_text,
    parse_date_to_standard,
    parse_int_value,
    split_codes,
)


class GoogleTimeoutHTTPClient(HTTPClient):
    def __init__(self, auth, session=None):
        super().__init__(auth, session=session)
        self.timeout = GOOGLE_API_TIMEOUT_SECONDS


GOOGLE_BACKOFF_UNTIL_TS = 0.0
GOOGLE_BACKOFF_LAST_LOG_TS = 0.0
RETURN_STATUS_COLUMN = "Статус возврата"
RETURN_DATE_COLUMN = "Дата возврата"
RETURN_REFERENCE_COLUMN = "Основание возврата"
RETURNED_BY_COLUMN = "Принял возврат"
RETURN_EXTRA_COLUMNS = [
    RETURN_STATUS_COLUMN,
    RETURN_DATE_COLUMN,
    RETURN_REFERENCE_COLUMN,
    RETURNED_BY_COLUMN,
]
RETURN_STATUS_VALUE = "Возврат"
DEFAULT_BLOCK_PRICE = 240000
BLOCK_PRICE_COLUMN = "Цена за блок"
LINE_TOTAL_COLUMN = "Сумма позиции"
CALCULATED_LINE_TOTAL_COLUMN = "Сумма рассчитанная"


def is_google_transient_error(exc):
    message = normalize_text(exc).lower()
    retryable_markers = (
        "429",
        "quota",
        "rate limit",
        "read timed out",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "service unavailable",
        "internal error",
        "backend error",
        "http error 500",
        "http error 502",
        "http error 503",
        "apierror: [500]",
        "apierror: [502]",
        "apierror: [503]",
    )
    return any(marker in message for marker in retryable_markers)


def note_google_transient_error(exc, now_ts=None):
    global GOOGLE_BACKOFF_UNTIL_TS, GOOGLE_BACKOFF_LAST_LOG_TS
    if not is_google_transient_error(exc):
        return False

    now_ts = time.time() if now_ts is None else float(now_ts)
    GOOGLE_BACKOFF_UNTIL_TS = max(
        GOOGLE_BACKOFF_UNTIL_TS,
        now_ts + GOOGLE_RETRY_COOLDOWN_SECONDS,
    )
    if now_ts - GOOGLE_BACKOFF_LAST_LOG_TS >= GOOGLE_BACKOFF_LOG_INTERVAL_SECONDS:
        logging.warning(
            "Google Sheets временно перегружен, фоновые обращения на паузе %s сек.: %s",
            GOOGLE_RETRY_COOLDOWN_SECONDS,
            normalize_text(exc),
        )
        GOOGLE_BACKOFF_LAST_LOG_TS = now_ts
    return True


def google_backoff_remaining(now_ts=None):
    now_ts = time.time() if now_ts is None else float(now_ts)
    return max(0, int(GOOGLE_BACKOFF_UNTIL_TS - now_ts))


def ensure_google_background_allowed(operation_name, now_ts=None):
    remaining = google_backoff_remaining(now_ts=now_ts)
    if remaining <= 0:
        return
    raise RuntimeError(
        f"Google Sheets временно на паузе после 429/timeout "
        f"({operation_name}, осталось {remaining} сек.)"
    )


def reset_google_backoff_for_tests():
    global GOOGLE_BACKOFF_UNTIL_TS, GOOGLE_BACKOFF_LAST_LOG_TS
    GOOGLE_BACKOFF_UNTIL_TS = 0.0
    GOOGLE_BACKOFF_LAST_LOG_TS = 0.0


def normalize_order_money_fields(record):
    quantity_blocks = parse_int_value(record.get("Кол-во блок"))
    block_price = parse_int_value(record.get(BLOCK_PRICE_COLUMN)) or DEFAULT_BLOCK_PRICE
    calculated_line_total = quantity_blocks * block_price if quantity_blocks > 0 else 0

    if block_price > 0:
        record[BLOCK_PRICE_COLUMN] = block_price
    if calculated_line_total > 0:
        record[CALCULATED_LINE_TOTAL_COLUMN] = calculated_line_total
        record[LINE_TOTAL_COLUMN] = calculated_line_total
    elif not parse_int_value(record.get(LINE_TOTAL_COLUMN)):
        record[LINE_TOTAL_COLUMN] = 0
    return record


def money_field_updates_for_row(record, header_idx, row_number):
    updates = []
    fields = (
        BLOCK_PRICE_COLUMN,
        CALCULATED_LINE_TOTAL_COLUMN,
        LINE_TOTAL_COLUMN,
    )
    for field in fields:
        idx = header_idx.get(field)
        if idx is None:
            continue
        value = record.get(field)
        if value in (None, ""):
            continue
        updates.append({
            "range": f"{column_index_to_letter(idx)}{row_number}",
            "values": [[value]],
        })
    return updates


def get_google_client():
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    credentials = load_credentials_data()
    if isinstance(credentials, dict) and credentials.get("client_email"):
        creds = ServiceAccountCredentials.from_json_keyfile_dict(credentials, scope)
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
    return gspread.authorize(creds, http_client=GoogleTimeoutHTTPClient)


def format_google_sheets_error(exc):
    message = normalize_text(exc)
    lower_message = message.lower()
    if (
        isinstance(exc, PermissionError)
        or "does not have permission" in lower_message
        or "apierror: [403]" in lower_message
        or "http error 403" in lower_message
    ):
        return (
            "Нет доступа к Google-таблице. Проверьте, что таблица открыта для "
            "service account из TakSklad_data.json или credentials.json рядом с приложением."
        )
    if not message:
        return (
            "Google Sheets вернул ошибку без подробностей. Проверьте доступ к таблице "
            "и попробуйте обновить список ещё раз."
        )
    if "invalid jwt signature" in lower_message or "invalid_grant" in lower_message:
        return (
            "Google-ключ повреждён или устарел: Invalid JWT Signature. "
            "Запустите новую папку TakSklad с рабочим TakSklad_data.json или положите "
            "актуальный credentials.json рядом с приложением."
        )
    if "429" in lower_message or "quota" in lower_message or "rate limit" in lower_message:
        return (
            "Google Sheets временно ограничил запросы. Подождите минуту и повторите "
            "обновление; сканирование можно продолжать по уже загруженному списку."
        )
    if (
        "getaddrinfo failed" in lower_message
        or "failed to resolve" in lower_message
        or "name resolution" in lower_message
        or "connection" in lower_message
        or "timed out" in lower_message
        or "timeout" in lower_message
        or "ssl" in lower_message
        or "remote host" in lower_message
    ):
        return (
            "Нет стабильной связи с Google Sheets. Проверьте интернет/VPN и повторите "
            "обновление; уже загруженный список остаётся доступен."
        )
    return message


TELEGRAM_LOCK_HEADER = ["key", "owner_id", "owner_label", "updated_at", "updated_ts"]


def parse_lock_timestamp(value):
    try:
        return float(normalize_text(value).replace(",", "."))
    except ValueError:
        return 0.0


def telegram_lock_is_stale(updated_ts, now_ts=None):
    now_ts = time.time() if now_ts is None else float(now_ts)
    updated_ts = parse_lock_timestamp(updated_ts)
    return not updated_ts or now_ts - updated_ts > TELEGRAM_LOCK_TTL_SECONDS


def get_or_create_telegram_lock_sheet(client):
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    try:
        sheet = spreadsheet.worksheet(TELEGRAM_LOCK_SHEET_NAME)
    except WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(
            title=TELEGRAM_LOCK_SHEET_NAME,
            rows=10,
            cols=len(TELEGRAM_LOCK_HEADER),
        )
    rows = sheet.get_all_values()
    header = rows[0] if rows else []
    if header[:len(TELEGRAM_LOCK_HEADER)] != TELEGRAM_LOCK_HEADER:
        sheet.batch_update(
            [{"range": f"A1:E1", "values": [TELEGRAM_LOCK_HEADER]}],
            value_input_option="USER_ENTERED",
        )
    return sheet


def write_telegram_lock_row(sheet, owner_id, owner_label, now_ts):
    values = [
        TELEGRAM_LOCK_KEY,
        normalize_text(owner_id),
        normalize_text(owner_label),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        str(int(now_ts)),
    ]
    sheet.batch_update(
        [{"range": "A2:E2", "values": [values]}],
        value_input_option="USER_ENTERED",
    )
    return values


def acquire_telegram_poll_lock(owner_id, owner_label, now_ts=None):
    now_ts = time.time() if now_ts is None else float(now_ts)
    owner_id = normalize_text(owner_id)
    owner_label = normalize_text(owner_label)
    ensure_google_background_allowed("Telegram lock", now_ts=now_ts)
    try:
        sheet = get_or_create_telegram_lock_sheet(get_google_client())
        rows = sheet.get_all_values()
    except Exception as exc:
        note_google_transient_error(exc, now_ts=now_ts)
        raise
    current = rows[1] if len(rows) > 1 else []
    current_key = get_cell(current, 0)
    current_owner_id = get_cell(current, 1)
    current_owner_label = get_cell(current, 2)
    current_updated_ts = get_cell(current, 4)

    lock_is_free = (
        current_key != TELEGRAM_LOCK_KEY
        or not current_owner_id
        or current_owner_id == owner_id
        or telegram_lock_is_stale(current_updated_ts, now_ts=now_ts)
    )
    if not lock_is_free:
        return {
            "acquired": False,
            "owner_id": current_owner_id,
            "owner_label": current_owner_label,
            "updated_ts": parse_lock_timestamp(current_updated_ts),
        }

    try:
        write_telegram_lock_row(sheet, owner_id, owner_label, now_ts)
        verify_rows = sheet.get_all_values()
    except Exception as exc:
        note_google_transient_error(exc, now_ts=now_ts)
        raise
    verify = verify_rows[1] if len(verify_rows) > 1 else []
    acquired = get_cell(verify, 1) == owner_id
    return {
        "acquired": acquired,
        "owner_id": get_cell(verify, 1),
        "owner_label": get_cell(verify, 2),
        "updated_ts": parse_lock_timestamp(get_cell(verify, 4)),
    }


def release_telegram_poll_lock(owner_id):
    owner_id = normalize_text(owner_id)
    ensure_google_background_allowed("Telegram lock release")
    try:
        sheet = get_or_create_telegram_lock_sheet(get_google_client())
        rows = sheet.get_all_values()
    except Exception as exc:
        note_google_transient_error(exc)
        raise
    current = rows[1] if len(rows) > 1 else []
    if get_cell(current, 0) != TELEGRAM_LOCK_KEY or get_cell(current, 1) != owner_id:
        return False
    try:
        write_telegram_lock_row(sheet, "", "", time.time())
    except Exception as exc:
        note_google_transient_error(exc)
        raise
    return True


# --- Общий telegram-state (shared last_update_id для двух+ компьютеров) ---
#
# Чтобы два запущенных TakSklad не обработали один и тот же update_id, общий
# счётчик прочитанных Telegram-апдейтов держится в листе _TakSklad_System,
# строка 3 (после header в 1 и lock в 2). Схема та же: key/owner_id/owner_label/
# updated_at/updated_ts. В owner_id пишется last_update_id строкой, чтобы не
# плодить колонки. Локальный telegram_state в TakSklad_data.json остаётся как
# fallback на случай, когда Google Sheets временно недоступен.

TELEGRAM_STATE_KEY = "telegram_state"
TELEGRAM_STATE_ROW = 3


def _read_telegram_state_row(sheet):
    rows = sheet.get_all_values()
    if len(rows) < TELEGRAM_STATE_ROW:
        return []
    return rows[TELEGRAM_STATE_ROW - 1]


def _write_telegram_state_row(sheet, last_update_id, owner_label, now_ts):
    values = [
        TELEGRAM_STATE_KEY,
        str(parse_int_value(last_update_id)),
        normalize_text(owner_label),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        str(int(now_ts)),
    ]
    sheet.batch_update(
        [{"range": f"A{TELEGRAM_STATE_ROW}:E{TELEGRAM_STATE_ROW}", "values": [values]}],
        value_input_option="USER_ENTERED",
    )
    return values


def read_shared_telegram_state():
    """Прочитать общий last_update_id из листа _TakSklad_System.

    Возвращает dict {"last_update_id": int, "updated_ts": float, "owner_label": str}.
    Если строки нет, last_update_id = 0.
    Бросает исключение, если Google Sheets недоступен — вызывающий должен
    откатиться на локальный кэш.
    """
    ensure_google_background_allowed("Telegram state read")
    try:
        sheet = get_or_create_telegram_lock_sheet(get_google_client())
        row = _read_telegram_state_row(sheet)
    except Exception as exc:
        note_google_transient_error(exc)
        raise
    if get_cell(row, 0) != TELEGRAM_STATE_KEY:
        return {"last_update_id": 0, "updated_ts": 0.0, "owner_label": ""}
    return {
        "last_update_id": parse_int_value(get_cell(row, 1)),
        "updated_ts": parse_lock_timestamp(get_cell(row, 4)),
        "owner_label": get_cell(row, 2),
    }


def write_shared_telegram_state(last_update_id, owner_label, now_ts=None):
    """Записать общий last_update_id, только если он строго больше текущего.

    Защита от двух параллельных писателей: если в момент между нашим read и
    write кто-то уже записал большее значение — не откатываем его назад.
    Возвращает True, если запись произошла, иначе False.
    """
    now_ts = time.time() if now_ts is None else float(now_ts)
    new_value = parse_int_value(last_update_id)
    if new_value <= 0:
        return False
    ensure_google_background_allowed("Telegram state write", now_ts=now_ts)
    try:
        sheet = get_or_create_telegram_lock_sheet(get_google_client())
        row = _read_telegram_state_row(sheet)
    except Exception as exc:
        note_google_transient_error(exc, now_ts=now_ts)
        raise
    current_key = get_cell(row, 0)
    current_value = parse_int_value(get_cell(row, 1)) if current_key == TELEGRAM_STATE_KEY else 0
    if new_value <= current_value:
        return False
    try:
        _write_telegram_state_row(sheet, new_value, owner_label, now_ts)
    except Exception as exc:
        note_google_transient_error(exc, now_ts=now_ts)
        raise
    return True


def skladbot_visibility_filter_enabled():
    settings = load_data_section("skladbot_settings", {})
    if not isinstance(settings, dict):
        settings = {}
    token = normalize_text(
        os.environ.get("SKLADBOT_API_TOKEN")
        or settings.get("api_token")
        or settings.get("token")
        or settings.get("bearer_token")
    )
    return bool(settings.get("enabled", True) and token)


def validate_sheet_header(header):
    header_idx = get_header_index(header)
    if ORDER_DATE_COLUMN not in header_idx and LEGACY_ORDER_DATE_COLUMN in header_idx:
        header_idx[ORDER_DATE_COLUMN] = header_idx[LEGACY_ORDER_DATE_COLUMN]
    missing = [col for col in REQUIRED_COLUMNS if col not in header_idx]
    return header_idx, missing


def ensure_sheet_columns(sheet, columns):
    all_rows = sheet.get_all_values()
    if not all_rows:
        header = list(columns)
        sheet.append_row(header, value_input_option="USER_ENTERED")
        return header

    header = [normalize_header_name(col) for col in all_rows[0]]
    header_idx = get_header_index(header)
    for column in columns:
        if column not in header_idx:
            header.append(column)
            sheet.update_cell(1, len(header), column)
            header_idx[column] = len(header) - 1
    return header


def build_import_sheet_header():
    header = [""] * (SERVICE_COLUMN_START_INDEX + len(SERVICE_COLUMNS))
    for idx, column in enumerate(WORKING_COLUMNS):
        header[idx] = column
    for offset, column in enumerate(SERVICE_COLUMNS):
        header[SERVICE_COLUMN_START_INDEX + offset] = column
    return header


def get_import_column_targets():
    targets = []
    targets.extend((idx, column) for idx, column in enumerate(WORKING_COLUMNS))
    targets.extend(
        (SERVICE_COLUMN_START_INDEX + offset, column)
        for offset, column in enumerate(SERVICE_COLUMNS)
    )
    return targets


def ensure_import_sheet_columns(sheet):
    all_rows = sheet.get_all_values()
    required_len = SERVICE_COLUMN_START_INDEX + len(SERVICE_COLUMNS)
    if not all_rows:
        header = build_import_sheet_header()
        sheet.append_row(header, value_input_option="USER_ENTERED")
        return header

    header = [normalize_header_name(col) for col in all_rows[0]]
    if len(header) < required_len:
        header.extend([""] * (required_len - len(header)))

    # A:J are warehouse fields and AA:AI are import/SkladBot metadata.
    for target_idx, column in get_import_column_targets():
        header[target_idx] = column

    last_col = column_index_to_letter(len(header) - 1)
    sheet.batch_update([{
        "range": f"A1:{last_col}1",
        "values": [header],
    }], value_input_option="USER_ENTERED")

    return header


def migrate_legacy_service_columns(sheet):
    all_rows = sheet.get_all_values()
    if len(all_rows) <= 1:
        return

    header = [normalize_header_name(col) for col in all_rows[0]]
    updates = []
    clear_ranges = []

    for offset, column in enumerate(SERVICE_COLUMNS):
        target_idx = SERVICE_COLUMN_START_INDEX + offset
        target_has_data = any(get_cell(row, target_idx) for row in all_rows[1:])
        if target_has_data:
            continue

        for source_idx in get_header_indices(header, column):
            if source_idx == target_idx:
                continue
            source_has_data = any(get_cell(row, source_idx) for row in all_rows[1:])
            if not source_has_data:
                continue

            target_col = column_index_to_letter(target_idx)
            source_col = column_index_to_letter(source_idx)
            updates.append({
                "range": f"{target_col}2:{target_col}{len(all_rows)}",
                "values": [[get_cell(row, source_idx)] for row in all_rows[1:]],
            })
            clear_ranges.append(f"{source_col}2:{source_col}{len(all_rows)}")
            break

    if updates:
        sheet.batch_update(updates, value_input_option="USER_ENTERED")
    if clear_ranges:
        sheet.batch_clear(clear_ranges)


def build_import_record_row(record):
    row = [""] * (SERVICE_COLUMN_START_INDEX + len(SERVICE_COLUMNS))
    for idx, column in enumerate(WORKING_COLUMNS):
        row[idx] = record.get(column, "")
    for offset, column in enumerate(SERVICE_COLUMNS):
        row[SERVICE_COLUMN_START_INDEX + offset] = record.get(column, "")
    return row


def ensure_import_sheet_layout(sheet):
    header = ensure_import_sheet_columns(sheet)
    migrate_legacy_service_columns(sheet)
    return header


def get_existing_import_keys(all_rows):
    if not all_rows:
        return set(), set()

    import_indices = get_header_indices(all_rows[0], "ID импорта")
    order_indices = get_header_indices(all_rows[0], "ID заказа")
    import_ids = set()
    order_ids = set()

    for row in all_rows[1:]:
        for import_idx in import_indices:
            import_id = get_cell(row, import_idx)
            if import_id:
                import_ids.add(import_id)
        for order_idx in order_indices:
            order_id = get_cell(row, order_idx)
            if order_id:
                order_ids.add(order_id)

    return import_ids, order_ids


def get_existing_order_duplicate_keys(all_rows):
    if not all_rows:
        return set()

    header = [normalize_header_name(col) for col in all_rows[0]]
    header_idx = get_header_index(header)
    date_idx = get_order_date_header_index(header_idx)
    duplicate_keys = set()

    for row in all_rows[1:]:
        record = {
            ORDER_DATE_COLUMN: get_cell(row, date_idx),
            "Тип оплаты": get_cell(row, header_idx.get("Тип оплаты")),
            "Клиент": get_cell(row, header_idx.get("Клиент")),
            "Адрес": get_cell(row, header_idx.get("Адрес")),
            "Торговый представитель": get_cell(row, header_idx.get("Торговый представитель")),
            "Товары": get_cell(row, header_idx.get("Товары")),
            "Кол-во ШТ": get_cell(row, header_idx.get("Кол-во ШТ")),
        }
        duplicate_key = make_order_duplicate_key(record)
        if duplicate_key:
            duplicate_keys.add(duplicate_key)

    return duplicate_keys


def get_all_existing_codes_from_rows(all_rows):
    if not all_rows:
        return set()
    header_idx = get_header_index(all_rows[0])
    codes_idx = header_idx.get("Отсканированные коды")
    if codes_idx is None:
        logging.warning("Колонка 'Отсканированные коды' не найдена")
        return set()

    all_codes = set()
    for row in all_rows[1:]:
        for code in split_codes(get_cell(row, codes_idx)):
            all_codes.add(code)
    return all_codes


def get_all_existing_codes(sheet, all_rows=None):
    try:
        if all_rows is None:
            all_rows = sheet.get_all_values()
        return get_all_existing_codes_from_rows(all_rows)
    except Exception:
        logging.exception("Не удалось загрузить существующие коды")
        return set()


def find_code_details_in_rows(all_rows, code):
    if not all_rows:
        return []

    header_idx, missing = validate_sheet_header(all_rows[0])
    if missing:
        raise ValueError("В таблице не найдены обязательные колонки: " + ", ".join(missing))

    codes_idx = header_idx.get("Отсканированные коды")
    details = []
    for row_number, row in enumerate(all_rows[1:], start=2):
        row_codes = split_codes(get_cell(row, codes_idx))
        if code not in row_codes:
            continue

        details.append({
            "row_number": row_number,
            "date": get_cell(row, get_order_date_header_index(header_idx)),
            "payment": get_cell(row, header_idx.get("Тип оплаты")),
            "client": get_cell(row, header_idx.get("Клиент")),
            "address": get_cell(row, header_idx.get("Адрес")),
            "representative": get_cell(row, header_idx.get("Торговый представитель")),
            "product": get_cell(row, header_idx.get("Товары")),
            "quantity": get_cell(row, header_idx.get("Кол-во ШТ")),
            "blocks": get_cell(row, header_idx.get("Кол-во блок")),
            "status": get_cell(row, header_idx.get(STATUS_COLUMN)),
            "codes_count": len(row_codes),
        })
    return details


def find_code_details_in_sheet(sheet, code):
    if not sheet:
        return []
    return find_code_details_in_rows(sheet.get_all_values(), code)


def get_today_orders(apply_skladbot_filter=None, include_rows=False):
    try:
        client = get_google_client()
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
        ensure_import_sheet_layout(sheet)
        all_rows = sheet.get_all_values()
        if not all_rows:
            raise ValueError("Лист Google Sheets пустой")

        header = [normalize_header_name(col) for col in all_rows[0]]
        header_idx, missing = validate_sheet_header(header)
        if missing:
            raise ValueError("В таблице не найдены обязательные колонки: " + ", ".join(missing))

        today_orders = []
        require_skladbot_number = (
            skladbot_visibility_filter_enabled()
            if apply_skladbot_filter is None
            else bool(apply_skladbot_filter)
        )
        status_idx = header_idx.get(STATUS_COLUMN)
        status_updates = []

        for row_number, row in enumerate(all_rows[1:], start=2):
            if not any(normalize_text(cell) for cell in row):
                continue

            record = {}
            for col_name, idx in header_idx.items():
                record[col_name] = get_cell(row, idx)
            original_money = {
                field: record.get(field)
                for field in (BLOCK_PRICE_COLUMN, CALCULATED_LINE_TOTAL_COLUMN, LINE_TOTAL_COLUMN)
            }
            normalize_order_money_fields(record)

            normalized_date = parse_date_to_standard(get_order_date_value(record))
            scanned_codes = split_codes(record.get("Отсканированные коды"))
            current_status = normalize_text(record.get(STATUS_COLUMN))
            calculated_status = get_order_status(record)
            if status_idx is not None and (
                not current_status
                or (calculated_status == STATUS_COMPLETED and not is_completed_status(current_status))
                or (calculated_status == STATUS_NOT_COMPLETED and is_completed_status(current_status))
            ):
                status_updates.append({
                    "range": f"{column_index_to_letter(status_idx)}{row_number}",
                    "values": [[calculated_status]],
                })
                record[STATUS_COLUMN] = calculated_status
            if any(normalize_text(original_money.get(field)) != normalize_text(record.get(field)) for field in original_money):
                status_updates.extend(money_field_updates_for_row(record, header_idx, row_number))

            if is_order_active(record):
                if require_skladbot_number and not normalize_text(record.get(SKLADBOT_REQUEST_NUMBER_COLUMN)):
                    continue
                record["_row_number"] = row_number
                record["_normalized_date"] = normalized_date
                record["_existing_scanned_codes"] = scanned_codes
                today_orders.append(record)

        if status_updates:
            sheet.batch_update(status_updates, value_input_option="USER_ENTERED")

        if include_rows:
            return today_orders, sheet, all_rows
        return today_orders, sheet
    except Exception as exc:
        note_google_transient_error(exc)
        logging.exception("Не удалось загрузить данные из Google Sheets")
        friendly_message = format_google_sheets_error(exc)
        if friendly_message and friendly_message != str(exc):
            raise RuntimeError(friendly_message) from exc
        raise


def update_scanned_codes_to_gsheet(sheet, order, scanned_codes, allow_empty=False):
    try:
        if not scanned_codes and not allow_empty:
            return False, "Нет отсканированных кодов для записи"

        scanned_codes = split_codes("\n".join(scanned_codes))
        if not scanned_codes and not allow_empty:
            return False, "Нет корректных отсканированных кодов для записи"

        if len(scanned_codes) != len(set(scanned_codes)):
            return False, "В текущей позиции есть повторяющиеся коды"

        ensure_import_sheet_layout(sheet)
        all_rows = sheet.get_all_values()
        if not all_rows:
            return False, "Лист Google Sheets пустой"

        header_idx, missing = validate_sheet_header(all_rows[0])
        if missing:
            return False, "В таблице не найдены обязательные колонки: " + ", ".join(missing)

        codes_idx = header_idx["Отсканированные коды"]
        target_row_number = parse_int_value(order.get("_row_number"))

        target_row = None
        if 2 <= target_row_number <= len(all_rows):
            candidate = all_rows[target_row_number - 1]
            if row_matches_order(candidate, header_idx, order):
                target_row = target_row_number

        if target_row is None:
            for row_number, row in enumerate(all_rows[1:], start=2):
                if row_matches_order(row, header_idx, order):
                    target_row = row_number
                    break

        if target_row is None:
            return False, "Не найдена строка заказа для записи кодов"

        existing_codes = split_codes(get_cell(all_rows[target_row - 1], codes_idx))
        if existing_codes:
            existing_set = set(existing_codes)
            scanned_set = set(scanned_codes)
            if not existing_set.issubset(scanned_set) and not (
                allow_empty and scanned_set.issubset(existing_set)
            ):
                return False, "В строке заказа уже есть другие отсканированные коды"

        duplicate_codes = []
        scanned_set = set(scanned_codes)
        for row_number, row in enumerate(all_rows[1:], start=2):
            if row_number == target_row:
                continue
            row_codes = set(split_codes(get_cell(row, codes_idx)))
            duplicates = scanned_set.intersection(row_codes)
            duplicate_codes.extend(sorted(duplicates))

        if duplicate_codes:
            return False, "Коды уже есть в другой строке Google Sheets: " + ", ".join(duplicate_codes[:3])

        status_idx = header_idx.get(STATUS_COLUMN)
        updated_order = dict(order)
        updated_order["Отсканированные коды"] = "\n".join(scanned_codes)
        updates = [{
            "range": f"{column_index_to_letter(codes_idx)}{target_row}",
            "values": [["\n".join(scanned_codes)]],
        }]
        if status_idx is not None:
            updates.append({
                "range": f"{column_index_to_letter(status_idx)}{target_row}",
                "values": [[get_order_status(updated_order)]],
            })
        sheet.batch_update(updates, value_input_option="RAW")
        return True, "Коды записаны в Google Sheets"
    except Exception as exc:
        note_google_transient_error(exc)
        logging.exception("Не удалось записать коды в Google Sheets")
        return False, format_google_sheets_error(exc) or str(exc)


def get_or_create_workbook_sheet_like_data(source_sheet, title):
    spreadsheet = source_sheet.spreadsheet
    try:
        target_sheet = spreadsheet.worksheet(title)
    except WorksheetNotFound:
        target_sheet = spreadsheet.add_worksheet(
            title=title,
            rows=1000,
            cols=SERVICE_COLUMN_START_INDEX + len(SERVICE_COLUMNS),
        )
    ensure_import_sheet_layout(target_sheet)
    return target_sheet


def ensure_return_sheet_layout(sheet):
    ensure_import_sheet_layout(sheet)
    return ensure_sheet_columns(sheet, RETURN_EXTRA_COLUMNS)


def normalize_return_lookup(value):
    return "".join(char.casefold() for char in normalize_text(value) if char.isalnum())


def row_return_lookup_values(row, header_idx):
    values = [
        get_cell(row, header_idx.get(SKLADBOT_REQUEST_NUMBER_COLUMN)),
        get_cell(row, header_idx.get("ID заявки SkladBot")),
        get_cell(row, header_idx.get("ID заказа")),
    ]
    return {normalize_return_lookup(value) for value in values if normalize_text(value)}


def build_return_order(rows_with_numbers, header_idx):
    first_row_number, first_row = rows_with_numbers[0]
    request_number = get_cell(first_row, header_idx.get(SKLADBOT_REQUEST_NUMBER_COLUMN))
    request_id = get_cell(first_row, header_idx.get("ID заявки SkladBot"))
    return_status = get_cell(first_row, header_idx.get(RETURN_STATUS_COLUMN))
    returned_at = get_cell(first_row, header_idx.get(RETURN_DATE_COLUMN))
    return_reference = get_cell(first_row, header_idx.get(RETURN_REFERENCE_COLUMN))
    items = []
    for _, row in rows_with_numbers:
        quantity_blocks = parse_int_value(get_cell(row, header_idx.get("Кол-во блок")))
        line_total = parse_int_value(get_cell(row, header_idx.get("Сумма позиции")))
        if not line_total:
            line_total = quantity_blocks * 240000
        items.append({
            "product": get_cell(row, header_idx.get("Товары")),
            "quantity_blocks": quantity_blocks,
            "quantity_pieces": parse_int_value(get_cell(row, header_idx.get("Кол-во ШТ"))),
            "line_total": line_total,
        })
    return {
        "id": f"google:{request_number or request_id or first_row_number}",
        "source": "google_sheets",
        "order_date": parse_date_to_standard(get_cell(first_row, get_order_date_header_index(header_idx))) or "",
        "payment_type": get_cell(first_row, header_idx.get("Тип оплаты")),
        "client": get_cell(first_row, header_idx.get("Клиент")),
        "address": get_cell(first_row, header_idx.get("Адрес")),
        "representative": get_cell(first_row, header_idx.get("Торговый представитель")),
        "status": "returned" if normalize_return_lookup(return_status) == normalize_return_lookup(RETURN_STATUS_VALUE) else "completed",
        "skladbot_request_number": request_number,
        "skladbot_request_id": request_id,
        "return_status": "returned" if normalize_return_lookup(return_status) == normalize_return_lookup(RETURN_STATUS_VALUE) else "",
        "returned_at": returned_at,
        "return_reference": return_reference,
        "items": items,
        "_row_numbers": [row_number for row_number, _ in rows_with_numbers],
    }


def get_archive_sheet():
    client = get_google_client()
    return client.open_by_key(SPREADSHEET_ID).worksheet(ARCHIVE_SHEET_NAME)


def lookup_return_order_in_gsheet(lookup_value):
    lookup = normalize_return_lookup(lookup_value)
    if not lookup:
        raise ValueError("Введите номер или ID заявки SkladBot")

    sheet = get_archive_sheet()
    ensure_return_sheet_layout(sheet)
    all_rows = sheet.get_all_values()
    if not all_rows:
        raise ValueError("Архив Google Sheets пустой")

    header_idx, missing = validate_sheet_header(all_rows[0])
    if missing:
        raise ValueError("В Архиве не найдены обязательные колонки: " + ", ".join(missing))

    matched_rows = [
        (row_number, row)
        for row_number, row in enumerate(all_rows[1:], start=2)
        if lookup in row_return_lookup_values(row, header_idx)
    ]
    if not matched_rows:
        raise ValueError("Закрытая заявка не найдена в Архиве")

    group_keys = {
        normalize_text(get_cell(row, header_idx.get(SKLADBOT_REQUEST_NUMBER_COLUMN)))
        or normalize_text(get_cell(row, header_idx.get("ID заявки SkladBot")))
        or normalize_text(get_cell(row, header_idx.get("ID заказа")))
        for _, row in matched_rows
    }
    if len(group_keys) > 1:
        raise ValueError("Найдено несколько заявок. Уточните номер/ID SkladBot")
    return build_return_order(matched_rows, header_idx)


def mark_return_order_in_gsheet(order, return_reference="", returned_by="desktop"):
    row_numbers = [parse_int_value(value) for value in order.get("_row_numbers") or [] if parse_int_value(value)]
    if not row_numbers:
        raise ValueError("Нет строк Архива для возврата")

    sheet = get_archive_sheet()
    ensure_return_sheet_layout(sheet)
    all_rows = sheet.get_all_values()
    if not all_rows:
        raise ValueError("Архив Google Sheets пустой")

    header = [normalize_header_name(col) for col in all_rows[0]]
    header_idx = get_header_index(header)
    status_idx = header_idx.get(RETURN_STATUS_COLUMN)
    date_idx = header_idx.get(RETURN_DATE_COLUMN)
    reference_idx = header_idx.get(RETURN_REFERENCE_COLUMN)
    returned_by_idx = header_idx.get(RETURNED_BY_COLUMN)
    if None in (status_idx, date_idx, reference_idx, returned_by_idx):
        raise ValueError("В Архиве не найдены колонки возврата")

    rows_to_return = []
    for row_number in row_numbers:
        if row_number < 2 or row_number > len(all_rows):
            continue
        row = list(all_rows[row_number - 1])
        if normalize_return_lookup(get_cell(row, status_idx)) == normalize_return_lookup(RETURN_STATUS_VALUE):
            raise ValueError("Возврат уже принят")
        rows_to_return.append((row_number, row))

    if not rows_to_return:
        raise ValueError("Строки возврата не найдены в Архиве")

    returned_at = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    reference = normalize_text(return_reference) or normalize_text(order.get("skladbot_request_number")) or normalize_text(order.get("skladbot_request_id"))
    actor = normalize_text(returned_by) or "desktop"
    updates = []
    header_len = len(header)
    rows_for_returns = []
    for row_number, row in rows_to_return:
        if len(row) < header_len:
            row.extend([""] * (header_len - len(row)))
        row[status_idx] = RETURN_STATUS_VALUE
        row[date_idx] = returned_at
        row[reference_idx] = reference
        row[returned_by_idx] = actor
        rows_for_returns.append(row[:header_len])
        updates.extend([
            {"range": f"{column_index_to_letter(status_idx)}{row_number}", "values": [[RETURN_STATUS_VALUE]]},
            {"range": f"{column_index_to_letter(date_idx)}{row_number}", "values": [[returned_at]]},
            {"range": f"{column_index_to_letter(reference_idx)}{row_number}", "values": [[reference]]},
            {"range": f"{column_index_to_letter(returned_by_idx)}{row_number}", "values": [[actor]]},
        ])

    sheet.batch_update(updates, value_input_option="USER_ENTERED")

    returns_sheet = get_or_create_workbook_sheet_like_data(sheet, RETURNS_SHEET_NAME)
    ensure_return_sheet_layout(returns_sheet)
    returns_rows = returns_sheet.get_all_values()
    start_row = max(2, len(returns_rows) + 1)
    end_col = column_index_to_letter(header_len - 1)
    returns_sheet.batch_update([{
        "range": f"A{start_row}:{end_col}{start_row + len(rows_for_returns) - 1}",
        "values": rows_for_returns,
    }], value_input_option="USER_ENTERED")

    updated = dict(order)
    updated["status"] = "returned"
    updated["return_status"] = "returned"
    updated["returned_at"] = returned_at
    updated["return_reference"] = reference
    return updated


def fetch_returned_orders_from_gsheet(limit=50):
    try:
        client = get_google_client()
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet(RETURNS_SHEET_NAME)
    except WorksheetNotFound:
        return []

    ensure_return_sheet_layout(sheet)
    all_rows = sheet.get_all_values()
    if not all_rows:
        return []

    header_idx, missing = validate_sheet_header(all_rows[0])
    if missing:
        raise ValueError("В Возвратах не найдены обязательные колонки: " + ", ".join(missing))

    groups = {}
    for row_number, row in enumerate(all_rows[1:], start=2):
        key = (
            get_cell(row, header_idx.get(RETURN_REFERENCE_COLUMN))
            or get_cell(row, header_idx.get(SKLADBOT_REQUEST_NUMBER_COLUMN))
            or get_cell(row, header_idx.get("ID заявки SkladBot"))
            or get_cell(row, header_idx.get("ID заказа"))
            or str(row_number)
        )
        groups.setdefault(key, []).append((row_number, row))

    orders = [build_return_order(rows, header_idx) for rows in groups.values()]
    orders.sort(key=lambda item: normalize_text(item.get("returned_at")), reverse=True)
    return orders[: max(1, int(limit or 50))]


def archive_order_group_to_gsheet(sheet, orders):
    try:
        if not sheet:
            return False, "Нет подключения к Google Sheets"
        if not orders:
            return False, "Нет строк заказа для архивации"

        ensure_import_sheet_layout(sheet)
        all_rows = sheet.get_all_values()
        if not all_rows:
            return False, "Лист Google Sheets пустой"

        header = [normalize_header_name(col) for col in all_rows[0]]
        header_idx, missing = validate_sheet_header(header)
        if missing:
            return False, "В таблице не найдены обязательные колонки: " + ", ".join(missing)

        target_rows = []
        used_rows = set()
        for order in orders:
            row_number = find_order_row_number(all_rows, header_idx, order)
            if row_number and row_number not in used_rows:
                target_rows.append(row_number)
                used_rows.add(row_number)

        if not target_rows:
            return False, "Не найдены строки заказа для переноса в Архив"

        archive_sheet = get_or_create_workbook_sheet_like_data(sheet, ARCHIVE_SHEET_NAME)
        archive_rows = archive_sheet.get_all_values()
        archive_header_idx = get_header_index(archive_rows[0]) if archive_rows else {}
        archived_import_ids, archived_order_ids = get_existing_import_keys(archive_rows)
        archive_start_row = max(2, len(archive_rows) + 1)
        header_len = max(len(header), SERVICE_COLUMN_START_INDEX + len(SERVICE_COLUMNS))
        status_idx = header_idx.get(STATUS_COLUMN)
        rows_to_archive = []
        for row_number in sorted(target_rows):
            source_row = list(all_rows[row_number - 1])
            source_order_id = get_cell(source_row, header_idx.get("ID заказа"))
            source_import_id = get_cell(source_row, header_idx.get("ID импорта"))
            if (
                (source_order_id and source_order_id in archived_order_ids)
                or (source_import_id and source_import_id in archived_import_ids)
                or archive_row_already_exists(source_row, header_idx, archive_rows, archive_header_idx)
            ):
                continue
            if len(source_row) < header_len:
                source_row.extend([""] * (header_len - len(source_row)))
            if status_idx is not None:
                source_row[status_idx] = STATUS_COMPLETED
            rows_to_archive.append(source_row[:header_len])

        if rows_to_archive:
            end_col = column_index_to_letter(header_len - 1)
            archive_sheet.batch_update([{
                "range": f"A{archive_start_row}:{end_col}{archive_start_row + len(rows_to_archive) - 1}",
                "values": rows_to_archive,
            }], value_input_option="USER_ENTERED")

        for row_number in sorted(target_rows, reverse=True):
            sheet.delete_rows(row_number)

        return True, f"Заказ перенесён в Архив: {len(target_rows)} строк"
    except Exception as exc:
        note_google_transient_error(exc)
        logging.exception("Не удалось перенести заказ в Архив")
        return False, format_google_sheets_error(exc) or str(exc)


def find_order_row_number(all_rows, header_idx, order):
    row_number = parse_int_value(order.get("_row_number"))
    if 2 <= row_number <= len(all_rows):
        candidate = all_rows[row_number - 1]
        if row_matches_order(candidate, header_idx, order):
            return row_number

    for candidate_row_number, row in enumerate(all_rows[1:], start=2):
        if row_matches_order(row, header_idx, order):
            return candidate_row_number
    return 0


def archive_row_already_exists(source_row, source_header_idx, archive_rows, archive_header_idx):
    if not archive_rows:
        return False
    source_record = {
        ORDER_DATE_COLUMN: get_cell(source_row, get_order_date_header_index(source_header_idx)),
        "Тип оплаты": get_cell(source_row, source_header_idx.get("Тип оплаты")),
        "Клиент": get_cell(source_row, source_header_idx.get("Клиент")),
        "Адрес": get_cell(source_row, source_header_idx.get("Адрес")),
        "Товары": get_cell(source_row, source_header_idx.get("Товары")),
        "Кол-во ШТ": get_cell(source_row, source_header_idx.get("Кол-во ШТ")),
    }
    source_key = make_order_duplicate_key(source_record)
    if not source_key:
        return False

    for archive_row in archive_rows[1:]:
        archive_record = {
            ORDER_DATE_COLUMN: get_cell(archive_row, get_order_date_header_index(archive_header_idx)),
            "Тип оплаты": get_cell(archive_row, archive_header_idx.get("Тип оплаты")),
            "Клиент": get_cell(archive_row, archive_header_idx.get("Клиент")),
            "Адрес": get_cell(archive_row, archive_header_idx.get("Адрес")),
            "Товары": get_cell(archive_row, archive_header_idx.get("Товары")),
            "Кол-во ШТ": get_cell(archive_row, archive_header_idx.get("Кол-во ШТ")),
        }
        if make_order_duplicate_key(archive_record) == source_key:
            return True
    return False
