import base64
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime

from gspread.http_client import HTTPClient


SPREADSHEET_ID = os.environ.get("TAKSKLAD_GOOGLE_SPREADSHEET_ID", "1hisRZ667qEhsRTfoPzv4r78naYhc9kdzhkmUKvZEUr8")
SHEET_NAME = os.environ.get("TAKSKLAD_GOOGLE_SHEET_NAME", "data")
GOOGLE_CREDENTIALS_JSON_ENV = "TAKSKLAD_GOOGLE_CREDENTIALS_JSON"
GOOGLE_CREDENTIALS_JSON_BASE64_ENV = "TAKSKLAD_GOOGLE_CREDENTIALS_JSON_BASE64"
GOOGLE_CREDENTIALS_FILE_ENV = "TAKSKLAD_GOOGLE_CREDENTIALS_FILE"


def env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


GOOGLE_API_TIMEOUT_SECONDS = max(1, env_int("TAKSKLAD_GOOGLE_API_TIMEOUT_SECONDS", 8))

ORDER_DATE_COLUMN = "Дата отгрузки"
LEGACY_ORDER_DATE_COLUMN = "Дата получения заказа"
STATUS_COLUMN = "Статус"
STATUS_NOT_COMPLETED = "Не выполнено"
STATUS_COMPLETED = "Выполнено"
STATUS_ARCHIVED_NO_KIZ = "Архив без КИЗов"
STATUS_CANCELLED = "Отменено"
ARCHIVE_SHEET_NAME = "Архив"
ARCHIVE_NO_KIZ_SHEET_NAME = "Архив без КИЗов"
CANCELLED_SHEET_NAME = "Отмененные"
RETURNS_SHEET_NAME = "Возвраты"
RETURN_STATUS_COLUMN = "Статус возврата"
RETURN_DATE_COLUMN = "Дата возврата"
RETURN_REFERENCE_COLUMN = "Основание возврата"
RETURNED_BY_COLUMN = "Принял возврат"
RETURN_STATUS_VALUE = "Возврат"

WORKING_COLUMNS = [
    ORDER_DATE_COLUMN,
    "Тип оплаты",
    "Клиент",
    "Адрес",
    "Торговый представитель",
    "Товары",
    "Кол-во ШТ",
    "Кол-во блок",
    "Отсканированные коды",
    STATUS_COLUMN,
]
SERVICE_COLUMNS = [
    "ID заказа",
    "ID импорта",
    "Источник файла",
    "Строка файла",
    "Дата импорта",
    "Номер заявки SkladBot",
    "ID заявки SkladBot",
    "Статус SkladBot",
    "Последняя проверка SkladBot",
]
SERVICE_COLUMN_START_INDEX = 26
RETURN_COLUMNS = [
    RETURN_STATUS_COLUMN,
    RETURN_DATE_COLUMN,
    RETURN_REFERENCE_COLUMN,
    RETURNED_BY_COLUMN,
]


@dataclass(frozen=True)
class GoogleSheetsExportResult:
    status: str
    imported: int = 0
    duplicates: int = 0
    updated: int = 0
    error: str = ""

    def as_dict(self):
        return {
            "status": self.status,
            "imported": self.imported,
            "duplicates": self.duplicates,
            "updated": self.updated,
            "error": self.error,
        }


class GoogleSheetsExportDisabled(Exception):
    pass


class GoogleTimeoutHTTPClient(HTTPClient):
    def __init__(self, auth, session=None):
        super().__init__(auth, session=session)
        self.timeout = GOOGLE_API_TIMEOUT_SECONDS


def append_import_records_to_google_sheets(records):
    if not records:
        return GoogleSheetsExportResult(status="skipped").as_dict()

    try:
        client = get_google_client()
    except GoogleSheetsExportDisabled as exc:
        return GoogleSheetsExportResult(status="disabled", error=str(exc)).as_dict()

    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    sheet = spreadsheet.worksheet(SHEET_NAME)
    ensure_import_sheet_layout(sheet)
    all_rows = sheet.get_all_values()
    updated = update_missing_sheet_addresses(sheet, all_rows, records)
    existing_import_ids, existing_order_ids = get_existing_import_keys(all_rows)
    existing_duplicate_keys = get_existing_order_duplicate_keys(all_rows)

    rows_to_append = []
    duplicates = 0
    for record in records:
        duplicate_key = make_order_duplicate_key(record)
        import_id = normalize_text(record.get("ID импорта"))
        order_id = normalize_text(record.get("ID заказа"))
        if (
            (import_id and import_id in existing_import_ids)
            or (order_id and order_id in existing_order_ids)
            or (duplicate_key and duplicate_key in existing_duplicate_keys)
        ):
            duplicates += 1
            continue

        rows_to_append.append(build_import_record_row(record))
        if import_id:
            existing_import_ids.add(import_id)
        if order_id:
            existing_order_ids.add(order_id)
        if duplicate_key:
            existing_duplicate_keys.add(duplicate_key)

    if rows_to_append:
        start_row = len(all_rows) + 1
        end_row = start_row + len(rows_to_append) - 1
        end_col = column_index_to_letter(len(rows_to_append[0]) - 1)
        sheet.batch_update(
            [{
                "range": f"A{start_row}:{end_col}{end_row}",
                "values": rows_to_append,
            }],
            value_input_option="USER_ENTERED",
        )

    return GoogleSheetsExportResult(
        status="completed",
        imported=len(rows_to_append),
        duplicates=duplicates,
        updated=updated,
    ).as_dict()


def restore_import_records_to_google_sheets(records):
    if not records:
        return GoogleSheetsExportResult(status="skipped").as_dict()

    try:
        client = get_google_client()
    except GoogleSheetsExportDisabled as exc:
        return GoogleSheetsExportResult(status="disabled", error=str(exc)).as_dict()

    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    sheet = spreadsheet.worksheet(SHEET_NAME)
    ensure_import_sheet_layout(sheet)
    all_rows = sheet.get_all_values()
    header = all_rows[0] if all_rows else build_import_sheet_header()

    updates = []
    rows_to_append = []
    updated = 0
    header_len = max(SERVICE_COLUMN_START_INDEX + len(SERVICE_COLUMNS), len(header))
    for record in records:
        row_values = build_import_record_row(record)
        row_number = find_record_row_number(all_rows, record)
        if row_number:
            end_col = column_index_to_letter(max(header_len, len(row_values)) - 1)
            updates.append({
                "range": f"A{row_number}:{end_col}{row_number}",
                "values": [row_values],
            })
            updated += 1
        else:
            rows_to_append.append(row_values)

    if updates:
        sheet.batch_update(updates, value_input_option="USER_ENTERED")
    if rows_to_append:
        start_row = len(all_rows) + 1
        end_row = start_row + len(rows_to_append) - 1
        end_col = column_index_to_letter(len(rows_to_append[0]) - 1)
        sheet.batch_update(
            [{
                "range": f"A{start_row}:{end_col}{end_row}",
                "values": rows_to_append,
            }],
            value_input_option="USER_ENTERED",
        )

    return GoogleSheetsExportResult(
        status="completed",
        imported=len(rows_to_append),
        updated=updated,
    ).as_dict()


def sync_backend_order_item_to_google_sheets(item):
    try:
        spreadsheet = get_google_client().open_by_key(SPREADSHEET_ID)
    except GoogleSheetsExportDisabled as exc:
        return GoogleSheetsExportResult(status="disabled", error=str(exc)).as_dict()

    sheet = spreadsheet.worksheet(SHEET_NAME)
    return update_backend_order_item_row(sheet, item)


def sync_backend_order_items_to_google_sheets(items):
    items = list(items or [])
    if not items:
        return GoogleSheetsExportResult(status="skipped").as_dict()

    try:
        spreadsheet = get_google_client().open_by_key(SPREADSHEET_ID)
    except GoogleSheetsExportDisabled as exc:
        return GoogleSheetsExportResult(status="disabled", error=str(exc)).as_dict()

    sheet = spreadsheet.worksheet(SHEET_NAME)
    return update_backend_order_item_rows(sheet, items)


def sync_backend_orders_skladbot_to_google_sheets(orders, include_archive=False):
    orders = list(orders or [])
    if not orders:
        return GoogleSheetsExportResult(status="skipped").as_dict()

    try:
        spreadsheet = get_google_client().open_by_key(SPREADSHEET_ID)
    except GoogleSheetsExportDisabled as exc:
        return GoogleSheetsExportResult(status="disabled", error=str(exc)).as_dict()

    sheet = spreadsheet.worksheet(SHEET_NAME)
    data_result = update_backend_orders_skladbot_rows(sheet, orders)
    if not include_archive:
        return data_result

    archive_sheet = get_or_create_sheet(spreadsheet, ARCHIVE_SHEET_NAME)
    archive_result = update_backend_orders_skladbot_rows(archive_sheet, orders)
    status = "completed"
    if data_result.get("status") in {"disabled", "error"}:
        status = data_result.get("status")
    elif archive_result.get("status") in {"disabled", "error"}:
        status = archive_result.get("status")
    return {
        "status": status,
        "updated": int(data_result.get("updated") or 0) + int(archive_result.get("updated") or 0),
        "data": data_result,
        "archive": archive_result,
    }


def archive_backend_order_to_google_sheets(order):
    return move_backend_order_to_google_sheet(order, ARCHIVE_SHEET_NAME)


def archive_backend_orders_to_google_sheets(orders):
    orders = list(orders or [])
    if not orders:
        return {"status": "skipped", "updated": 0, "orders": {}}

    try:
        spreadsheet = get_google_client().open_by_key(SPREADSHEET_ID)
    except GoogleSheetsExportDisabled as exc:
        return GoogleSheetsExportResult(status="disabled", error=str(exc)).as_dict()

    data_sheet = spreadsheet.worksheet(SHEET_NAME)
    archive_sheet = get_or_create_sheet(spreadsheet, ARCHIVE_SHEET_NAME)
    return archive_backend_orders_rows(data_sheet, archive_sheet, orders)


def archive_backend_order_without_kiz_to_google_sheets(order):
    return move_backend_order_to_google_sheet(
        order,
        ARCHIVE_NO_KIZ_SHEET_NAME,
        sheet_status=STATUS_ARCHIVED_NO_KIZ,
    )


def cancel_backend_order_in_google_sheets(order):
    return move_backend_order_to_google_sheet(
        order,
        CANCELLED_SHEET_NAME,
        sheet_status=STATUS_CANCELLED,
    )


def move_backend_order_to_google_sheet(order, target_sheet_name, sheet_status=None):
    try:
        spreadsheet = get_google_client().open_by_key(SPREADSHEET_ID)
    except GoogleSheetsExportDisabled as exc:
        return GoogleSheetsExportResult(status="disabled", error=str(exc)).as_dict()

    data_sheet = spreadsheet.worksheet(SHEET_NAME)
    target_sheet = get_or_create_sheet(spreadsheet, target_sheet_name)
    return archive_backend_order_rows(data_sheet, target_sheet, order, sheet_status=sheet_status)


def mark_backend_order_returned_in_google_sheets(order):
    try:
        spreadsheet = get_google_client().open_by_key(SPREADSHEET_ID)
    except GoogleSheetsExportDisabled as exc:
        return GoogleSheetsExportResult(status="disabled", error=str(exc)).as_dict()

    archive_sheet = get_or_create_sheet(spreadsheet, ARCHIVE_SHEET_NAME)
    returns_sheet = get_or_create_sheet(spreadsheet, RETURNS_SHEET_NAME)
    return mark_backend_return_rows(archive_sheet, returns_sheet, order)


def update_backend_order_item_row(sheet, item):
    return update_backend_order_item_rows(sheet, [item])


def update_backend_order_item_rows(sheet, items):
    items = list(items or [])
    if not items:
        return GoogleSheetsExportResult(status="skipped").as_dict()

    ensure_import_sheet_layout(sheet)
    all_rows = sheet.get_all_values()
    header = all_rows[0] if all_rows else []
    header_idx = get_header_index(header)

    updates = []
    codes_idx = header_idx.get("Отсканированные коды")
    status_idx = header_idx.get(STATUS_COLUMN)
    updated_rows = set()
    missing = 0
    for item in items:
        row_number = find_backend_item_row_number(all_rows, item)
        if not row_number:
            missing += 1
            continue
        updated_rows.add(row_number)
        if codes_idx is not None:
            updates.append({
                "range": f"{column_index_to_letter(codes_idx)}{row_number}",
                "values": [["\n".join(backend_item_codes(item))]],
            })
        if status_idx is not None:
            updates.append({
                "range": f"{column_index_to_letter(status_idx)}{row_number}",
                "values": [[backend_item_sheet_status(item)]],
            })

    if updates:
        sheet.batch_update(updates, value_input_option="USER_ENTERED")
    if not updated_rows and missing:
        return GoogleSheetsExportResult(status="missing", error="order item rows not found").as_dict()
    return GoogleSheetsExportResult(status="completed", updated=len(updated_rows)).as_dict()


def update_backend_orders_skladbot_rows(sheet, orders):
    ensure_import_sheet_layout(sheet)
    all_rows = sheet.get_all_values()
    if not all_rows:
        return GoogleSheetsExportResult(status="missing").as_dict()

    header = all_rows[0]
    header_idx = get_header_index(header)
    column_names = [
        "Номер заявки SkladBot",
        "ID заявки SkladBot",
        "Статус SkladBot",
        "Последняя проверка SkladBot",
    ]
    if any(header_idx.get(column) is None for column in column_names):
        return GoogleSheetsExportResult(status="missing").as_dict()

    rows_by_business_key, ambiguous_business_keys = build_backend_item_business_row_index(all_rows)
    updates = []
    updated_rows = set()
    for order in orders:
        raw_payload = getattr(order, "raw_payload", None) or {}
        values = {
            "Номер заявки SkladBot": normalize_text(raw_payload.get("skladbot_request_number")),
            "ID заявки SkladBot": normalize_text(raw_payload.get("skladbot_request_id")),
            "Статус SkladBot": format_skladbot_status(raw_payload.get("skladbot_status")),
            "Последняя проверка SkladBot": normalize_text(raw_payload.get("skladbot_checked_at")),
        }
        for item in getattr(order, "items", []) or []:
            row_number = find_backend_item_row_number(all_rows, item)
            if not row_number:
                business_key = make_backend_item_sheet_business_key(order, item)
                if business_key and business_key not in ambiguous_business_keys:
                    row_number = rows_by_business_key.get(business_key)
            if not row_number:
                continue
            updated_rows.add(row_number)
            row = all_rows[row_number - 1] if row_number - 1 < len(all_rows) else []
            for column, value in values.items():
                idx = header_idx[column]
                if (
                    column in {"Номер заявки SkladBot", "ID заявки SkladBot"}
                    and not value
                    and normalize_text(get_cell(row, idx))
                ):
                    continue
                updates.append({
                    "range": f"{column_index_to_letter(idx)}{row_number}",
                    "values": [[value]],
                })

    if updates:
        sheet.batch_update(updates, value_input_option="USER_ENTERED")
    return GoogleSheetsExportResult(status="completed", updated=len(updated_rows)).as_dict()


def build_backend_item_business_row_index(all_rows):
    if not all_rows:
        return {}, set()
    header_idx = get_header_index(all_rows[0])
    rows_by_key = {}
    ambiguous = set()
    for row_number, row in enumerate(all_rows[1:], start=2):
        record = {
            ORDER_DATE_COLUMN: get_cell(row, header_idx.get(ORDER_DATE_COLUMN, header_idx.get(LEGACY_ORDER_DATE_COLUMN))),
            "Тип оплаты": get_cell(row, header_idx.get("Тип оплаты")),
            "Клиент": get_cell(row, header_idx.get("Клиент")),
            "Адрес": get_cell(row, header_idx.get("Адрес")),
            "Торговый представитель": get_cell(row, header_idx.get("Торговый представитель")),
            "Товары": get_cell(row, header_idx.get("Товары")),
            "Кол-во ШТ": get_cell(row, header_idx.get("Кол-во ШТ")),
            "Кол-во блок": get_cell(row, header_idx.get("Кол-во блок")),
        }
        key = make_backend_item_business_key(record)
        if not key:
            continue
        if key in rows_by_key:
            ambiguous.add(key)
        else:
            rows_by_key[key] = row_number
    return rows_by_key, ambiguous


def make_backend_item_sheet_business_key(order, item):
    record = {
        ORDER_DATE_COLUMN: getattr(order, "order_date", ""),
        "Тип оплаты": getattr(order, "payment_type", ""),
        "Клиент": getattr(order, "client", ""),
        "Адрес": getattr(order, "address", ""),
        "Торговый представитель": getattr(order, "representative", ""),
        "Товары": getattr(item, "product", ""),
        "Кол-во ШТ": getattr(item, "quantity_pieces", 0),
        "Кол-во блок": getattr(item, "quantity_blocks", 0),
    }
    return make_backend_item_business_key(record)


def make_backend_item_business_key(record):
    payload = {
        "date": parse_date_to_standard(record.get(ORDER_DATE_COLUMN) or record.get(LEGACY_ORDER_DATE_COLUMN)),
        "payment": normalize_lookup_text(record.get("Тип оплаты")),
        "client": normalize_lookup_text(record.get("Клиент")),
        "address": normalize_lookup_text(record.get("Адрес")),
        "representative": normalize_lookup_text(record.get("Торговый представитель")),
        "product": normalize_lookup_text(record.get("Товары")),
        "quantity_pieces": parse_int_value(record.get("Кол-во ШТ")),
        "quantity_blocks": parse_int_value(record.get("Кол-во блок")),
    }
    if (
        not payload["date"]
        or not payload["payment"]
        or not payload["client"]
        or not payload["product"]
        or payload["quantity_pieces"] <= 0
        or payload["quantity_blocks"] <= 0
    ):
        return ""
    return make_hash(payload)


def format_skladbot_status(value):
    text = normalize_text(value).casefold()
    if text == "found":
        return "Найдено"
    if text == "not_found":
        return "Не найдено"
    if text == "multiple":
        return "Несколько совпадений"
    if text in {"pending", "checking", "in_progress"}:
        return "Проверяется"
    if text == "error":
        return "Ошибка синхронизации"
    return normalize_text(value)


def archive_backend_order_rows(data_sheet, archive_sheet, order, sheet_status=None):
    return archive_backend_orders_rows(data_sheet, archive_sheet, [order], sheet_status=sheet_status)


def archive_backend_orders_rows(data_sheet, archive_sheet, orders, sheet_status=None):
    orders = list(orders or [])
    if not orders:
        return {"status": "skipped", "updated": 0, "orders": {}}

    ensure_import_sheet_layout(data_sheet)
    ensure_import_sheet_layout(archive_sheet)
    data_rows = data_sheet.get_all_values()
    archive_rows = archive_sheet.get_all_values()
    if not data_rows:
        return {
            "status": "missing",
            "updated": 0,
            "orders": {
                str(getattr(order, "id", "")): {"status": "missing", "updated": 0, "error": "data rows not found"}
                for order in orders
            },
        }

    header = data_rows[0]
    header_idx = get_header_index(header)
    archive_header = archive_rows[0] if archive_rows else header
    archive_header_idx = get_header_index(archive_header)
    archived_import_ids, archived_order_ids = get_existing_import_keys(archive_rows)
    header_len = max(len(header), SERVICE_COLUMN_START_INDEX + len(SERVICE_COLUMNS))

    results_by_order_id = {}
    rows_to_archive = []
    rows_to_delete = set()
    archive_rows_for_duplicates = list(archive_rows)
    for order in orders:
        order_id = str(getattr(order, "id", "") or "")
        target_rows = []
        already_archived_count = 0
        used_rows = set()
        for item in getattr(order, "items", []) or []:
            row_number = find_backend_item_row_number(data_rows, item)
            if row_number and row_number not in used_rows:
                target_rows.append((row_number, item, None))
                used_rows.add(row_number)
                continue
            if find_backend_item_row_number(archive_rows_for_duplicates, item):
                already_archived_count += 1
                continue
            target_rows.append((None, item, build_backend_item_archive_row(order, item, header_len, sheet_status)))

        if not target_rows:
            if already_archived_count:
                results_by_order_id[order_id] = {
                    "status": "skipped",
                    "updated": 0,
                    "archived": 0,
                    "error": "order rows already archived",
                }
            else:
                results_by_order_id[order_id] = {"status": "missing", "updated": 0, "error": "order rows not found"}
            continue

        appended = 0
        for row_number, item, generated_row in sorted(target_rows, key=lambda value: value[0] or 10**9):
            if generated_row is None:
                source_row = list(data_rows[row_number - 1])
                if len(source_row) < header_len:
                    source_row.extend([""] * (header_len - len(source_row)))
            else:
                source_row = list(generated_row)

            source_order_id = get_backend_item_source_order_id(item)
            source_import_id = get_backend_item_source_import_id(item)
            already_archived = (
                (source_order_id and source_order_id in archived_order_ids)
                or (source_import_id and source_import_id in archived_import_ids)
                or archive_row_already_exists(source_row, header_idx, archive_rows_for_duplicates, archive_header_idx)
            )
            if not already_archived:
                apply_backend_item_state_to_row(source_row, header_idx, item, completed=True, sheet_status=sheet_status)
                archived_row = source_row[:header_len]
                rows_to_archive.append(archived_row)
                archive_rows_for_duplicates.append(archived_row)
                if source_order_id:
                    archived_order_ids.add(source_order_id)
                if source_import_id:
                    archived_import_ids.add(source_import_id)
                appended += 1

            if row_number:
                rows_to_delete.add(row_number)

        results_by_order_id[order_id] = {
            "status": "completed",
            "updated": len(target_rows),
            "archived": appended,
        }

    if rows_to_archive:
        start_row = max(2, len(archive_rows) + 1)
        end_col = column_index_to_letter(header_len - 1)
        archive_sheet.batch_update([{
            "range": f"A{start_row}:{end_col}{start_row + len(rows_to_archive) - 1}",
            "values": rows_to_archive,
        }], value_input_option="USER_ENTERED")

    for row_number in sorted(rows_to_delete, reverse=True):
        data_sheet.delete_rows(row_number)

    updated = sum(int(value.get("updated") or 0) for value in results_by_order_id.values())
    statuses = {str(value.get("status") or "").strip().lower() for value in results_by_order_id.values()}
    if updated:
        status = "completed"
    elif statuses and statuses <= {"skipped"}:
        status = "skipped"
    else:
        status = "missing"
    return {
        "status": status,
        "updated": updated,
        "archived": len(rows_to_archive),
        "orders": results_by_order_id,
    }


def build_backend_item_archive_row(order, item, header_len, sheet_status=None):
    item_payload = getattr(item, "raw_payload", None) or {}
    order_payload = getattr(order, "raw_payload", None) or {}
    record = {
        ORDER_DATE_COLUMN: format_sheet_date(getattr(order, "order_date", None)),
        "Тип оплаты": normalize_text(getattr(order, "payment_type", "")),
        "Клиент": normalize_text(getattr(order, "client", "")),
        "Адрес": normalize_text(getattr(order, "address", "")) or "Адрес не указан",
        "Торговый представитель": normalize_text(getattr(order, "representative", "")),
        "Товары": normalize_text(getattr(item, "product", "")),
        "Кол-во ШТ": parse_int_value(getattr(item, "quantity_pieces", 0)),
        "Кол-во блок": parse_int_value(getattr(item, "quantity_blocks", 0)),
        "Отсканированные коды": "\n".join(backend_item_codes(item)),
        STATUS_COLUMN: normalize_text(sheet_status) or STATUS_COMPLETED,
        "ID заказа": get_backend_item_source_order_id(item) or normalize_text(getattr(order, "external_id", "")) or str(getattr(order, "id", "")),
        "ID импорта": get_backend_item_source_import_id(item) or str(getattr(item, "id", "")),
        "Источник файла": normalize_text(item_payload.get("source_file") or order_payload.get("source_file")),
        "Строка файла": normalize_text(item_payload.get("source_row") or order_payload.get("source_row")),
        "Дата импорта": normalize_text(item_payload.get("imported_at") or order_payload.get("imported_at")),
        "Номер заявки SkladBot": normalize_text(order_payload.get("skladbot_request_number")),
        "ID заявки SkladBot": normalize_text(order_payload.get("skladbot_request_id")),
        "Статус SkladBot": format_skladbot_status(order_payload.get("skladbot_status")),
        "Последняя проверка SkladBot": normalize_text(order_payload.get("skladbot_checked_at")),
    }
    row = build_import_record_row(record)
    if len(row) < header_len:
        row.extend([""] * (header_len - len(row)))
    return row[:header_len]


def mark_backend_return_rows(archive_sheet, returns_sheet, order):
    archive_header = ensure_return_sheet_layout(archive_sheet)
    ensure_return_sheet_layout(returns_sheet)
    archive_rows = archive_sheet.get_all_values()
    returns_rows = returns_sheet.get_all_values()
    header_idx = get_header_index(archive_header)
    returns_import_ids, returns_order_ids = get_existing_import_keys(returns_rows)

    target_rows = []
    used_rows = set()
    for item in getattr(order, "items", []) or []:
        row_number = find_backend_item_row_number(archive_rows, item)
        if row_number and row_number not in used_rows:
            target_rows.append((row_number, item))
            used_rows.add(row_number)

    if not target_rows:
        return GoogleSheetsExportResult(status="missing").as_dict()

    updates = []
    rows_to_returns = []
    return_values = backend_order_return_values(order)
    header_len = len(archive_header)
    for row_number, item in sorted(target_rows, key=lambda value: value[0]):
        source_row = list(archive_rows[row_number - 1])
        if len(source_row) < header_len:
            source_row.extend([""] * (header_len - len(source_row)))

        for column, value in return_values.items():
            idx = header_idx.get(column)
            if idx is None:
                continue
            source_row[idx] = value
            updates.append({
                "range": f"{column_index_to_letter(idx)}{row_number}",
                "values": [[value]],
            })

        source_order_id = get_backend_item_source_order_id(item)
        source_import_id = get_backend_item_source_import_id(item)
        if (
            (source_order_id and source_order_id in returns_order_ids)
            or (source_import_id and source_import_id in returns_import_ids)
        ):
            continue
        rows_to_returns.append(source_row[:header_len])

    if updates:
        archive_sheet.batch_update(updates, value_input_option="USER_ENTERED")
    if rows_to_returns:
        start_row = max(2, len(returns_rows) + 1)
        end_col = column_index_to_letter(header_len - 1)
        returns_sheet.batch_update([{
            "range": f"A{start_row}:{end_col}{start_row + len(rows_to_returns) - 1}",
            "values": rows_to_returns,
        }], value_input_option="USER_ENTERED")

    return GoogleSheetsExportResult(status="completed", updated=len(target_rows)).as_dict()


def update_missing_sheet_addresses(sheet, all_rows, records):
    if not all_rows or not records:
        return 0

    header = [normalize_header_name(col) for col in all_rows[0]]
    header_idx = get_header_index(header)
    address_idx = header_idx.get("Адрес")
    import_indices = get_header_indices(all_rows[0], "ID импорта")
    order_indices = get_header_indices(all_rows[0], "ID заказа")
    if address_idx is None or (not import_indices and not order_indices):
        return 0

    rows_by_import_id = {}
    rows_by_order_id = {}
    rows_by_business_key = {}
    ambiguous_business_keys = set()
    for sheet_row_number, row in enumerate(all_rows[1:], start=2):
        for import_idx in import_indices:
            import_id = get_cell(row, import_idx)
            if import_id and import_id not in rows_by_import_id:
                rows_by_import_id[import_id] = (sheet_row_number, row)
        for order_idx in order_indices:
            order_id = get_cell(row, order_idx)
            if order_id and order_id not in rows_by_order_id:
                rows_by_order_id[order_id] = (sheet_row_number, row)
        if is_missing_address(get_cell(row, address_idx)):
            business_key = make_address_backfill_business_key({
                ORDER_DATE_COLUMN: get_cell(row, header_idx.get(ORDER_DATE_COLUMN, header_idx.get(LEGACY_ORDER_DATE_COLUMN))),
                "Тип оплаты": get_cell(row, header_idx.get("Тип оплаты")),
                "Клиент": get_cell(row, header_idx.get("Клиент")),
                "Торговый представитель": get_cell(row, header_idx.get("Торговый представитель")),
                "Товары": get_cell(row, header_idx.get("Товары")),
                "Кол-во ШТ": get_cell(row, header_idx.get("Кол-во ШТ")),
                "Кол-во блок": get_cell(row, header_idx.get("Кол-во блок")),
            })
            if business_key:
                if business_key in rows_by_business_key:
                    ambiguous_business_keys.add(business_key)
                else:
                    rows_by_business_key[business_key] = (sheet_row_number, row)

    updates = []
    updated_rows = set()
    address_column = column_index_to_letter(address_idx)
    for record in records:
        new_address = normalize_text(record.get("Адрес"))
        if not is_real_address(new_address):
            continue

        import_id = normalize_text(record.get("ID импорта"))
        order_id = normalize_text(record.get("ID заказа"))
        found = rows_by_import_id.get(import_id) if import_id else None
        if found is None and order_id:
            found = rows_by_order_id.get(order_id)
        if found is None:
            business_key = make_address_backfill_business_key(record)
            if business_key and business_key not in ambiguous_business_keys:
                found = rows_by_business_key.get(business_key)
        if found is None:
            continue

        sheet_row_number, row = found
        if sheet_row_number in updated_rows:
            continue
        old_address = get_cell(row, address_idx)
        if not is_missing_address(old_address):
            continue

        updates.append({
            "range": f"{address_column}{sheet_row_number}",
            "values": [[new_address]],
        })
        if address_idx < len(row):
            row[address_idx] = new_address
        updated_rows.add(sheet_row_number)

    if updates:
        sheet.batch_update(updates, value_input_option="USER_ENTERED")
    return len(updates)


def make_address_backfill_business_key(record):
    payload = {
        "date": parse_date_to_standard(record.get(ORDER_DATE_COLUMN) or record.get(LEGACY_ORDER_DATE_COLUMN)),
        "payment": normalize_lookup_text(record.get("Тип оплаты")),
        "client": normalize_lookup_text(record.get("Клиент")),
        "representative": normalize_lookup_text(record.get("Торговый представитель")),
        "product": normalize_lookup_text(record.get("Товары")),
        "quantity_pieces": parse_int_value(record.get("Кол-во ШТ")),
        "quantity_blocks": parse_int_value(record.get("Кол-во блок")),
    }
    if (
        not payload["date"]
        or not payload["payment"]
        or not payload["client"]
        or not payload["product"]
        or payload["quantity_pieces"] <= 0
        or payload["quantity_blocks"] <= 0
    ):
        return ""
    return make_hash(payload)


def get_google_client():
    credentials = load_google_credentials()
    if not credentials:
        raise GoogleSheetsExportDisabled(
            f"set {GOOGLE_CREDENTIALS_JSON_BASE64_ENV}, {GOOGLE_CREDENTIALS_JSON_ENV} or {GOOGLE_CREDENTIALS_FILE_ENV}"
        )

    import gspread

    return gspread.service_account_from_dict(credentials, http_client=GoogleTimeoutHTTPClient)


def load_google_credentials():
    raw_base64 = normalize_text(os.environ.get(GOOGLE_CREDENTIALS_JSON_BASE64_ENV))
    if raw_base64:
        decoded = base64.b64decode(raw_base64).decode("utf-8")
        return json.loads(decoded)

    raw_json = normalize_text(os.environ.get(GOOGLE_CREDENTIALS_JSON_ENV))
    if raw_json:
        return json.loads(raw_json)

    file_path = normalize_text(os.environ.get(GOOGLE_CREDENTIALS_FILE_ENV))
    if file_path:
        with open(file_path, "r", encoding="utf-8") as file_obj:
            return json.load(file_obj)

    return None


def make_sheet_record(row, import_id="", item_key="", filename=""):
    source_file = normalize_text(row.get("source_file")) or normalize_text(filename)
    source_row = normalize_text(row.get("source_row"))
    source_order_id = normalize_text(row.get("source_order_id")) or normalize_text(item_key)
    source_import_id = normalize_text(row.get("source_import_id")) or normalize_text(item_key)
    return {
        ORDER_DATE_COLUMN: format_sheet_date(row.get("order_date")),
        "Тип оплаты": normalize_text(row.get("payment_type")),
        "Клиент": normalize_text(row.get("client")),
        "Адрес": normalize_text(row.get("address")) or "Адрес не указан",
        "Торговый представитель": normalize_text(row.get("representative")),
        "Товары": normalize_text(row.get("product")),
        "Кол-во ШТ": row.get("quantity_pieces") or 0,
        "Кол-во блок": row.get("quantity_blocks") or 0,
        "Отсканированные коды": "",
        STATUS_COLUMN: backend_status_to_sheet_status(row.get("status")),
        "ID заказа": source_order_id,
        "ID импорта": source_import_id,
        "Источник файла": source_file,
        "Строка файла": source_row,
        "Дата импорта": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        "Номер заявки SkladBot": normalize_text(row.get("skladbot_request_number")),
        "ID заявки SkladBot": normalize_text(row.get("skladbot_request_id")),
        "Статус SkladBot": "",
        "Последняя проверка SkladBot": "",
    }


def backend_status_to_sheet_status(value):
    text = normalize_text(value).casefold()
    if text in {"completed", "done", "closed", "выполнено", "готово"}:
        return STATUS_COMPLETED
    return STATUS_NOT_COMPLETED


def format_sheet_date(value):
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    parsed = parse_date_to_standard(value)
    return parsed or normalize_text(value)


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


def ensure_import_sheet_layout(sheet):
    all_rows = sheet.get_all_values()
    required_len = SERVICE_COLUMN_START_INDEX + len(SERVICE_COLUMNS)
    if not all_rows:
        header = build_import_sheet_header()
        sheet.append_row(header, value_input_option="USER_ENTERED")
        return header

    header = [normalize_header_name(col) for col in all_rows[0]]
    original_header = list(header)
    if len(header) < required_len:
        header.extend([""] * (required_len - len(header)))

    for target_idx, column in get_import_column_targets():
        header[target_idx] = column

    if header != original_header:
        last_col = column_index_to_letter(len(header) - 1)
        sheet.batch_update([{
            "range": f"A1:{last_col}1",
            "values": [header],
        }], value_input_option="USER_ENTERED")
    return header


def build_import_record_row(record):
    row = [""] * (SERVICE_COLUMN_START_INDEX + len(SERVICE_COLUMNS))
    for idx, column in enumerate(WORKING_COLUMNS):
        row[idx] = record.get(column, "")
    for offset, column in enumerate(SERVICE_COLUMNS):
        row[SERVICE_COLUMN_START_INDEX + offset] = record.get(column, "")
    return row


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


def find_record_row_number(all_rows, record):
    if not all_rows:
        return None
    header = all_rows[0]
    import_indices = get_header_indices(header, "ID импорта")
    order_indices = get_header_indices(header, "ID заказа")
    import_id = normalize_text(record.get("ID импорта"))
    order_id = normalize_text(record.get("ID заказа"))
    for row_number, row in enumerate(all_rows[1:], start=2):
        if import_id:
            for import_idx in import_indices:
                if get_cell(row, import_idx) == import_id:
                    return row_number
        if order_id:
            for order_idx in order_indices:
                if get_cell(row, order_idx) == order_id:
                    return row_number
    return None


def get_existing_order_duplicate_keys(all_rows):
    if not all_rows:
        return set()

    header = [normalize_header_name(col) for col in all_rows[0]]
    header_idx = get_header_index(header)
    duplicate_keys = set()
    for row in all_rows[1:]:
        record = {
            ORDER_DATE_COLUMN: get_cell(row, header_idx.get(ORDER_DATE_COLUMN, header_idx.get(LEGACY_ORDER_DATE_COLUMN))),
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


def get_or_create_sheet(spreadsheet, title):
    try:
        return spreadsheet.worksheet(title)
    except Exception:
        return spreadsheet.add_worksheet(title=title, rows=1000, cols=SERVICE_COLUMN_START_INDEX + len(SERVICE_COLUMNS) + 8)


def ensure_return_sheet_layout(sheet):
    header = ensure_import_sheet_layout(sheet)
    all_rows = sheet.get_all_values()
    if all_rows:
        header = [normalize_header_name(col) for col in all_rows[0]]
    for column in RETURN_COLUMNS:
        if column not in header:
            header.append(column)
    last_col = column_index_to_letter(len(header) - 1)
    sheet.batch_update([{
        "range": f"A1:{last_col}1",
        "values": [header],
    }], value_input_option="USER_ENTERED")
    return header


def find_backend_item_row_number(all_rows, item):
    if not all_rows:
        return None
    source_import_id = get_backend_item_source_import_id(item)
    source_order_id = get_backend_item_source_order_id(item)
    if not source_import_id and not source_order_id:
        return None

    header = all_rows[0]
    import_indices = get_header_indices(header, "ID импорта")
    order_indices = get_header_indices(header, "ID заказа")
    for row_number, row in enumerate(all_rows[1:], start=2):
        if source_import_id:
            for import_idx in import_indices:
                if get_cell(row, import_idx) == source_import_id:
                    return row_number
        if source_order_id:
            for order_idx in order_indices:
                if get_cell(row, order_idx) == source_order_id:
                    return row_number
    return None


def get_backend_item_source_import_id(item):
    raw_payload = getattr(item, "raw_payload", None) or {}
    return normalize_text(raw_payload.get("source_import_id"))


def get_backend_item_source_order_id(item):
    raw_payload = getattr(item, "raw_payload", None) or {}
    return normalize_text(raw_payload.get("source_order_id"))


def backend_item_codes(item):
    scans = list(getattr(item, "scan_codes", []) or [])
    scans.sort(key=lambda scan: (str(getattr(scan, "scanned_at", "") or ""), str(getattr(scan, "id", "") or "")))
    return [normalize_text(getattr(scan, "code", "")) for scan in scans if normalize_text(getattr(scan, "code", ""))]


def merge_codes(existing_codes, new_codes):
    merged = []
    seen = set()
    for code in [*(existing_codes or []), *(new_codes or [])]:
        text = normalize_text(code)
        if text and text not in seen:
            merged.append(text)
            seen.add(text)
    return merged


def backend_item_sheet_status(item):
    status = normalize_text(getattr(item, "status", "")).casefold()
    scanned_blocks = parse_int_value(getattr(item, "scanned_blocks", 0))
    quantity_blocks = parse_int_value(getattr(item, "quantity_blocks", 0))
    if status in {"completed", "done", "closed", "returned"} or (quantity_blocks > 0 and scanned_blocks >= quantity_blocks):
        return STATUS_COMPLETED
    return STATUS_NOT_COMPLETED


def apply_backend_item_state_to_row(row, header_idx, item, completed=False, sheet_status=None):
    codes_idx = header_idx.get("Отсканированные коды")
    if codes_idx is not None:
        row[codes_idx] = "\n".join(backend_item_codes(item))
    status_idx = header_idx.get(STATUS_COLUMN)
    if status_idx is not None:
        row[status_idx] = normalize_text(sheet_status) or (STATUS_COMPLETED if completed else backend_item_sheet_status(item))


def archive_row_already_exists(source_row, source_header_idx, archive_rows, archive_header_idx):
    if not archive_rows:
        return False

    source_order_id = get_cell(source_row, source_header_idx.get("ID заказа"))
    source_import_id = get_cell(source_row, source_header_idx.get("ID импорта"))
    if not source_order_id and not source_import_id:
        return False

    for row in archive_rows[1:]:
        if source_order_id and get_cell(row, archive_header_idx.get("ID заказа")) == source_order_id:
            return True
        if source_import_id and get_cell(row, archive_header_idx.get("ID импорта")) == source_import_id:
            return True
    return False


def backend_order_return_values(order):
    raw_payload = getattr(order, "raw_payload", None) or {}
    return {
        RETURN_STATUS_COLUMN: RETURN_STATUS_VALUE,
        RETURN_DATE_COLUMN: normalize_text(raw_payload.get("returned_at")) or datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        RETURN_REFERENCE_COLUMN: normalize_text(raw_payload.get("return_reference")),
        RETURNED_BY_COLUMN: normalize_text(raw_payload.get("returned_by")) or "backend",
    }


def split_codes(value):
    return [
        item.strip(" \t\r\n")
        for item in str(value or "").replace("\r", "\n").split("\n")
        if item.strip(" \t\r\n")
    ]


def make_order_duplicate_key(record):
    payload = {
        "date": parse_date_to_standard(record.get(ORDER_DATE_COLUMN) or record.get(LEGACY_ORDER_DATE_COLUMN)),
        "payment": normalize_lookup_text(record.get("Тип оплаты")),
        "client": normalize_lookup_text(record.get("Клиент")),
        "address": normalize_lookup_text(record.get("Адрес")),
        "representative": normalize_lookup_text(record.get("Торговый представитель")),
        "product": normalize_lookup_text(record.get("Товары")),
        "quantity": parse_int_value(record.get("Кол-во ШТ")),
    }
    if (
        not payload["date"]
        or not payload["payment"]
        or not payload["client"]
        or not payload["product"]
        or payload["quantity"] <= 0
    ):
        return ""
    return make_hash(payload)


def normalize_text(value):
    return str(value or "").strip()


def normalize_header_name(value):
    return normalize_text(value).replace("\ufeff", "")


def get_header_index(header):
    return {normalize_header_name(col): idx for idx, col in enumerate(header) if normalize_header_name(col)}


def get_header_indices(header, column_name):
    normalized_column = normalize_header_name(column_name)
    return [idx for idx, col in enumerate(header) if normalize_header_name(col) == normalized_column]


def get_cell(row, idx):
    if idx is None or idx >= len(row):
        return ""
    return normalize_text(row[idx])


def parse_int_value(value):
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = normalize_text(value).replace(" ", "").replace(",", ".")
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def parse_date_to_standard(value):
    text = normalize_text(value)
    if not text:
        return ""
    if " " in text:
        text = text.split()[0]
    text = re.sub(r'^[\'"]+|[\'"]+$', "", text)
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%y", "%Y.%m.%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.strftime("%d.%m.%Y")
        except ValueError:
            pass
    return text


def normalize_lookup_text(value):
    text = normalize_text(value).lower().replace("ё", "е")
    text = text.replace("\ufeff", "")
    text = re.sub(r"[*:]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_missing_address(value):
    text = normalize_lookup_text(value)
    return (
        not text
        or text in {
            "адрес не указан",
            "адрес не найден",
            "адреса не найдены",
            "адрес не определен",
            "адрес отсутствует",
        }
        or text.startswith("координаты")
    )


def is_real_address(value):
    text = normalize_lookup_text(value)
    return bool(text and not is_missing_address(text))


def column_index_to_letter(index):
    index += 1
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def make_hash(payload):
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    import hashlib

    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()
