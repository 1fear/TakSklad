import logging
from datetime import datetime

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from config import (
    CHAPMAN_DATA_FORMAT_COLUMN_COUNT,
    CHAPMAN_DATA_LEAD_STATUS,
    CHAPMAN_DATA_VISIBLE_COLUMN_COUNT,
    CREDENTIALS_FILE,
    LEGACY_ORDER_DATE_COLUMN,
    ORDER_DATE_COLUMN,
    REQUIRED_COLUMNS,
    SERVICE_COLUMNS,
    SERVICE_COLUMN_START_INDEX,
    SHEET_NAME,
    SPREADSHEET_ID,
    STATUS_COLUMN,
    STATUS_COMPLETED,
    WORKING_COLUMNS,
)
from orders import (
    get_order_date_header_index,
    get_order_date_value,
    get_order_status,
    is_completed_status,
    is_order_active,
    make_order_duplicate_key,
    row_matches_order,
)
from storage import load_credentials_data
from utils import (
    column_index_to_letter,
    get_cell,
    get_header_index,
    get_header_indices,
    make_hash,
    normalize_lookup_text,
    normalize_header_name,
    normalize_text,
    parse_date_to_standard,
    parse_int_value,
    split_codes,
)


def get_google_client():
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    credentials = load_credentials_data()
    if isinstance(credentials, dict) and credentials.get("client_email"):
        creds = ServiceAccountCredentials.from_json_keyfile_dict(credentials, scope)
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
    return gspread.authorize(creds)


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

    # A:J are warehouse fields and AA:AE are import metadata.
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


def build_chapman_data_row(record, received_date=None):
    source = record.get("_chapman_data", {})
    received_date = received_date or datetime.now().strftime("%d.%m.%Y")
    delivery_date = source.get("delivery_date") or get_order_date_value(record)
    lead_status = source.get("lead_status") or CHAPMAN_DATA_LEAD_STATUS
    row = [
        received_date,
        delivery_date,
        record.get("Торговый представитель", ""),
        record.get("Клиент", ""),
        source.get("inn", ""),
        source.get("address", record.get("Адрес", "")),
        source.get("coords", ""),
        record.get("Товары", ""),
        record.get("Тип оплаты", ""),
        lead_status,
        record.get("Кол-во ШТ", ""),
        record.get("Кол-во блок", ""),
    ]
    row.extend([""] * (CHAPMAN_DATA_VISIBLE_COLUMN_COUNT - len(row)))
    return row


def make_chapman_data_duplicate_key(row):
    payload = {
        # The first Data date is the import day; the delivery date identifies
        # the order and must keep same orders from different dates separate.
        "delivery_date": parse_date_to_standard(get_cell(row, 1)),
        "representative": normalize_lookup_text(get_cell(row, 2)),
        "client": normalize_lookup_text(get_cell(row, 3)),
        "inn": normalize_lookup_text(get_cell(row, 4)),
        "address": normalize_lookup_text(get_cell(row, 5)),
        "coords": normalize_lookup_text(get_cell(row, 6)),
        "product": normalize_lookup_text(get_cell(row, 7)),
        "payment": normalize_lookup_text(get_cell(row, 8)),
        "lead_status": normalize_lookup_text(get_cell(row, 9)),
        "quantity": parse_int_value(get_cell(row, 10)),
    }
    if (
        not payload["delivery_date"]
        or not payload["client"]
        or not payload["product"]
        or not payload["payment"]
        or payload["quantity"] <= 0
    ):
        return ""
    return make_hash(payload)


def get_existing_chapman_data_duplicate_keys(all_rows):
    return {
        duplicate_key
        for duplicate_key in (make_chapman_data_duplicate_key(row) for row in all_rows[1:])
        if duplicate_key
    }


def copy_chapman_data_row_format(sheet, source_row, start_row, row_count):
    if row_count <= 0 or source_row < 2:
        return
    sheet.spreadsheet.batch_update({
        "requests": [{
            "copyPaste": {
                "source": {
                    "sheetId": sheet.id,
                    "startRowIndex": source_row - 1,
                    "endRowIndex": source_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": CHAPMAN_DATA_FORMAT_COLUMN_COUNT,
                },
                "destination": {
                    "sheetId": sheet.id,
                    "startRowIndex": start_row - 1,
                    "endRowIndex": start_row - 1 + row_count,
                    "startColumnIndex": 0,
                    "endColumnIndex": CHAPMAN_DATA_FORMAT_COLUMN_COUNT,
                },
                "pasteType": "PASTE_FORMAT",
            }
        }]
    })


def append_chapman_data_records(sheet, records):
    if not records:
        return {"appended": 0, "duplicates": 0, "start_row": None, "end_row": None}

    all_rows = sheet.get_all_values()
    existing_duplicate_keys = get_existing_chapman_data_duplicate_keys(all_rows)
    rows = []
    duplicates = 0
    for record in records:
        row = build_chapman_data_row(record)
        duplicate_key = make_chapman_data_duplicate_key(row)
        if duplicate_key and duplicate_key in existing_duplicate_keys:
            duplicates += 1
            continue
        rows.append(row)
        if duplicate_key:
            existing_duplicate_keys.add(duplicate_key)

    if not rows:
        return {"appended": 0, "duplicates": duplicates, "start_row": None, "end_row": None}

    start_row = max(2, len(all_rows) + 1)
    end_row = start_row + len(rows) - 1

    existing_row_count = getattr(sheet, "row_count", end_row)
    if end_row > existing_row_count and hasattr(sheet, "add_rows"):
        sheet.add_rows(end_row - existing_row_count)

    # Copy the visual template first, then write values so Data keeps its
    # font, fills, borders, alignments and number formats on new rows.
    copy_chapman_data_row_format(sheet, start_row - 1, start_row, len(rows))
    sheet.batch_update([{
        "range": f"A{start_row}:X{end_row}",
        "values": rows,
    }], value_input_option="USER_ENTERED")
    return {"appended": len(rows), "duplicates": duplicates, "start_row": start_row, "end_row": end_row}


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


def get_all_existing_codes(sheet):
    try:
        all_rows = sheet.get_all_values()
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


def get_today_orders():
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
        status_idx = header_idx.get(STATUS_COLUMN)
        status_updates = []

        for row_number, row in enumerate(all_rows[1:], start=2):
            if not any(normalize_text(cell) for cell in row):
                continue

            record = {}
            for col_name, idx in header_idx.items():
                record[col_name] = get_cell(row, idx)

            normalized_date = parse_date_to_standard(get_order_date_value(record))
            scanned_codes = split_codes(record.get("Отсканированные коды"))
            current_status = normalize_text(record.get(STATUS_COLUMN))
            calculated_status = get_order_status(record)
            if status_idx is not None and (
                not current_status
                or (calculated_status == STATUS_COMPLETED and not is_completed_status(current_status))
            ):
                status_updates.append({
                    "range": f"{column_index_to_letter(status_idx)}{row_number}",
                    "values": [[calculated_status]],
                })
                record[STATUS_COLUMN] = calculated_status

            if is_order_active(record):
                record["_row_number"] = row_number
                record["_normalized_date"] = normalized_date
                record["_existing_scanned_codes"] = scanned_codes
                today_orders.append(record)

        if status_updates:
            sheet.batch_update(status_updates, value_input_option="USER_ENTERED")

        return today_orders, sheet
    except Exception:
        logging.exception("Не удалось загрузить данные из Google Sheets")
        raise


def update_scanned_codes_to_gsheet(sheet, order, scanned_codes):
    try:
        if not scanned_codes:
            return False, "Нет отсканированных кодов для записи"

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
            if not existing_set.issubset(scanned_set):
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
        sheet.batch_update(updates, value_input_option="USER_ENTERED")
        return True, "Коды записаны в Google Sheets"
    except Exception as exc:
        logging.exception("Не удалось записать коды в Google Sheets")
        return False, str(exc)
