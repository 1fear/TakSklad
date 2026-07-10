from .redaction import redact_secrets
from .spreadsheet_safety import SpreadsheetSafetyError, normalize_spreadsheet_filename


def normalize_text(value):
    return str(value or "").strip()


def safe_telegram_spreadsheet_filename(value):
    candidate = normalize_text(value) or "telegram_import.xlsx"
    try:
        return normalize_spreadsheet_filename(candidate)
    except SpreadsheetSafetyError:
        return ""


def telegram_import_failure_message(file_name, reason):
    reason_text = redact_secrets(normalize_text(reason)) or "неизвестная ошибка"
    safe_file_name = safe_telegram_spreadsheet_filename(file_name) or "telegram_import.xlsx"
    return "\n".join([
        "Не удалось импортировать Excel-файл.",
        "",
        f"Файл: {safe_file_name}",
        f"Причина: {reason_text}",
        "",
        "Что сделать: исправьте файл и отправьте его заново. Если файл уже в очереди, проверьте Инциденты в web-панели.",
        "Заказы и заявки SkladBot не созданы.",
    ])


def telegram_import_unconfirmed_message(file_name, reason):
    reason_text = redact_secrets(normalize_text(reason)) or "backend не ответил вовремя"
    safe_file_name = safe_telegram_spreadsheet_filename(file_name) or "telegram_import.xlsx"
    return "\n".join([
        "Не удалось подтвердить импорт Excel-файла.",
        "",
        f"Файл: {safe_file_name}",
        f"Причина: {reason_text}",
        "",
        "Что сделать: проверьте web-панель и Последние импорты. До проверки не отправляйте файл повторно, потому что заказ мог уже создаться.",
    ])
