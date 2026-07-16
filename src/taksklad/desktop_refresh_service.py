import logging

from .backend_client import (
    fetch_backend_sheet_data,
    sync_backend_sources,
)
from .backend_events import (
    get_pending_backend_codes,
    load_pending_backend_events,
    sync_pending_backend_events,
)
from .utils import normalize_text


def backend_only_refresh_enabled():
    return True


def format_refresh_error_message(exc, has_cached_orders=False):
    reason = normalize_text(exc) or "ошибка без подробностей"
    if has_cached_orders:
        return (
            f"Список заказов не обновился: {reason}. "
            "Работаем с последним загруженным списком; повторите обновление позже."
        )
    if "backend" in reason.casefold():
        return (
            f"Список заказов пока не загружен: {reason}. "
            "Проверьте связь с backend и повторите обновление."
        )
    return f"Список заказов пока не загружен: {reason}. Проверьте связь с backend и повторите обновление."


def fetch_sheet_data():
    try:
        today_orders, sheet, all_existing_codes = fetch_backend_sheet_data()
        all_existing_codes.update(get_pending_backend_codes())
        return today_orders, sheet, all_existing_codes
    except Exception as exc:
        logging.warning("Backend orders unavailable; desktop fails closed", exc_info=True)
        raise RuntimeError(f"Backend refresh недоступен: {exc}") from exc


def fetch_sheet_data_with_sync(sync_skladbot=True):
    try:
        backend_result = sync_pending_backend_events()
    except Exception as exc:
        logging.warning("Backend queue sync failed before refresh", exc_info=True)
        backend_result = {
            "enabled": True,
            "synced": 0,
            "failed": 1,
            "remaining": len(load_pending_backend_events()),
            "message": str(exc),
        }

    try:
        sources_result = sync_backend_sources(sync_skladbot=sync_skladbot, wait_skladbot=False)
    except Exception as exc:
        logging.warning("Backend sources sync failed before backend refresh", exc_info=True)
        sources_result = {
            "status": "error",
            "message": str(exc),
            "skladbot": {"status": "error", "message": str(exc), "errors": 1},
        }

    try:
        today_orders, sheet, all_existing_codes = fetch_backend_sheet_data()
    except Exception as exc:
        logging.warning("Backend primary refresh failed; desktop fails closed", exc_info=True)
        raise RuntimeError(f"Backend refresh недоступен: {exc}") from exc
    all_existing_codes.update(get_pending_backend_codes())
    sync_result = {
        "synced": 0,
        "failed": 0,
        "remaining": 0,
        "backend": backend_result,
        "sources": sources_result,
        "primary_source": "backend",
        "backend_only_refresh": True,
        "skladbot": backend_skladbot_sync_result(sources_result),
    }
    return today_orders, sheet, all_existing_codes, sync_result


def backend_skladbot_sync_result(sources_result):
    if not isinstance(sources_result, dict):
        return {
            "enabled": True,
            "updated": 0,
            "matched": 0,
            "not_found": 0,
            "multiple": 0,
            "errors": 1,
            "message": "Backend sync вернул неизвестный ответ",
        }
    skladbot_result = sources_result.get("skladbot", {}) or {}
    if skladbot_result.get("status") == "skipped":
        return {
            "enabled": False,
            "updated": 0,
            "matched": 0,
            "not_found": 0,
            "multiple": 0,
            "errors": 0,
            "message": "SkladBot sync пропущен",
        }
    if skladbot_result.get("status") in {"started", "busy"}:
        return {
            "enabled": True,
            "pending": True,
            "updated": 0,
            "matched": 0,
            "not_found": 0,
            "multiple": 0,
            "errors": 0,
            "message": skladbot_result.get("message") or "SkladBot sync запущен",
        }
    return {
        "enabled": True,
        "updated": int(skladbot_result.get("updated") or 0),
        "matched": int(skladbot_result.get("matched") or 0),
        "not_found": int(skladbot_result.get("not_found") or 0),
        "multiple": int(skladbot_result.get("multiple") or 0),
        "errors": 1 if skladbot_result.get("status") == "error" else 0,
        "message": skladbot_result.get("error") or skladbot_result.get("message") or "SkladBot sync через backend",
    }
