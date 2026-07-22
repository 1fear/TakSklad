import os
from datetime import datetime

from .catalog import calculate_blocks, load_product_catalog, merge_product_catalog_defaults
from .config import (
    ORDER_DATE_COLUMN,
    STATUS_COLUMN,
    STATUS_NOT_COMPLETED,
)
from .excel_normalizer import (
    detect_excel_source,
    get_source_cell,
    is_summary_row,
    parse_strict_coordinate_pair,
)
from .geocoding import reverse_geocode_yandex
from .orders import make_order_id
from .spreadsheet_safety import (
    SpreadsheetSafetyError,
    load_safe_workbook,
    normalize_spreadsheet_filename,
)
from .storage import load_data_section, mutate_data_section
from .utils import (
    file_sha1,
    file_sha256,
    make_hash,
    normalize_lookup_text,
    normalize_text,
    parse_date_to_standard,
    parse_int_value,
)


MISSING_ADDRESS_MARKERS = {
    "адрес не указан",
    "адрес не найден",
    "адреса не найдены",
    "адрес не определен",
    "адрес отсутствует",
    "самовывоз",
    "самовывоз со склада",
    "нет",
    "n/a",
    "na",
    "null",
    "none",
    "-",
    "—",
}
PICKUP_ADDRESS = "Самовывоз со склада"


def is_missing_address_text(value):
    text = normalize_lookup_text(value)
    return not text or text in MISSING_ADDRESS_MARKERS or text.startswith("координаты")


def is_pickup_address(value):
    text = normalize_lookup_text(value)
    return text in {normalize_lookup_text(PICKUP_ADDRESS), "самовывоз"}


def parse_coordinate_component(value):
    text = normalize_text(value).replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def format_coordinate(value):
    return f"{value:.12f}".rstrip("0").rstrip(".")


def normalize_coordinates(value):
    text = normalize_text(value)
    if not text:
        return ""
    import re
    numbers = re.findall(r"-?\d+(?:[.,]\d+)?", text)
    if len(numbers) < 2:
        return ""
    latitude_text = numbers[0].replace(",", ".")
    longitude_text = numbers[1].replace(",", ".")
    try:
        latitude = float(latitude_text)
        longitude = float(longitude_text)
    except ValueError:
        return ""
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return ""
    return f"{latitude_text}, {longitude_text}"


def normalize_split_coordinates(latitude_value, longitude_value):
    latitude = parse_coordinate_component(latitude_value)
    longitude = parse_coordinate_component(longitude_value)
    if latitude is None or longitude is None:
        return ""
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return ""
    return f"{format_coordinate(latitude)}, {format_coordinate(longitude)}"


def get_coordinates_from_row(row, columns):
    candidates = list(columns.get("coords_candidates") or [])
    primary = columns.get("coords")
    if columns.get("coords_inferred_from_content"):
        return parse_strict_coordinate_pair(get_source_cell(row, primary))

    if primary is not None and primary not in candidates:
        candidates.insert(0, primary)

    expanded_candidates = []
    for index in candidates:
        for offset in (0, 1, 2):
            expanded = index + offset
            if expanded < len(row) and expanded not in expanded_candidates:
                expanded_candidates.append(expanded)

    for index in expanded_candidates:
        coords = normalize_coordinates(get_source_cell(row, index))
        if coords:
            return coords

    for index in candidates:
        coords = normalize_split_coordinates(get_source_cell(row, index), get_source_cell(row, index + 1))
        if coords:
            return coords
    return ""


def make_source_row_duplicate_key(row):
    return make_hash({
        "date": row["date"],
        "payment": normalize_lookup_text(row["payment"]),
        "client": normalize_lookup_text(row["client"]),
        "address": normalize_lookup_text(row["address"]),
        "representative": normalize_lookup_text(row["representative"]),
        "inn": normalize_lookup_text(row["inn"]),
        "product": normalize_lookup_text(row["product"]),
        "quantity": parse_int_value(row["quantity"]),
        "coords": normalize_lookup_text(row["coords"]),
        "source_address": normalize_lookup_text(row["source_address"]),
        "lead_status": normalize_lookup_text(row["lead_status"]),
    })


def parse_excel_order_files(file_paths, source_names=None):
    catalog = load_product_catalog()
    raw_rows = []
    errors = []
    warnings = []
    source_rows_count = 0
    geocoded_count = 0
    geocode_failed_count = 0
    geocode_cache = {}
    source_names = source_names or {}
    source_names_by_path = {
        os.path.abspath(path): name
        for path, name in source_names.items()
    }

    for file_path in file_paths:
        candidate_name = source_names_by_path.get(os.path.abspath(file_path)) or os.path.basename(file_path)
        try:
            file_name = normalize_spreadsheet_filename(candidate_name)
            workbook = load_safe_workbook(file_path, file_name=file_name, data_only=True, read_only=True)
        except SpreadsheetSafetyError as exc:
            errors.append(str(exc))
            continue

        source = detect_excel_source(workbook, file_name)
        if not source:
            workbook.close()
            errors.append(f"{file_name}: не найден шаблон с обязательными колонками")
            continue

        sheet_name = source["sheet_name"]
        worksheet = workbook[sheet_name]
        columns = source["columns"]
        default_date = source.get("default_date") or ""

        source_file_hash = file_sha1(file_path)
        source_file_sha256 = file_sha256(file_path)
        rows_iter = worksheet.iter_rows(min_row=source["first_data_row"], values_only=True)
        for row_number, row in enumerate(rows_iter, start=source["first_data_row"]):
            if not row or not any(normalize_text(cell) for cell in row if cell is not None):
                continue
            if is_summary_row(row, columns):
                continue

            source_rows_count += 1
            client = get_source_cell(row, columns["client"])
            payment = get_source_cell(row, columns["payment"])
            product = get_source_cell(row, columns["product"])
            quantity = parse_int_value(get_source_cell(row, columns["quantity"]))

            if not client or not payment or not product or quantity <= 0:
                warnings.append(f"{file_name}, строка {row_number}: пропущена, не заполнены клиент/оплата/товар/количество")
                continue

            date_value = (
                parse_date_to_standard(get_source_cell(row, columns.get("date")))
                or default_date
                or datetime.now().strftime("%d.%m.%Y")
            )
            source_address = get_source_cell(row, columns.get("address"))
            address = PICKUP_ADDRESS if is_pickup_address(source_address) else (
                "" if is_missing_address_text(source_address) else source_address
            )
            coords = get_coordinates_from_row(row, columns)
            if not address and coords:
                geocoded_address, geocode_error = reverse_geocode_yandex(coords, cache=geocode_cache)
                if geocoded_address:
                    address = geocoded_address
                    geocoded_count += 1
                else:
                    geocode_failed_count += 1
                    address = f"Координаты: {coords}"
                    warnings.append(f"{file_name}, строка {row_number}: адрес по координатам не получен ({geocode_error})")
            if not address:
                address = PICKUP_ADDRESS

            representative = get_source_cell(row, columns.get("representative"))
            inn = get_source_cell(row, columns.get("inn"))
            lead_status = get_source_cell(row, columns.get("lead_status"))
            source_id = make_hash({
                "file_hash": source_file_hash,
                "sheet": sheet_name,
                "row": row_number,
            })

            raw_rows.append({
                "date": date_value,
                "payment": payment,
                "client": client,
                "address": address,
                "representative": representative,
                "inn": inn,
                "product": product,
                "quantity": quantity,
                "coords": coords,
                "source_address": source_address,
                "lead_status": lead_status,
                "source_id": source_id,
                "source_file": file_name,
                "source_file_sha256": source_file_sha256,
                "source_row": row_number,
            })
        workbook.close()

    unique_raw_rows = []
    duplicate_source_rows = []
    source_duplicate_keys = set()
    for row in raw_rows:
        duplicate_key = make_source_row_duplicate_key(row)
        if duplicate_key in source_duplicate_keys:
            duplicate_source_rows.append(row)
            continue
        source_duplicate_keys.add(duplicate_key)
        unique_raw_rows.append(row)

    records = []
    for item in unique_raw_rows:
        blocks, pieces_per_block = calculate_blocks(item["quantity"], item["product"], catalog, warnings)
        record = {
            ORDER_DATE_COLUMN: item["date"],
            "Тип оплаты": item["payment"],
            "Клиент": item["client"],
            "Адрес": item["address"],
            "Координаты": item["coords"],
            "Торговый представитель": item["representative"],
            "Товары": item["product"],
            "Кол-во ШТ": item["quantity"],
            "Кол-во блок": blocks,
            "Отсканированные коды": "",
            STATUS_COLUMN: STATUS_NOT_COMPLETED,
            "ID импорта": make_hash([item["source_id"]]),
            "Источник файла": item["source_file"],
            "Строка файла": str(item["source_row"]),
            "Дата импорта": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        }
        record["ID заказа"] = make_order_id(record)
        record["_pieces_per_block"] = pieces_per_block
        record["_source_file_sha256"] = [item["source_file_sha256"]]
        records.append(record)

    merge_product_catalog_defaults(catalog)

    return {
        "records": records,
        "errors": errors,
        "warnings": warnings,
        "source_rows_count": source_rows_count,
        "source_duplicate_rows_count": len(duplicate_source_rows),
        "files_count": len(file_paths),
        "geocoded_count": geocoded_count,
        "geocode_failed_count": geocode_failed_count,
    }


def find_successful_import_by_file_hash(file_hash):
    normalized_target = normalize_text(file_hash).lower()
    if not normalized_target:
        return None

    history = load_data_section("import_history", [])
    if not isinstance(history, list):
        return None

    for item in reversed(history):
        if not isinstance(item, dict) or parse_int_value(item.get("imported")) <= 0:
            continue
        entry_hashes = set()
        for key in ("source_file_hashes_sha256", "file_hashes_sha256", "file_sha256"):
            raw_hashes = item.get(key, [])
            if isinstance(raw_hashes, str):
                raw_hashes = [raw_hashes]
            for value in raw_hashes:
                normalized_hash = normalize_text(value).lower()
                if normalized_hash:
                    entry_hashes.add(normalized_hash)
        if normalized_target in entry_hashes:
            return item
    return None
