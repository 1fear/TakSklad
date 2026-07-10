import copy
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
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
    RUNTIME_CONFIG_FILE,
    TAKSKLAD_DATA_FILE,
    TELEGRAM_SETTINGS_FILE,
    TELEGRAM_STATE_FILE,
    YANDEX_GEOCODER_KEY_FILE,
)
from .secret_store import (
    BACKEND_API_TOKEN_SECRET,
    GEOCODER_API_KEY_SECRET,
    GOOGLE_CREDENTIALS_SECRET,
    TELEGRAM_BOT_TOKEN_SECRET,
    SecretStoreError,
    get_secret_store,
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
LAST_SECRET_MIGRATION_STATUS = {
    "status": "not_run",
    "migrated": 0,
    "restart_required": False,
    "error_class": "",
}

LEGACY_JSON_SECTIONS = {
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

APP_DATA_LOCK = threading.RLock()
QUEUE_DB_FILENAME = "TakSklad_queues.sqlite3"
QUEUE_BUSY_TIMEOUT_MS = 10000


def _storage_fault_hook(stage):
    return None


def queue_db_path():
    return os.path.join(os.path.dirname(TAKSKLAD_DATA_FILE), QUEUE_DB_FILENAME)


def _queue_item_id(item):
    if isinstance(item, dict) and str(item.get("id") or "").strip():
        return str(item["id"]).strip()
    raw = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _open_queue_db():
    path = queue_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    connection = sqlite3.connect(path, timeout=QUEUE_BUSY_TIMEOUT_MS / 1000)
    connection.execute(f"PRAGMA busy_timeout = {QUEUE_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS desktop_queue_items (
            section TEXT NOT NULL,
            event_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (section, event_id)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS desktop_queue_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_desktop_queue_section_position "
        "ON desktop_queue_items(section, position, event_id)"
    )
    _restrict_queue_db_permissions(path)
    return connection


def _load_queue_section_unlocked(section, connection=None):
    owns_connection = connection is None
    connection = connection or _open_queue_db()
    try:
        rows = connection.execute(
            "SELECT payload_json FROM desktop_queue_items "
            "WHERE section = ? ORDER BY position, event_id",
            (section,),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]
    finally:
        if owns_connection:
            connection.close()


def _replace_queue_section_unlocked(section, items, connection=None):
    owns_connection = connection is None
    connection = connection or _open_queue_db()
    try:
        if owns_connection:
            connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM desktop_queue_items WHERE section = ?", (section,))
        for position, item in enumerate(items or []):
            event_id = _queue_item_id(item)
            if not (isinstance(item, dict) and str(item.get("id") or "").strip()):
                event_id = f"legacy-{position:08d}-{event_id}"
            connection.execute(
                "INSERT OR IGNORE INTO desktop_queue_items "
                "(section, event_id, position, payload_json) VALUES (?, ?, ?, ?)",
                (
                    section,
                    event_id,
                    position,
                    json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
                ),
            )
        if owns_connection:
            connection.commit()
    except Exception:
        if owns_connection:
            connection.rollback()
        raise
    finally:
        if owns_connection:
            connection.close()


def load_queue_section(section):
    if section not in APP_DATA_QUEUE_SECTIONS:
        raise ValueError(f"not a queue section: {section}")
    with APP_DATA_LOCK:
        return _load_queue_section_unlocked(section)


def replace_queue_section(section, items):
    if section not in APP_DATA_QUEUE_SECTIONS:
        raise ValueError(f"not a queue section: {section}")
    with APP_DATA_LOCK:
        _replace_queue_section_unlocked(section, list(items or []))
    return True


def mutate_queue_section(section, mutator):
    if section not in APP_DATA_QUEUE_SECTIONS:
        raise ValueError(f"not a queue section: {section}")
    with APP_DATA_LOCK:
        connection = _open_queue_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = _load_queue_section_unlocked(section, connection=connection)
            working = copy.deepcopy(current)
            updated = mutator(working)
            if updated is None:
                updated = working
            _replace_queue_section_unlocked(section, list(updated), connection=connection)
            connection.commit()
            return copy.deepcopy(list(updated))
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def append_queue_item(section, item, *, update_existing=None):
    if section not in APP_DATA_QUEUE_SECTIONS:
        raise ValueError(f"not a queue section: {section}")
    event_id = _queue_item_id(item)
    with APP_DATA_LOCK:
        connection = _open_queue_db()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing_row = connection.execute(
                "SELECT payload_json FROM desktop_queue_items WHERE section = ? AND event_id = ?",
                (section, event_id),
            ).fetchone()
            if existing_row is not None:
                if update_existing is not None:
                    existing = json.loads(existing_row[0])
                    updated = update_existing(copy.deepcopy(existing))
                    if updated is not None:
                        connection.execute(
                            "UPDATE desktop_queue_items SET payload_json = ?, updated_at = CURRENT_TIMESTAMP "
                            "WHERE section = ? AND event_id = ?",
                            (json.dumps(updated, ensure_ascii=False, sort_keys=True, default=str), section, event_id),
                        )
                connection.commit()
                return False
            position = connection.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM desktop_queue_items WHERE section = ?",
                (section,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO desktop_queue_items (section, event_id, position, payload_json) VALUES (?, ?, ?, ?)",
                (section, event_id, int(position), json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def reconcile_queue_section(section, snapshot, remaining):
    snapshot_ids = {_queue_item_id(item) for item in snapshot or []}
    remaining_by_id = {_queue_item_id(item): item for item in remaining or []}

    def reconcile(current):
        result = []
        seen = set()
        for item in current:
            event_id = _queue_item_id(item)
            if event_id in snapshot_ids:
                replacement = remaining_by_id.get(event_id)
                if replacement is not None:
                    result.append(replacement)
                    seen.add(event_id)
                continue
            result.append(item)
            seen.add(event_id)
        for event_id, item in remaining_by_id.items():
            if event_id not in seen:
                result.append(item)
        return result

    return mutate_queue_section(section, reconcile)


def _hydrate_queue_sections_unlocked(data):
    connection = _open_queue_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        for section in APP_DATA_QUEUE_SECTIONS:
            marker = f"json_migrated:{section}"
            migrated = connection.execute(
                "SELECT value FROM desktop_queue_metadata WHERE key = ?", (marker,)
            ).fetchone()
            if migrated is None:
                legacy_items = data.get(section)
                if isinstance(legacy_items, list) and legacy_items:
                    _replace_queue_section_unlocked(section, legacy_items, connection=connection)
                connection.execute(
                    "INSERT OR REPLACE INTO desktop_queue_metadata (key, value) VALUES (?, '1')",
                    (marker,),
                )
            data[section] = _load_queue_section_unlocked(section, connection=connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return data


def default_app_data():
    return copy.deepcopy(APP_DATA_DEFAULTS)


def sanitize_app_data_secrets(data):
    sanitized = copy.deepcopy(data) if isinstance(data, dict) else {}
    sanitized["credentials"] = {}
    telegram_settings = sanitized.get("telegram_settings")
    if isinstance(telegram_settings, dict):
        telegram_settings = dict(telegram_settings)
        telegram_settings.pop("bot_token", None)
        telegram_settings.pop("token", None)
        sanitized["telegram_settings"] = telegram_settings
    for key in (
        "backend_api_token",
        "TAKSKLAD_BACKEND_API_TOKEN",
        "TAKSKLAD_API_TOKEN",
        "yandex_geocoder_api_key",
        "YANDEX_GEOCODER_API_KEY",
    ):
        sanitized.pop(key, None)
    return sanitized


def app_data_backup_path(index=1):
    return f"{TAKSKLAD_DATA_FILE}.last_good.{index}.bak"


def _restrict_local_data_permissions(path):
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        logging.warning("Не удалось ограничить права локального файла данных: %s", path)


def _restrict_queue_db_permissions(path=None):
    path = path or queue_db_path()
    for candidate in (path, f"{path}-wal", f"{path}-shm"):
        if os.path.exists(candidate):
            _restrict_local_data_permissions(candidate)


def _flush_and_fsync(file_obj):
    file_obj.flush()
    os.fsync(file_obj.fileno())


def _fsync_directory(path):
    if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
    data = sanitize_app_data_secrets(data)
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
            _flush_and_fsync(json_file)
        os.replace(temp_path, TAKSKLAD_DATA_FILE)
        _fsync_directory(data_dir)
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
                _fsync_directory(data_dir)
            except Exception:
                logging.exception("Не удалось повернуть last-good backup: %s", newer)

    backup_path = app_data_backup_path(1)
    try:
        fd, temp_path = tempfile.mkstemp(
            prefix=os.path.basename(backup_path) + ".",
            suffix=".tmp",
            dir=data_dir,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as backup_file:
                json.dump(sanitize_app_data_secrets(current_data), backup_file, ensure_ascii=False, indent=2)
                _flush_and_fsync(backup_file)
            os.replace(temp_path, backup_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        _restrict_local_data_permissions(backup_path)
        _fsync_directory(data_dir)
    except Exception:
        logging.exception("Не удалось создать last-good backup: %s", backup_path)
        return

    logging.info(
        "Создан last-good backup локального файла данных: pending_counts %s",
        format_app_data_queue_counts(app_data_queue_counts(current_data)),
    )


def _load_json_state_unlocked():
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
    data = sanitize_app_data_secrets(data)
    merged = default_app_data()
    for key, value in data.items():
        merged[key] = value
    return merged


def _load_app_data_unlocked():
    merged = _load_json_state_unlocked()
    merged = _hydrate_queue_sections_unlocked(merged)
    _set_app_data_recovery_status(
        LAST_APP_DATA_RECOVERY_STATUS.get("status") or "ok",
        source=LAST_APP_DATA_RECOVERY_STATUS.get("source") or TAKSKLAD_DATA_FILE,
        restored_from=LAST_APP_DATA_RECOVERY_STATUS.get("restored_from") or "",
        queue_counts=app_data_queue_counts(merged),
    )
    return merged


def load_app_data():
    with APP_DATA_LOCK:
        return _load_app_data_unlocked()


def _save_app_data_unlocked(data, *, persist_queues=True):
    temp_path = None
    try:
        if persist_queues and isinstance(data, dict):
            for section in APP_DATA_QUEUE_SECTIONS:
                if section in data and isinstance(data.get(section), list):
                    _replace_queue_section_unlocked(section, data[section])
        normalized = default_app_data()
        if isinstance(data, dict):
            normalized.update(sanitize_app_data_secrets(data))
        for section in APP_DATA_QUEUE_SECTIONS:
            normalized[section] = []
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
            _flush_and_fsync(json_file)

        last_error = None
        for attempt in range(1, SAVE_RETRY_ATTEMPTS + 1):
            try:
                _storage_fault_hook("before_replace")
                os.replace(temp_path, TAKSKLAD_DATA_FILE)
                _restrict_local_data_permissions(TAKSKLAD_DATA_FILE)
                _fsync_directory(data_dir)
                _storage_fault_hook("after_replace")
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


def save_app_data(data):
    with APP_DATA_LOCK:
        return _save_app_data_unlocked(data)


def load_data_section(section, default=None):
    default = APP_DATA_DEFAULTS.get(section, default)
    if section in APP_DATA_QUEUE_SECTIONS:
        value = load_queue_section(section)
    else:
        with APP_DATA_LOCK:
            value = _load_json_state_unlocked().get(section, default)
    return value if value is not None else default


def save_data_section(section, value):
    if section in APP_DATA_QUEUE_SECTIONS:
        return replace_queue_section(section, value)
    with APP_DATA_LOCK:
        data = _load_json_state_unlocked()
        data[section] = value
        return _save_app_data_unlocked(data, persist_queues=False)


def mutate_data_section(section, mutator, default=None):
    if section in APP_DATA_QUEUE_SECTIONS:
        return mutate_queue_section(section, mutator)
    with APP_DATA_LOCK:
        data = _load_json_state_unlocked()
        current = copy.deepcopy(data.get(section, APP_DATA_DEFAULTS.get(section, default)))
        updated = mutator(current)
        if updated is None:
            updated = current
        data[section] = updated
        if not _save_app_data_unlocked(data, persist_queues=False):
            raise OSError(f"failed to persist section: {section}")
        return copy.deepcopy(updated)


def should_migrate_section(current_value, default_value):
    return current_value in (None, "", [], {}) or current_value == default_value


def credentials_look_valid(credentials):
    return (
        isinstance(credentials, dict)
        and bool(credentials.get("client_email"))
        and bool(credentials.get("private_key"))
    )


class SecretMigrationError(RuntimeError):
    pass


def get_secret_migration_status():
    return copy.deepcopy(LAST_SECRET_MIGRATION_STATUS)


def _set_secret_migration_status(status, *, migrated=0, restart_required=False, error_class=""):
    LAST_SECRET_MIGRATION_STATUS.update({
        "status": status,
        "migrated": int(migrated or 0),
        "restart_required": bool(restart_required),
        "error_class": str(error_class or ""),
    })


def _atomic_write_bytes(path, content):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=os.path.basename(path) + ".",
        suffix=".tmp",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            _flush_and_fsync(output)
        os.replace(temp_path, path)
        _restrict_local_data_permissions(path)
        _fsync_directory(directory)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def _atomic_write_json(path, value):
    content = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
    _atomic_write_bytes(path, content)


def _optional_file_bytes(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, "rb") as source:
        return source.read()


def _json_from_bytes(content, path):
    if content is None:
        return {}
    try:
        value = json.loads(content.decode("utf-8"))
    except Exception as exc:
        raise SecretMigrationError(f"invalid JSON source class={os.path.basename(path)}") from exc
    if not isinstance(value, dict):
        raise SecretMigrationError(f"invalid JSON object class={os.path.basename(path)}")
    return value


def _restore_file_snapshots(snapshots):
    restore_errors = []
    for path, content in snapshots.items():
        try:
            if content is None:
                if os.path.exists(path):
                    os.remove(path)
                continue
            _atomic_write_bytes(path, content)
        except Exception as exc:
            restore_errors.append(exc)
    if restore_errors:
        raise SecretMigrationError("one or more plaintext source restores failed") from restore_errors[0]


def _credential_candidate(value, source_class):
    if value in (None, {}):
        return None
    if not isinstance(value, dict) or not credentials_look_valid(value):
        raise SecretMigrationError(f"invalid Google credential source class={source_class}")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _settings_token_candidate(value, source_class):
    if value in (None, {}):
        return None
    if not isinstance(value, dict):
        raise SecretMigrationError(f"invalid Telegram settings source class={source_class}")
    token = value.get("bot_token") or value.get("token") or ""
    return str(token).strip() or None


def _mapping_token_candidate(value, keys, source_class):
    if value in (None, {}):
        return None
    if not isinstance(value, dict):
        raise SecretMigrationError(f"invalid token source class={source_class}")
    for key in keys:
        token = value.get(key)
        if token is not None and str(token).strip():
            return str(token).strip()
    return None


def _first_candidate(values):
    return next((value for value in values if value), None)


def migrate_desktop_secrets(secret_store=None, *, allow_volatile_test_store=False):
    """Move legacy desktop secrets only after a verified secure-store round trip."""
    with APP_DATA_LOCK:
        store = None
        snapshots = {}
        secure_snapshots = {}
        snapshots_complete = False
        mutation_started = False
        candidates = {}
        try:
            store = secret_store or get_secret_store()
            backup_paths = [app_data_backup_path(index) for index in range(1, APP_DATA_BACKUP_LIMIT + 1)]
            source_paths = [
                TAKSKLAD_DATA_FILE,
                *backup_paths,
                CREDENTIALS_FILE,
                TELEGRAM_SETTINGS_FILE,
                RUNTIME_CONFIG_FILE,
                YANDEX_GEOCODER_KEY_FILE,
            ]
            snapshots = {path: _optional_file_bytes(path) for path in source_paths}
            snapshots_complete = True
            state = _json_from_bytes(snapshots[TAKSKLAD_DATA_FILE], TAKSKLAD_DATA_FILE)
            backup_states = [
                _json_from_bytes(snapshots[path], path)
                for path in backup_paths
            ]
            credentials_file = _json_from_bytes(snapshots[CREDENTIALS_FILE], CREDENTIALS_FILE)
            telegram_file = _json_from_bytes(snapshots[TELEGRAM_SETTINGS_FILE], TELEGRAM_SETTINGS_FILE)
            runtime_config = _json_from_bytes(snapshots[RUNTIME_CONFIG_FILE], RUNTIME_CONFIG_FILE)

            google_values = [
                _credential_candidate(state.get("credentials"), "current_state"),
                _credential_candidate(credentials_file, "credentials_file"),
            ]
            telegram_values = [
                _settings_token_candidate(state.get("telegram_settings"), "current_state"),
                _settings_token_candidate(telegram_file, "telegram_file"),
            ]
            backend_values = [
                _mapping_token_candidate(
                    state,
                    ("backend_api_token", "TAKSKLAD_BACKEND_API_TOKEN", "TAKSKLAD_API_TOKEN"),
                    "current_state",
                ),
                _mapping_token_candidate(
                    runtime_config,
                    ("TAKSKLAD_BACKEND_API_TOKEN", "TAKSKLAD_API_TOKEN"),
                    "runtime_config",
                ),
            ]
            geocoder_values = [
                _mapping_token_candidate(
                    state,
                    ("yandex_geocoder_api_key", "YANDEX_GEOCODER_API_KEY"),
                    "current_state",
                )
            ]
            for index, backup_state in enumerate(backup_states, start=1):
                source_class = f"backup_{index}"
                google_values.append(_credential_candidate(backup_state.get("credentials"), source_class))
                telegram_values.append(
                    _settings_token_candidate(backup_state.get("telegram_settings"), source_class)
                )
                backend_values.append(
                    _mapping_token_candidate(
                        backup_state,
                        ("backend_api_token", "TAKSKLAD_BACKEND_API_TOKEN", "TAKSKLAD_API_TOKEN"),
                        source_class,
                    )
                )
                geocoder_values.append(
                    _mapping_token_candidate(
                        backup_state,
                        ("yandex_geocoder_api_key", "YANDEX_GEOCODER_API_KEY"),
                        source_class,
                    )
                )

            geocoder_bytes = snapshots[YANDEX_GEOCODER_KEY_FILE]
            if geocoder_bytes is not None:
                try:
                    geocoder_values.insert(0, geocoder_bytes.decode("utf-8").strip() or None)
                except UnicodeDecodeError as exc:
                    raise SecretMigrationError("invalid geocoder source class=geocoder_file") from exc

            selected = {
                GOOGLE_CREDENTIALS_SECRET: _first_candidate(google_values),
                TELEGRAM_BOT_TOKEN_SECRET: _first_candidate(telegram_values),
                BACKEND_API_TOKEN_SECRET: _first_candidate(backend_values),
                GEOCODER_API_KEY_SECRET: _first_candidate(geocoder_values),
            }
            candidates = {name: value for name, value in selected.items() if value}
            restart_required = BACKEND_API_TOKEN_SECRET in candidates

            store_status = store.status()
            if (
                not allow_volatile_test_store
                and isinstance(store_status, dict)
                and store_status.get("provider") == "windows_dpapi"
                and (
                    not store_status.get("available")
                    or not store_status.get("persistent")
                    or store_status.get("state") != "ok"
                )
            ):
                raise SecretMigrationError("production Windows DPAPI store is unavailable")
            if candidates and not allow_volatile_test_store:
                if (
                    not isinstance(store_status, dict)
                    or not store_status.get("available")
                    or not store_status.get("persistent")
                    or store_status.get("provider") != "windows_dpapi"
                ):
                    raise SecretMigrationError("secret migration requires persistent Windows DPAPI store")

            secure_snapshots = {name: store.get_text(name) for name in candidates}
            for name, value in candidates.items():
                mutation_started = True
                store.set_text(name, value)
                if store.get_text(name) != value:
                    raise SecretMigrationError(f"secure round trip failed key={name}")
                _storage_fault_hook(f"after_secret_roundtrip:{name}")
            _storage_fault_hook("after_secret_roundtrip")

            sanitized_state = sanitize_app_data_secrets(state)
            legacy_telegram_nonsecret = sanitize_app_data_secrets({"telegram_settings": telegram_file}).get(
                "telegram_settings", {}
            )
            if legacy_telegram_nonsecret:
                current_telegram = sanitized_state.get("telegram_settings")
                if not isinstance(current_telegram, dict):
                    current_telegram = {}
                sanitized_state["telegram_settings"] = {**legacy_telegram_nonsecret, **current_telegram}
            if snapshots[TAKSKLAD_DATA_FILE] is not None or legacy_telegram_nonsecret:
                mutation_started = True
                _atomic_write_json(TAKSKLAD_DATA_FILE, sanitized_state)
            _storage_fault_hook("after_state_sanitize")

            for path in backup_paths:
                content = snapshots[path]
                if content is None:
                    continue
                mutation_started = True
                _atomic_write_json(path, sanitize_app_data_secrets(_json_from_bytes(content, path)))
            _storage_fault_hook("after_backup_sanitize")

            runtime_sanitized = dict(runtime_config)
            runtime_sanitized.pop("TAKSKLAD_BACKEND_API_TOKEN", None)
            runtime_sanitized.pop("TAKSKLAD_API_TOKEN", None)
            if snapshots[RUNTIME_CONFIG_FILE] is not None:
                mutation_started = True
                _atomic_write_json(RUNTIME_CONFIG_FILE, runtime_sanitized)
            _storage_fault_hook("after_runtime_sanitize")

            for path in (CREDENTIALS_FILE, TELEGRAM_SETTINGS_FILE, YANDEX_GEOCODER_KEY_FILE):
                if os.path.exists(path):
                    mutation_started = True
                    os.remove(path)
            _storage_fault_hook("after_plaintext_purge")
        except Exception as exc:
            restore_errors = []
            if mutation_started and store is not None:
                for name, previous_value in secure_snapshots.items():
                    try:
                        if previous_value is not None:
                            store.set_text(name, previous_value)
                        else:
                            store.delete(name)
                    except Exception as restore_exc:
                        restore_errors.append(restore_exc)
            if mutation_started and snapshots_complete:
                try:
                    _restore_file_snapshots(snapshots)
                except Exception as restore_exc:
                    restore_errors.append(restore_exc)
            if restore_errors:
                restore_exc = restore_errors[0]
                _set_secret_migration_status(
                    "migration_failed_restore_failed",
                    migrated=0,
                    error_class=type(restore_exc).__name__,
                )
                raise SecretMigrationError("secret migration rollback failed") from restore_exc
            _set_secret_migration_status("migration_failed", migrated=0, error_class=type(exc).__name__)
            raise SecretMigrationError("secret migration failed closed") from exc

        status = "migrated_restart_required" if restart_required else "migrated" if candidates else "clean"
        _set_secret_migration_status(
            status,
            migrated=len(candidates),
            restart_required=restart_required,
        )
        return get_secret_migration_status()


def migrate_legacy_json_files_to_app_data():
    with APP_DATA_LOCK:
        data = _load_app_data_unlocked()
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
            if not _save_app_data_unlocked(data):
                raise OSError(f"failed to migrate JSON data into {TAKSKLAD_DATA_FILE}")
            logging.info("Данные JSON объединены в %s", TAKSKLAD_DATA_FILE)
        return data


def load_credentials_data():
    try:
        serialized = get_secret_store().get_text(GOOGLE_CREDENTIALS_SECRET)
    except SecretStoreError:
        return {}
    if not serialized:
        return {}
    try:
        credentials = json.loads(serialized)
    except (TypeError, ValueError):
        return {}
    return credentials if credentials_look_valid(credentials) else {}


def credentials_available():
    credentials = load_credentials_data()
    return credentials_look_valid(credentials)
