import copy
import json
import logging
import os
import shutil
import tempfile
import time
from datetime import datetime

from .config import (
    CREDENTIALS_FILE,
    IMPORT_HISTORY_FILE,
    PENDING_PRINTS_FILE,
    PENDING_SAVES_FILE,
    PENDING_BACKEND_EVENTS_FILE,
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
    "pending_backend_events": [],
    "telegram_state": {},
    "product_catalog": {},
    "import_history": [],
    "print_settings": {},
    "skladbot_settings": {},
    "daily_report_state": {},
}

SAVE_RETRY_ATTEMPTS = 8
SAVE_RETRY_DELAY_SECONDS = 0.2
APP_DATA_BACKUP_LIMIT = 3
APP_DATA_QUEUE_SECTIONS = (
    "pending_saves",
    "pending_prints",
    "pending_telegram",
    "pending_backend_events",
)
LAST_APP_DATA_RECOVERY_STATUS = {
    "status": "unknown",
    "source": "",
    "restored_from": "",
    "queue_counts": {},
}

LEGACY_JSON_SECTIONS = {
    "credentials": CREDENTIALS_FILE,
    "telegram_settings": TELEGRAM_SETTINGS_FILE,
    "pending_saves": PENDING_SAVES_FILE,
    "pending_prints": PENDING_PRINTS_FILE,
    "pending_telegram": PENDING_TELEGRAM_FILE,
    "pending_backend_events": PENDING_BACKEND_EVENTS_FILE,
    "telegram_state": TELEGRAM_STATE_FILE,
    "product_catalog": PRODUCT_CATALOG_FILE,
    "import_history": IMPORT_HISTORY_FILE,
    "print_settings": PRINT_SETTINGS_FILE,
}


def default_app_data():
    return copy.deepcopy(APP_DATA_DEFAULTS)


def app_data_backup_path(index=1):
    return f"{TAKSKLAD_DATA_FILE}.last_good.{index}.bak"


def _restrict_local_data_permissions(path):
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        logging.warning("Не удалось ограничить права локального файла данных: %s", path)


def app_data_queue_counts(data):
    data = data if isinstance(data, dict) else {}
    return {
        section: len(data.get(section)) if isinstance(data.get(section), list) else 0
        for section in APP_DATA_QUEUE_SECTIONS
    }


def format_app_data_queue_counts(counts):
    counts = counts if isinstance(counts, dict) else {}
    return " ".join(f"{section}={int(counts.get(section) or 0)}" for section in APP_DATA_QUEUE_SECTIONS)


def _set_app_data_recovery_status(status, source="", restored_from="", queue_counts=None):
    LAST_APP_DATA_RECOVERY_STATUS.update({
        "status": status,
        "source": source,
        "restored_from": restored_from,
        "queue_counts": dict(queue_counts or {}),
    })


def get_app_data_recovery_status():
    return copy.deepcopy(LAST_APP_DATA_RECOVERY_STATUS)


def _read_json_dict(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as json_file:
        data = json.load(json_file)
    if not isinstance(data, dict):
        raise ValueError("JSON root is not an object")
    return data


def _latest_valid_app_data_backup():
    for index in range(1, APP_DATA_BACKUP_LIMIT + 1):
        path = app_data_backup_path(index)
        try:
            data = _read_json_dict(path)
        except Exception as exc:
            logging.warning("Last-good backup is invalid: %s error=%s", path, type(exc).__name__)
            continue
        if isinstance(data, dict):
            return path, data
    return "", None


def _restore_app_data_from_backup(backup_path, data, before_counts=None):
    data_dir = os.path.dirname(TAKSKLAD_DATA_FILE)
    os.makedirs(data_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=os.path.basename(TAKSKLAD_DATA_FILE) + ".restore.",
        suffix=".tmp",
        dir=data_dir,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as json_file:
            json.dump(data, json_file, ensure_ascii=False, indent=2)
        os.replace(temp_path, TAKSKLAD_DATA_FILE)
        _restrict_local_data_permissions(TAKSKLAD_DATA_FILE)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

    counts = app_data_queue_counts(data)
    _set_app_data_recovery_status(
        "restored",
        source=TAKSKLAD_DATA_FILE,
        restored_from=backup_path,
        queue_counts=counts,
    )
    logging.warning(
        "Локальный файл данных восстановлен из last-good backup: before_restore %s after_restore %s",
        format_app_data_queue_counts(before_counts or {}),
        format_app_data_queue_counts(counts),
    )
    return data


def _fallback_to_last_good_backup(reason, before_counts=None):
    backup_path, backup_data = _latest_valid_app_data_backup()
    if isinstance(backup_data, dict):
        return _restore_app_data_from_backup(backup_path, backup_data, before_counts=before_counts)

    data = {}
    status = "missing" if reason == "missing" else "degraded"
    _set_app_data_recovery_status(status, source=TAKSKLAD_DATA_FILE, queue_counts=app_data_queue_counts(data))
    if reason == "missing":
        logging.warning(
            "Локальный файл данных отсутствует, valid last-good backup не найден: pending_counts %s",
            format_app_data_queue_counts(app_data_queue_counts(data)),
        )
    else:
        logging.error(
            "Локальный файл данных поврежден, valid last-good backup не найден: pending_counts %s",
            format_app_data_queue_counts(app_data_queue_counts(data)),
        )
    return data


def _backup_current_app_data_if_valid():
    try:
        current_data = _read_json_dict(TAKSKLAD_DATA_FILE)
    except FileNotFoundError:
        return
    except Exception:
        logging.warning("Last-good backup skipped: current app data is invalid")
        return
    if not isinstance(current_data, dict):
        return

    data_dir = os.path.dirname(TAKSKLAD_DATA_FILE)
    os.makedirs(data_dir, exist_ok=True)
    for index in range(APP_DATA_BACKUP_LIMIT, 1, -1):
        older = app_data_backup_path(index - 1)
        newer = app_data_backup_path(index)
        if os.path.exists(older):
            try:
                os.replace(older, newer)
            except Exception:
                logging.exception("Не удалось повернуть last-good backup: %s", newer)

    backup_path = app_data_backup_path(1)
    try:
        shutil.copy2(TAKSKLAD_DATA_FILE, backup_path)
        _restrict_local_data_permissions(backup_path)
    except Exception:
        logging.exception("Не удалось создать last-good backup: %s", backup_path)
        return

    logging.info(
        "Создан last-good backup локального файла данных: pending_counts %s",
        format_app_data_queue_counts(app_data_queue_counts(current_data)),
    )


def load_app_data():
    try:
        data = _read_json_dict(TAKSKLAD_DATA_FILE)
        if data is None:
            data = _fallback_to_last_good_backup("missing", before_counts=app_data_queue_counts({}))
        else:
            _set_app_data_recovery_status("ok", source=TAKSKLAD_DATA_FILE, queue_counts=app_data_queue_counts(data))
    except FileNotFoundError:
        data = _fallback_to_last_good_backup("missing", before_counts=app_data_queue_counts({}))
    except Exception as exc:
        logging.error("Не удалось загрузить общий файл данных: %s error=%s", TAKSKLAD_DATA_FILE, type(exc).__name__)
        data = _fallback_to_last_good_backup("degraded", before_counts=app_data_queue_counts({}))
    merged = default_app_data()
    for key, value in data.items():
        merged[key] = value
    return merged


def save_app_data(data):
    temp_path = None
    try:
        normalized = default_app_data()
        if isinstance(data, dict):
            normalized.update(data)
        normalized["_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        data_dir = os.path.dirname(TAKSKLAD_DATA_FILE)
        os.makedirs(data_dir, exist_ok=True)
        _backup_current_app_data_if_valid()
        fd, temp_path = tempfile.mkstemp(
            prefix=os.path.basename(TAKSKLAD_DATA_FILE) + ".",
            suffix=".tmp",
            dir=data_dir,
            text=True,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as json_file:
            json.dump(normalized, json_file, ensure_ascii=False, indent=2)

        last_error = None
        for attempt in range(1, SAVE_RETRY_ATTEMPTS + 1):
            try:
                os.replace(temp_path, TAKSKLAD_DATA_FILE)
                _restrict_local_data_permissions(TAKSKLAD_DATA_FILE)
                return True
            except PermissionError as exc:
                last_error = exc
                if attempt >= SAVE_RETRY_ATTEMPTS:
                    break
                logging.warning(
                    "Общий файл данных временно занят, повтор сохранения %s/%s",
                    attempt,
                    SAVE_RETRY_ATTEMPTS,
                )
                time.sleep(SAVE_RETRY_DELAY_SECONDS)
        if last_error:
            raise last_error
        return True
    except Exception:
        logging.exception("Не удалось сохранить общий файл данных: %s", TAKSKLAD_DATA_FILE)
        return False
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


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
