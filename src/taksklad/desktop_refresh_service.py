import logging

from .backend_client import (
    backend_enabled,
    backend_read_orders_enabled,
    fetch_backend_sheet_data,
    sync_backend_sources,
)
from .backend_events import (
    get_pending_backend_codes,
    load_pending_backend_events,
    sync_pending_backend_events,
)
from .pending_store import (
    get_pending_codes,
    load_pending_saves,
    sync_pending_saves,
)
from .sheets import get_all_existing_codes, get_today_orders
from .skladbot_sync import sync_skladbot_request_numbers
from .utils import normalize_text


def format_refresh_error_message(exc, has_cached_orders=False):
    reason = normalize_text(exc) or "ошибка без подробностей"
    if has_cached_orders:
        return (
            f"Список заказов не обновился: {reason}. "
            "Работаем с последним загруженным списком; повторите обновление позже."
        )
    return (
        f"Список заказов пока не загружен: {reason}. "
        "Проверьте связь с Google Sheets и повторите обновление."
    )


def fetch_google_sheet_data():
    today_orders, sheet, all_rows = get_today_orders(apply_skladbot_filter=False, include_rows=True)
    all_existing_codes = get_all_existing_codes(sheet, all_rows=all_rows) if sheet else set()
    all_existing_codes.update(get_pending_codes())
    all_existing_codes.update(get_pending_backend_codes())
    return today_orders, sheet, all_existing_codes


def fetch_sheet_data():
    if backend_read_orders_enabled():
        try:
            today_orders, sheet, all_existing_codes = fetch_backend_sheet_data()
            all_existing_codes.update(get_pending_backend_codes())
            return today_orders, sheet, all_existing_codes
        except Exception:
            logging.warning("Backend orders unavailable, fallback to Google Sheets", exc_info=True)

    try:
        return fetch_google_sheet_data()
    except Exception:
        if not backend_read_orders_enabled():
            raise
        logging.warning("Google Sheets недоступен, загружаем fallback из backend", exc_info=True)
        today_orders, sheet, all_existing_codes = fetch_backend_sheet_data()
        all_existing_codes.update(get_pending_backend_codes())
        return today_orders, sheet, all_existing_codes


def fetch_sheet_data_with_sync(sync_skladbot=True):
    if backend_read_orders_enabled():
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
                "google_sheets": {"status": "unknown", "message": str(exc)},
                "skladbot": {"status": "unknown", "message": str(exc), "errors": 1},
            }

        try:
            today_orders, sheet, all_existing_codes = fetch_backend_sheet_data()
            all_existing_codes.update(get_pending_backend_codes())
            google_queue_result = {
                "synced": 0,
                "failed": 0,
                "remaining": len(load_pending_saves()),
            }
            primary_source = "backend"
        except Exception:
            logging.warning("Backend primary refresh failed, fallback to Google Sheets", exc_info=True)
            today_orders, sheet, all_existing_codes = fetch_google_sheet_data()
            google_queue_result = {"synced": 0, "failed": 0, "remaining": len(load_pending_saves())}
            primary_source = "google_fallback"

        sync_result = {
            "synced": google_queue_result.get("synced", 0),
            "failed": google_queue_result.get("failed", 0),
            "remaining": google_queue_result.get("remaining", 0),
            "dropped": google_queue_result.get("dropped", 0),
            "backend": backend_result,
            "sources": sources_result,
            "primary_source": primary_source,
            "google_sheets": sources_result.get("google_sheets", {}) if isinstance(sources_result, dict) else {},
            "skladbot": backend_skladbot_sync_result(sources_result),
        }
        return today_orders, sheet, all_existing_codes, sync_result

    fallback_orders, sheet, all_rows = get_today_orders(apply_skladbot_filter=False, include_rows=True)
    today_orders = fallback_orders
    sync_result = sync_pending_saves(sheet)
    backend_result = sync_pending_backend_events() if backend_enabled() else {"enabled": False}
    sheet_rows_changed = bool(sync_result.get("synced"))
    if sync_skladbot:
        skladbot_result = sync_skladbot_request_numbers(sheet)
        sheet_rows_changed = sheet_rows_changed or bool(skladbot_result.get("updated"))
    else:
        skladbot_result = {
            "enabled": False,
            "updated": 0,
            "matched": 0,
            "not_found": 0,
            "multiple": 0,
            "errors": 0,
            "message": "SkladBot синхронизируется отдельно",
        }
    if sync_skladbot and skladbot_result.get("enabled") and not skladbot_result.get("errors"):
        today_orders, sheet, all_rows = get_today_orders(apply_skladbot_filter=False, include_rows=True)
    elif sync_result.get("synced"):
        today_orders, sheet, all_rows = get_today_orders(apply_skladbot_filter=False, include_rows=True)
    elif skladbot_result.get("errors"):
        logging.warning(
            "SkladBot недоступен, показываем активные Google-заказы без фильтра SkladBot: %s",
            skladbot_result.get("message", ""),
        )
    elif sheet_rows_changed:
        today_orders, sheet, all_rows = get_today_orders(apply_skladbot_filter=False, include_rows=True)
    all_existing_codes = get_all_existing_codes(sheet, all_rows=all_rows) if sheet else set()
    all_existing_codes.update(get_pending_codes())
    all_existing_codes.update(get_pending_backend_codes())
    sync_result["skladbot"] = skladbot_result
    sync_result["backend"] = backend_result
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
