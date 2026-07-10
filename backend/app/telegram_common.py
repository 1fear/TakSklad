import re
from datetime import datetime


DATE_PATTERN = re.compile(r"(?<!\d)(\d{1,2})[._/-](\d{1,2})[._/-](\d{2,4})(?!\d)")


def normalize_text(value):
    return str(value or "").strip()


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
