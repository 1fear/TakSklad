import os
import re
from datetime import date, datetime

from .config import SOURCE_OPTIONAL_ALIASES, SOURCE_REQUIRED_ALIASES
from .utils import clean_file_name, normalize_lookup_text, normalize_text, parse_date_to_standard


MAX_HEADER_SCAN_ROWS = 40

EXTRA_REQUIRED_ALIASES = {
    "client": [
        "Покупатель",
        "Контрагент",
        "Наименование клиента",
        "Название компании",
        "Название компании/Имя человека",
        "Юридическое лицо",
    ],
    "payment": [
        "Способ оплаты",
        "Форма оплаты",
        "Комментарий оплаты",
    ],
    "product": [
        "ТМЦ",
        "SKU",
        "Артикул",
        "Продукт",
        "Наименование продукции",
    ],
    "quantity": [
        "Количество заказа",
        "Кол-во заказа",
        "Заказано",
        "В заявке",
        "Штук",
        "ШТ",
    ],
}

EXTRA_OPTIONAL_ALIASES = {
    "date": [
        "Дата выгрузки",
        "Дата поставки",
        "Дата документа",
    ],
    "address": [
        "Адрес клиента",
        "Адрес торговой точки",
        "Адрес получателя",
        "Локация",
    ],
    "coords": [
        "Координаты клиента",
        "GPS",
        "Геолокация",
    ],
    "representative": [
        "Торговый",
        "Агент",
        "Ответственный",
    ],
    "inn": [
        "ИНН юр. лица",
        "ИНН юр лица",
    ],
}

SUMMARY_MARKERS = {"итого", "total", "grand total", "всего"}
DATE_PATTERN = re.compile(r"(?<!\d)(\d{1,2})[._/-](\d{1,2})[._/-](\d{2,4})(?!\d)")


def merge_aliases(base_aliases, extra_aliases):
    merged = {}
    for key, aliases in base_aliases.items():
        seen = set()
        merged[key] = []
        for alias in list(aliases) + list(extra_aliases.get(key, [])):
            normalized = normalize_lookup_text(alias)
            if normalized and normalized not in seen:
                merged[key].append(alias)
                seen.add(normalized)
    return merged


NORMALIZER_REQUIRED_ALIASES = merge_aliases(SOURCE_REQUIRED_ALIASES, EXTRA_REQUIRED_ALIASES)
NORMALIZER_OPTIONAL_ALIASES = merge_aliases(SOURCE_OPTIONAL_ALIASES, EXTRA_OPTIONAL_ALIASES)


def get_source_header_index(header):
    header_idx = {}
    for idx, col in enumerate(header):
        normalized = normalize_lookup_text(col)
        if normalized and normalized not in header_idx:
            header_idx[normalized] = idx
    return header_idx


def find_source_column(header_idx, aliases):
    for alias in aliases:
        key = normalize_lookup_text(alias)
        if key in header_idx:
            return header_idx[key]
    return None


def get_source_cell(row, idx):
    if idx is None or idx >= len(row):
        return ""
    value = row[idx]
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    return normalize_text(value)


def row_has_data(row):
    return bool(row and any(normalize_text(cell) for cell in row if cell is not None))


def build_source_columns(header):
    header_idx = get_source_header_index(header)
    columns = {}
    missing = []

    for key, aliases in NORMALIZER_REQUIRED_ALIASES.items():
        idx = find_source_column(header_idx, aliases)
        if idx is None:
            missing.append(aliases[0])
        columns[key] = idx

    for key, aliases in NORMALIZER_OPTIONAL_ALIASES.items():
        columns[key] = find_source_column(header_idx, aliases)

    optional_found = sum(1 for key in NORMALIZER_OPTIONAL_ALIASES if columns.get(key) is not None)
    required_found = len(NORMALIZER_REQUIRED_ALIASES) - len(missing)
    score = required_found * 10 + optional_found
    return columns, missing, score


def detect_header_row(preview_rows):
    best = None
    for row_number, row in enumerate(preview_rows, start=1):
        if not row_has_data(row):
            continue
        columns, missing, score = build_source_columns(row)
        if missing:
            continue

        candidate = {
            "header_row": row_number,
            "columns": columns,
            "score": score,
        }
        if not best or candidate["score"] > best["score"]:
            best = candidate

    return best


def normalize_date_text(value):
    text = normalize_text(value).replace("_", ".").replace("/", ".").replace("-", ".")
    return parse_date_to_standard(text)


def extract_dates_from_text(value):
    text = normalize_text(value)
    if not text:
        return []

    dates = []
    for day, month, year in DATE_PATTERN.findall(text):
        if len(year) == 2:
            year = "20" + year
        parsed = normalize_date_text(f"{day}.{month}.{year}")
        if parsed:
            dates.append(parsed)
    return dates


def extract_default_date_from_file_name(file_name):
    base_name = os.path.splitext(clean_file_name(file_name))[0]
    dates = extract_dates_from_text(base_name)
    return dates[-1] if dates else ""


def extract_default_date_from_context(preview_rows, header_row):
    dates = []
    for row in preview_rows[: max(header_row - 1, 0)]:
        for cell in row:
            dates.extend(extract_dates_from_text(cell))
    return dates[-1] if dates else ""


def is_summary_row(row, columns):
    if not row_has_data(row):
        return False

    normalized_values = [
        normalize_lookup_text(cell)
        for cell in row
        if normalize_lookup_text(cell)
    ]
    if not normalized_values:
        return False

    first_value = normalized_values[0]
    if first_value in SUMMARY_MARKERS:
        return True

    key_values = [
        normalize_lookup_text(get_source_cell(row, columns.get("client"))),
        normalize_lookup_text(get_source_cell(row, columns.get("payment"))),
        normalize_lookup_text(get_source_cell(row, columns.get("product"))),
    ]
    return key_values.count("итого") >= 2


def detect_excel_source(workbook, file_name):
    sheet_names = list(workbook.sheetnames)
    preferred_names = []
    if "Заявки" in sheet_names:
        preferred_names.append("Заявки")
    preferred_names.extend(name for name in sheet_names if name not in preferred_names)

    best = None
    for sheet_name in preferred_names:
        worksheet = workbook[sheet_name]
        preview_rows = list(
            worksheet.iter_rows(
                min_row=1,
                max_row=min(MAX_HEADER_SCAN_ROWS, worksheet.max_row or MAX_HEADER_SCAN_ROWS),
                values_only=True,
            )
        )
        detected = detect_header_row(preview_rows)
        if not detected:
            continue

        default_date = extract_default_date_from_file_name(file_name)
        default_date_source = "file_name" if default_date else ""
        if not default_date:
            default_date = extract_default_date_from_context(preview_rows, detected["header_row"])
            default_date_source = "sheet_header" if default_date else ""

        candidate = {
            "sheet_name": sheet_name,
            "header_row": detected["header_row"],
            "first_data_row": detected["header_row"] + 1,
            "columns": detected["columns"],
            "default_date": default_date,
            "default_date_source": default_date_source,
            "score": detected["score"] + (5 if sheet_name == "Заявки" else 0),
        }
        if not best or candidate["score"] > best["score"]:
            best = candidate

    return best
