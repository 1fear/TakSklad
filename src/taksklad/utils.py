import hashlib
import json
import os
import re
from datetime import datetime

from .config import EXCEL_IMPORT_EXTENSIONS, KIZ_MAX_LENGTH, KIZ_MIN_LENGTH


def clean_date_value(date_value):
    if date_value is None:
        return None
    date_str = str(date_value).strip()
    date_str = re.sub(r'^[\'"]+|[\'"]+$', "", date_str)
    if " " in date_str:
        date_str = date_str.split()[0]
    return date_str


def parse_date_to_standard(date_value):
    cleaned = clean_date_value(date_value)
    if not cleaned:
        return None
    formats = ["%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%y", "%Y.%m.%d"]
    for fmt in formats:
        try:
            parsed = datetime.strptime(cleaned, fmt)
            return parsed.strftime("%d.%m.%Y")
        except ValueError:
            continue
    return cleaned


def normalize_text(value):
    return str(value or "").strip()


def clean_file_name(file_name, fallback="file"):
    text = normalize_text(file_name).replace("\\", "/")
    name = os.path.basename(text).strip()
    return name or fallback


def is_supported_excel_file_name(file_name):
    extension = os.path.splitext(clean_file_name(file_name))[1].lower()
    return extension in EXCEL_IMPORT_EXTENSIONS


def normalize_header_name(value):
    return normalize_text(value).replace("\ufeff", "")


def get_header_index(header):
    return {normalize_header_name(col): idx for idx, col in enumerate(header) if normalize_header_name(col)}


def get_header_indices(header, column_name):
    normalized_column = normalize_header_name(column_name)
    return [
        idx
        for idx, col in enumerate(header)
        if normalize_header_name(col) == normalized_column
    ]


def column_index_to_letter(index):
    index += 1
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def get_cell(row, idx):
    if idx is None or idx >= len(row):
        return ""
    return normalize_text(row[idx])


def split_codes(codes_str):
    if not codes_str:
        return []
    codes = []
    for line in str(codes_str).splitlines():
        code = normalize_kiz_code(line)
        if code:
            codes.append(code)
    return codes


def normalize_kiz_code(raw_code):
    return str(raw_code or "").strip(" \t\r\n")


def validate_kiz_code(raw_code, min_length=KIZ_MIN_LENGTH, max_length=KIZ_MAX_LENGTH):
    code = normalize_kiz_code(raw_code)
    if not code:
        return False, "Код пустой", code
    if not code.startswith("01"):
        return False, "КИЗ должен начинаться с 01", code
    if len(code) < min_length:
        return False, f"Код слишком короткий для КИЗа (минимум {min_length} символов)", code
    if len(code) > max_length:
        return False, f"Код слишком длинный для КИЗа (максимум {max_length} символов)", code
    if re.search(r"[а-яА-ЯёЁ]", code):
        return False, "Код содержит русские буквы! Используйте только латиницу", code
    if any(char in code for char in (" ", "\t", "\r", "\n", "\v", "\f")):
        return False, "Код содержит пробелы или переносы", code
    if not re.fullmatch(r"[\x1d\x21-\x7E]+", code):
        return False, "Код содержит недопустимые символы", code
    return True, "", code


def normalize_payment_type(value):
    payment = normalize_text(value).lower().replace("ё", "е")
    if "терминал" in payment:
        return "terminal"
    if "перечис" in payment or "безнал" in payment:
        return "transfer"
    return "unknown"


def parse_int_value(value):
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    value_str = normalize_text(value).replace(" ", "").replace(",", ".")
    if not value_str:
        return 0
    try:
        return int(float(value_str))
    except ValueError:
        return 0


def normalize_lookup_text(value):
    text = normalize_text(value).lower().replace("ё", "е")
    text = text.replace("\ufeff", "")
    text = re.sub(r"[*:]+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def file_sha1(path):
    sha1 = hashlib.sha1()
    with open(path, "rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            sha1.update(chunk)
    return sha1.hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_hash(payload):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def normalize_coordinates(value):
    text = normalize_text(value)
    if not text:
        return ""

    numbers = re.findall(r"-?\d+(?:[.,]\d+)?", text)
    if len(numbers) < 2:
        return ""

    try:
        first = float(numbers[0].replace(",", "."))
        second = float(numbers[1].replace(",", "."))
    except ValueError:
        return ""

    if abs(first) <= 90 and abs(second) <= 180:
        lat, lon = first, second
    elif abs(second) <= 90 and abs(first) <= 180:
        lat, lon = second, first
    else:
        return ""

    return f"{lat:.8f},{lon:.8f}".rstrip("0").rstrip(".")
