import hashlib
import logging
import os
import sys
from urllib.parse import urlparse

from . import storage
from .config import (
    APP_DIR,
    APP_BUILD_LABEL,
    APP_VERSION,
    LOG_FILE,
    SHEET_NAME,
    SPREADSHEET_ID,
    TAKSKLAD_BACKEND_API_TOKEN,
    TAKSKLAD_BACKEND_BASE_URL,
    TAKSKLAD_BACKEND_ENABLED,
    TAKSKLAD_BACKEND_READ_ORDERS_ENABLED,
    UPDATE_INFO_URL,
)
from .geocoding import load_yandex_geocoder_key
from .telegram_service import get_telegram_chat_ids
from .utils import normalize_text


def safe_hash(value, length=10):
    text = normalize_text(value)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def credentials_status(app_data=None):
    app_data = app_data if isinstance(app_data, dict) else storage.load_app_data()
    stored = app_data.get("credentials")
    if storage.credentials_look_valid(stored):
        return "stored"

    file_credentials = storage.load_json_file(storage.CREDENTIALS_FILE, {})
    if storage.credentials_look_valid(file_credentials):
        return "file"
    return "missing"


def url_origin(value):
    text = normalize_text(value)
    if not text:
        return ""
    parsed = urlparse(text)
    if not parsed.netloc:
        return text
    return f"{parsed.scheme}://{parsed.netloc}"


def bool_text(value):
    return "yes" if value else "no"


def format_app_version_label():
    label = normalize_text(APP_BUILD_LABEL)
    if label:
        return f"Версия: {APP_VERSION} · {label}"
    return f"Версия: {APP_VERSION}"


def build_startup_self_check():
    app_data = storage.load_app_data()
    telegram_settings = app_data.get("telegram_settings") if isinstance(app_data.get("telegram_settings"), dict) else {}
    telegram_enabled = bool(telegram_settings.get("enabled"))
    telegram_token_configured = bool(normalize_text(telegram_settings.get("bot_token")))
    chat_ids_count = len(get_telegram_chat_ids(telegram_settings))

    pending_saves = app_data.get("pending_saves")
    pending_prints = app_data.get("pending_prints")
    pending_backend_events = app_data.get("pending_backend_events")
    pending_telegram = app_data.get("pending_telegram")

    return {
        "version": APP_VERSION,
        "build_label": APP_BUILD_LABEL,
        "frozen": bool_text(getattr(sys, "frozen", False)),
        "app_dir": APP_DIR,
        "log_file": LOG_FILE,
        "spreadsheet_hash": safe_hash(SPREADSHEET_ID),
        "sheet": SHEET_NAME,
        "update_origin": url_origin(UPDATE_INFO_URL),
        "app_data": "present" if os.path.exists(storage.TAKSKLAD_DATA_FILE) else "missing",
        "credentials": credentials_status(app_data),
        "telegram_enabled": bool_text(telegram_enabled),
        "telegram_token": bool_text(telegram_token_configured),
        "telegram_chats": str(chat_ids_count),
        "backend_enabled": bool_text(TAKSKLAD_BACKEND_ENABLED),
        "backend_read_orders": bool_text(TAKSKLAD_BACKEND_READ_ORDERS_ENABLED),
        "backend_origin": url_origin(TAKSKLAD_BACKEND_BASE_URL),
        "backend_token": bool_text(TAKSKLAD_BACKEND_API_TOKEN),
        "geocoder_key": bool_text(load_yandex_geocoder_key()),
        "pending_saves": str(len(pending_saves) if isinstance(pending_saves, list) else 0),
        "pending_prints": str(len(pending_prints) if isinstance(pending_prints, list) else 0),
        "pending_backend_events": str(len(pending_backend_events) if isinstance(pending_backend_events, list) else 0),
        "pending_telegram": str(len(pending_telegram) if isinstance(pending_telegram, list) else 0),
    }


def format_startup_self_check(check):
    ordered_keys = [
        "version",
        "build_label",
        "frozen",
        "spreadsheet_hash",
        "sheet",
        "credentials",
        "app_data",
        "telegram_enabled",
        "telegram_token",
        "telegram_chats",
        "backend_enabled",
        "backend_read_orders",
        "backend_origin",
        "backend_token",
        "geocoder_key",
        "pending_saves",
        "pending_prints",
        "pending_backend_events",
        "pending_telegram",
        "update_origin",
        "app_dir",
        "log_file",
    ]
    parts = []
    for key in ordered_keys:
        value = normalize_text(check.get(key))
        parts.append(f"{key}={value or '-'}")
    return "Startup self-check: " + " ".join(parts)


def log_startup_self_check():
    try:
        logging.info(format_startup_self_check(build_startup_self_check()))
    except Exception:
        logging.exception("Startup self-check failed")
