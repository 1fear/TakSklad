import os
import re
from datetime import date, datetime

from .config import SOURCE_OPTIONAL_ALIASES, SOURCE_REQUIRED_ALIASES
from .utils import clean_file_name, normalize_lookup_text, normalize_text, parse_date_to_standard


MAX_HEADER_SCAN_ROWS = 40
MAX_COORDINATE_INFERENCE_ROWS = 200

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
        "GPS-координаты клиента",
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
STRICT_COORDINATE_PAIR_PATTERN = re.compile(
    r"""
    ^\s*[\[(]?\s*
    (?P<latitude>[+-]?\d{1,3}(?:[.,]\d+))
    \s*(?:[,;|/]|\s)\s*
    (?P<longitude>[+-]?\d{1,3}(?:[.,]\d+))
    (?:\s*(?:[,;|/]|\s)\s*[+-]?\d+(?:[.,]\d+)?)?
    \s*[\])]?\s*$
    """,
    re.VERBOSE,
)
COORDINATE_HEADER_MARKERS = ("координат", "геолокац", "latlon", "latlong")
COORDINATE_HEADER_NEGATIVE_MARKERS = ("id", "код", "номер")
COORDINATE_CONTENT_MIN_RATIO = 0.8
COORDINATE_CONTENT_MIN_ROW_COVERAGE = 0.5
AMBIGUOUS_COORDINATE_COLUMNS_MESSAGE = (
    "Неоднозначные координаты: найдено несколько подходящих колонок"
)


class AmbiguousCoordinateColumnsError(ValueError):
    pass


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
CONTEXT_DATE_ALIASES = [
    "Дата доставки",
    "Дата отгрузки",
    "Дата поставки",
]


def get_source_header_index(header):
    header_idx = {}
    for idx, col in enumerate(header):
        normalized = normalize_lookup_text(col)
        if normalized and normalized not in header_idx:
            header_idx[normalized] = idx
    return header_idx


def get_source_header_positions(header):
    positions = {}
    for idx, col in enumerate(header):
        normalized = normalize_lookup_text(col)
        if normalized:
            positions.setdefault(normalized, []).append(idx)
    return positions


def find_source_column(header_idx, aliases):
    for alias in aliases:
        key = normalize_lookup_text(alias)
        if key in header_idx:
            return header_idx[key]
    return None


def find_source_columns(header_positions, aliases):
    result = []
    for alias in aliases:
        key = normalize_lookup_text(alias)
        result.extend(header_positions.get(key, []))
    return sorted(set(result))


def find_context_column(preview_rows, header_row, aliases):
    for row in reversed(preview_rows[: max(header_row - 1, 0)]):
        idx = find_source_column(get_source_header_index(row), aliases)
        if idx is not None:
            return idx
    return None


def add_context_columns(columns, preview_rows, header_row):
    columns = dict(columns)
    if columns.get("date") is None:
        columns["date"] = find_context_column(preview_rows, header_row, CONTEXT_DATE_ALIASES)
    return columns


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
    header_positions = get_source_header_positions(header)
    columns = {}
    missing = []

    for key, aliases in NORMALIZER_REQUIRED_ALIASES.items():
        idx = find_source_column(header_idx, aliases)
        if idx is None:
            missing.append(aliases[0])
        columns[key] = idx

    for key, aliases in NORMALIZER_OPTIONAL_ALIASES.items():
        columns[key] = find_source_column(header_idx, aliases)

    columns["coords_candidates"] = find_source_columns(header_positions, NORMALIZER_OPTIONAL_ALIASES["coords"])
    if columns["coords"] is None and not columns["coords_candidates"]:
        semantic_candidates = [
            index
            for index, value in enumerate(header)
            if is_coordinate_semantic_header(value)
        ]
        if len(semantic_candidates) == 1:
            columns["coords"] = semantic_candidates[0]
            columns["coords_candidates"] = semantic_candidates
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


def parse_strict_coordinate_pair(value):
    match = STRICT_COORDINATE_PAIR_PATTERN.fullmatch(normalize_text(value))
    if not match:
        return ""
    try:
        latitude = float(match.group("latitude").replace(",", "."))
        longitude = float(match.group("longitude").replace(",", "."))
    except ValueError:
        return ""
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return ""
    return f"{match.group('latitude').replace(',', '.')}, {match.group('longitude').replace(',', '.')}"


def is_coordinate_semantic_header(value):
    header = normalize_lookup_text(value)
    if any(marker in header for marker in COORDINATE_HEADER_MARKERS):
        return True
    return "gps" in header and not any(
        marker in header for marker in COORDINATE_HEADER_NEGATIVE_MARKERS
    )


def is_real_data_row(row, columns):
    if not row_has_data(row) or is_summary_row(row, columns):
        return False
    return all(
        get_source_cell(row, columns.get(key))
        for key in ("client", "payment", "product", "quantity")
    )


def add_content_inferred_coordinate_column(columns, rows, header_row):
    columns = dict(columns)
    if columns.get("coords") is not None or columns.get("coords_candidates"):
        return columns

    data_rows = [
        row
        for row in rows[header_row:]
        if is_real_data_row(row, columns)
    ]
    if not data_rows:
        return columns

    assigned_columns = {
        index
        for key, index in columns.items()
        if key not in {"coords", "coords_candidates"} and isinstance(index, int)
    }
    max_columns = max((len(row) for row in data_rows), default=0)
    minimum_matches = 1 if len(data_rows) == 1 else 2
    high_confidence = []
    for index in range(max_columns):
        if index in assigned_columns:
            continue
        non_empty_values = [
            get_source_cell(row, index)
            for row in data_rows
            if get_source_cell(row, index)
        ]
        if not non_empty_values:
            continue
        match_count = sum(bool(parse_strict_coordinate_pair(value)) for value in non_empty_values)
        match_ratio = match_count / len(non_empty_values)
        row_coverage = match_count / len(data_rows)
        if (
            match_count >= minimum_matches
            and match_ratio >= COORDINATE_CONTENT_MIN_RATIO
            and row_coverage >= COORDINATE_CONTENT_MIN_ROW_COVERAGE
        ):
            high_confidence.append(index)

    if not high_confidence:
        return columns
    if len(high_confidence) != 1:
        raise AmbiguousCoordinateColumnsError(AMBIGUOUS_COORDINATE_COLUMNS_MESSAGE)

    selected = high_confidence[0]

    columns["coords"] = selected
    columns["coords_candidates"] = [selected]
    columns["coords_inferred_from_content"] = True
    return columns


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
        columns = add_context_columns(detected["columns"], preview_rows, detected["header_row"])
        inference_last_row = min(
            worksheet.max_row or detected["header_row"],
            detected["header_row"] + MAX_COORDINATE_INFERENCE_ROWS,
        )
        inference_rows = list(
            worksheet.iter_rows(min_row=1, max_row=inference_last_row, values_only=True)
        )
        columns = add_content_inferred_coordinate_column(
            columns,
            inference_rows,
            detected["header_row"],
        )

        default_date = extract_default_date_from_file_name(file_name)
        default_date_source = "file_name" if default_date else ""
        if not default_date:
            default_date = extract_default_date_from_context(preview_rows, detected["header_row"])
            default_date_source = "sheet_header" if default_date else ""

        candidate = {
            "sheet_name": sheet_name,
            "header_row": detected["header_row"],
            "first_data_row": detected["header_row"] + 1,
            "columns": columns,
            "default_date": default_date,
            "default_date_source": default_date_source,
            "score": detected["score"] + (5 if sheet_name == "Заявки" else 0),
        }
        if not best or candidate["score"] > best["score"]:
            best = candidate

    return best
