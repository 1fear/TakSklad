import hashlib
import logging
import os
import socket
import sys
from urllib.parse import urlparse

from . import storage
from .config import (
    APP_DIR,
    APP_BUILD_LABEL,
    APP_VERSION,
    LOG_FILE,
    TAKSKLAD_BACKEND_BASE_URL,
    TAKSKLAD_BACKEND_ENABLED,
    TAKSKLAD_BACKEND_ONLY_REFRESH,
    TAKSKLAD_BACKEND_READ_ORDERS_ENABLED,
    TELEGRAM_DESKTOP_POLLING_ENABLED,
    UPDATE_INFO_URL,
)
from .geocoding import load_yandex_geocoder_key
from .telegram_service import get_telegram_chat_ids, load_telegram_settings
from .secret_store import BACKEND_AUTH_BUNDLE_SECRET, SecretStoreError, load_secret
from .update_service import compare_versions, package_transition_required
from .utils import normalize_text


def safe_hash(value, length=10):
    text = normalize_text(value)
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


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


def manifest_bool(value):
    if isinstance(value, bool):
        return value
    return normalize_text(value).lower() in {"1", "true", "yes", "on", "да"}


def build_workstation_id():
    hostname = normalize_text(socket.gethostname()) or "unknown"
    return safe_hash(f"{hostname}|{APP_DIR}", length=12)


def format_app_version_label():
    label = normalize_text(APP_BUILD_LABEL)
    if label:
        return f"Версия: {APP_VERSION} · {label}"
    return f"Версия: {APP_VERSION}"


def build_version_update_status(update_info=None, error=None, current_version=APP_VERSION):
    payload = update_info if isinstance(update_info, dict) else {}
    latest_version = normalize_text(payload.get("latest_version"))
    min_supported_version = normalize_text(payload.get("min_supported_version"))
    package_type = normalize_text(payload.get("package_type"))
    mandatory_update = manifest_bool(payload.get("mandatory"))
    block_workflow = manifest_bool(payload.get("block_workflow"))
    error_class = type(error).__name__ if error is not None else ""
    update_available = bool(latest_version) and compare_versions(current_version, latest_version) < 0
    below_min_version = bool(min_supported_version) and compare_versions(current_version, min_supported_version) < 0
    package_update_required = bool(payload) and package_transition_required(payload)
    blocking_forced_update = block_workflow and (below_min_version or (mandatory_update and update_available))

    if error_class:
        state = "unavailable"
    elif not payload:
        state = "checking"
    elif blocking_forced_update:
        state = "blocked"
    elif update_available or below_min_version or package_update_required:
        state = "outdated"
    else:
        state = "current"

    return {
        "state": state,
        "workstation_id": build_workstation_id(),
        "current_version": current_version,
        "build_label": APP_BUILD_LABEL,
        "latest_version": latest_version,
        "min_supported_version": min_supported_version,
        "package_type": package_type,
        "mandatory": bool_text(mandatory_update),
        "block_workflow": bool_text(block_workflow),
        "update_available": bool_text(update_available),
        "below_min_version": bool_text(below_min_version),
        "package_update_required": bool_text(package_update_required),
        "blocking": bool_text(blocking_forced_update),
        "error_class": error_class,
    }


def format_version_update_status_label(status):
    status = status if isinstance(status, dict) else build_version_update_status()
    current_version = normalize_text(status.get("current_version")) or APP_VERSION
    latest_version = normalize_text(status.get("latest_version"))
    min_supported_version = normalize_text(status.get("min_supported_version"))
    package_type = normalize_text(status.get("package_type"))
    workstation_id = normalize_text(status.get("workstation_id")) or build_workstation_id()
    base = format_app_version_label()
    suffix = f" · ПК {workstation_id}"
    if package_type:
        suffix += f" · пакет {package_type}"

    state = normalize_text(status.get("state"))
    if state == "current":
        return f"{base} · актуальная{suffix}"
    if state == "blocked":
        target = min_supported_version or latest_version or "новая версия"
        return f"⛔ Версия {current_version} заблокирована до обновления минимум до {target}{suffix}"
    if state == "outdated":
        target = latest_version or min_supported_version or "новая версия"
        return f"⚠ Доступно обновление {target}{suffix}"
    if state == "unavailable":
        error_class = normalize_text(status.get("error_class")) or "unknown"
        return f"⚠ Статус обновления недоступен ({error_class}){suffix}"
    return f"{base} · проверка обновлений{suffix}"


def build_startup_self_check(version_status=None):
    version_status = version_status if isinstance(version_status, dict) else build_version_update_status()
    app_data = storage.load_app_data()
    app_data_recovery = storage.get_app_data_recovery_status()
    telegram_settings = load_telegram_settings()
    telegram_enabled = bool(telegram_settings.get("enabled"))
    telegram_token_configured = bool(normalize_text(telegram_settings.get("bot_token")))
    chat_ids_count = len(get_telegram_chat_ids(telegram_settings))

    pending_prints = app_data.get("pending_prints")
    pending_backend_events = app_data.get("pending_backend_events")
    pending_telegram = app_data.get("pending_telegram")

    return {
        "version": APP_VERSION,
        "build_label": APP_BUILD_LABEL,
        "workstation_id": version_status.get("workstation_id") or build_workstation_id(),
        "version_status": version_status.get("state") or "checking",
        "version_latest": version_status.get("latest_version") or "",
        "version_min_supported": version_status.get("min_supported_version") or "",
        "version_package_type": version_status.get("package_type") or "",
        "version_mandatory": version_status.get("mandatory") or "no",
        "version_block_workflow": version_status.get("block_workflow") or "no",
        "version_error_class": version_status.get("error_class") or "",
        "frozen": bool_text(getattr(sys, "frozen", False)),
        "app_dir": APP_DIR,
        "log_file": LOG_FILE,
        "update_origin": url_origin(UPDATE_INFO_URL),
        "app_data": "present" if os.path.exists(storage.TAKSKLAD_DATA_FILE) else "missing",
        "app_data_status": app_data_recovery.get("status") or "unknown",
        "app_data_restored": bool_text(bool(app_data_recovery.get("restored_from"))),
        "telegram_enabled": bool_text(telegram_enabled),
        "telegram_token": bool_text(telegram_token_configured),
        "telegram_chats": str(chat_ids_count),
        "telegram_desktop_polling": bool_text(TELEGRAM_DESKTOP_POLLING_ENABLED),
        "backend_enabled": bool_text(TAKSKLAD_BACKEND_ENABLED),
        "backend_read_orders": bool_text(TAKSKLAD_BACKEND_READ_ORDERS_ENABLED),
        "backend_only_refresh": bool_text(TAKSKLAD_BACKEND_ONLY_REFRESH),
        "backend_origin": url_origin(TAKSKLAD_BACKEND_BASE_URL),
        "backend_token": bool_text(secret_available(BACKEND_AUTH_BUNDLE_SECRET)),
        "geocoder_key": bool_text(load_yandex_geocoder_key()),
        "pending_prints": str(len(pending_prints) if isinstance(pending_prints, list) else 0),
        "pending_backend_events": str(len(pending_backend_events) if isinstance(pending_backend_events, list) else 0),
        "pending_telegram": str(len(pending_telegram) if isinstance(pending_telegram, list) else 0),
    }


def secret_available(name):
    try:
        return bool(normalize_text(load_secret(name)))
    except SecretStoreError:
        return False


def format_startup_self_check(check):
    ordered_keys = [
        "version",
        "build_label",
        "workstation_id",
        "version_status",
        "version_latest",
        "version_min_supported",
        "version_package_type",
        "version_mandatory",
        "version_block_workflow",
        "version_error_class",
        "frozen",
        "app_data",
        "app_data_status",
        "app_data_restored",
        "telegram_enabled",
        "telegram_token",
        "telegram_chats",
        "telegram_desktop_polling",
        "backend_enabled",
        "backend_read_orders",
        "backend_only_refresh",
        "backend_origin",
        "backend_token",
        "geocoder_key",
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
