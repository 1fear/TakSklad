import copy
import json
import logging
import os
from datetime import datetime

from config import (
    CREDENTIALS_FILE,
    IMPORT_HISTORY_FILE,
    PENDING_PRINTS_FILE,
    PENDING_SAVES_FILE,
    PENDING_TELEGRAM_FILE,
    PRINT_SETTINGS_FILE,
    PRODUCT_CATALOG_FILE,
    TAKSKLAD_DATA_FILE,
    TELEGRAM_SETTINGS_FILE,
    TELEGRAM_STATE_FILE,
)


def load_json_file(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as json_file:
            data = json.load(json_file)
        return data if data is not None else default
    except Exception:
        logging.exception("Не удалось загрузить JSON-файл: %s", path)
        return default


def save_json_file(path, data):
    try:
        with open(path, "w", encoding="utf-8") as json_file:
            json.dump(data, json_file, ensure_ascii=False, indent=2)
        return True
    except Exception:
        logging.exception("Не удалось сохранить JSON-файл: %s", path)
        return False


APP_DATA_DEFAULTS = {
    "credentials": {},
    "telegram_settings": {},
    "pending_saves": [],
    "pending_prints": [],
    "pending_telegram": [],
    "telegram_state": {},
    "product_catalog": {},
    "import_history": [],
    "print_settings": {},
    "skladbot_settings": {},
    "daily_report_state": {},
}

LEGACY_JSON_SECTIONS = {
    "credentials": CREDENTIALS_FILE,
    "telegram_settings": TELEGRAM_SETTINGS_FILE,
    "pending_saves": PENDING_SAVES_FILE,
    "pending_prints": PENDING_PRINTS_FILE,
    "pending_telegram": PENDING_TELEGRAM_FILE,
    "telegram_state": TELEGRAM_STATE_FILE,
    "product_catalog": PRODUCT_CATALOG_FILE,
    "import_history": IMPORT_HISTORY_FILE,
    "print_settings": PRINT_SETTINGS_FILE,
}


def default_app_data():
    return copy.deepcopy(APP_DATA_DEFAULTS)


def load_app_data():
    data = load_json_file(TAKSKLAD_DATA_FILE, {})
    if not isinstance(data, dict):
        data = {}
    merged = default_app_data()
    for key, value in data.items():
        merged[key] = value
    return merged


def save_app_data(data):
    try:
        normalized = default_app_data()
        if isinstance(data, dict):
            normalized.update(data)
        normalized["_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        os.makedirs(os.path.dirname(TAKSKLAD_DATA_FILE), exist_ok=True)
        temp_path = TAKSKLAD_DATA_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as json_file:
            json.dump(normalized, json_file, ensure_ascii=False, indent=2)
        os.replace(temp_path, TAKSKLAD_DATA_FILE)
        return True
    except Exception:
        logging.exception("Не удалось сохранить общий файл данных: %s", TAKSKLAD_DATA_FILE)
        return False


def load_data_section(section, default=None):
    default = APP_DATA_DEFAULTS.get(section, default)
    value = load_app_data().get(section, default)
    return value if value is not None else default


def save_data_section(section, value):
    data = load_app_data()
    data[section] = value
    return save_app_data(data)


def should_migrate_section(current_value, default_value):
    return current_value in (None, "", [], {}) or current_value == default_value


def credentials_look_valid(credentials):
    return (
        isinstance(credentials, dict)
        and bool(credentials.get("client_email"))
        and bool(credentials.get("private_key"))
    )


def migrate_legacy_json_files_to_app_data():
    data = load_app_data()
    changed = False

    for section, path in LEGACY_JSON_SECTIONS.items():
        if not os.path.exists(path):
            continue
        legacy_value = load_json_file(path, None)
        if legacy_value is None:
            continue
        default_value = APP_DATA_DEFAULTS.get(section)
        if should_migrate_section(data.get(section), default_value):
            data[section] = legacy_value
            changed = True

    if changed or not os.path.exists(TAKSKLAD_DATA_FILE):
        save_app_data(data)
        logging.info("Данные JSON объединены в %s", TAKSKLAD_DATA_FILE)
    return data


def load_credentials_data():
    stored_credentials = load_data_section("credentials", {})
    if credentials_look_valid(stored_credentials):
        return stored_credentials

    file_credentials = load_json_file(CREDENTIALS_FILE, {})
    if credentials_look_valid(file_credentials):
        return file_credentials

    return file_credentials if isinstance(file_credentials, dict) else {}


def credentials_available():
    credentials = load_credentials_data()
    return credentials_look_valid(credentials)
