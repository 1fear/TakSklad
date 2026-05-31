import logging

from .backend_events import load_pending_backend_events
from .orders import get_order_date_value, order_group_key
from .pending_store import load_pending_prints, load_pending_saves
from .telegram_service import load_pending_telegram
from .utils import normalize_text


def _count_list(value):
    return len(value) if isinstance(value, list) else 0


def build_refresh_diagnostic_summary(orders, all_existing_codes, sync_result=None, source="google"):
    orders = orders if isinstance(orders, list) else []
    sync_result = sync_result if isinstance(sync_result, dict) else {}
    skladbot_result = sync_result.get("skladbot") if isinstance(sync_result.get("skladbot"), dict) else {}
    backend_result = sync_result.get("backend") if isinstance(sync_result.get("backend"), dict) else {}
    groups = {order_group_key(order) for order in orders if isinstance(order, dict)}
    order_dates = {
        normalize_text(get_order_date_value(order))
        for order in orders
        if isinstance(order, dict) and normalize_text(get_order_date_value(order))
    }

    return {
        "source": normalize_text(source) or "google",
        "orders": len(orders),
        "groups": len(groups),
        "order_dates": len(order_dates),
        "known_codes": len(all_existing_codes or []),
        "pending_saves": _count_list(load_pending_saves()),
        "pending_prints": _count_list(load_pending_prints()),
        "pending_backend_events": _count_list(load_pending_backend_events()),
        "pending_telegram": _count_list(load_pending_telegram()),
        "sync_synced": int(sync_result.get("synced") or 0),
        "sync_failed": int(sync_result.get("failed") or 0),
        "sync_remaining": int(sync_result.get("remaining") or 0),
        "backend_enabled": bool(backend_result.get("enabled")),
        "backend_synced": int(backend_result.get("synced") or 0),
        "backend_failed": int(backend_result.get("failed") or 0),
        "backend_remaining": int(backend_result.get("remaining") or 0),
        "skladbot_enabled": bool(skladbot_result.get("enabled")),
        "skladbot_matched": int(skladbot_result.get("matched") or 0),
        "skladbot_not_found": int(skladbot_result.get("not_found") or 0),
        "skladbot_multiple": int(skladbot_result.get("multiple") or 0),
        "skladbot_errors": int(skladbot_result.get("errors") or 0),
    }


def format_refresh_diagnostic_summary(summary):
    ordered_keys = [
        "source",
        "orders",
        "groups",
        "order_dates",
        "known_codes",
        "pending_saves",
        "pending_prints",
        "pending_backend_events",
        "pending_telegram",
        "sync_synced",
        "sync_failed",
        "sync_remaining",
        "backend_enabled",
        "backend_synced",
        "backend_failed",
        "backend_remaining",
        "skladbot_enabled",
        "skladbot_matched",
        "skladbot_not_found",
        "skladbot_multiple",
        "skladbot_errors",
    ]
    return "Refresh diagnostic summary: " + " ".join(
        f"{key}={summary.get(key)}" for key in ordered_keys
    )


def log_refresh_diagnostic_summary(orders, all_existing_codes, sync_result=None, source="google"):
    try:
        logging.info(
            format_refresh_diagnostic_summary(
                build_refresh_diagnostic_summary(
                    orders,
                    all_existing_codes,
                    sync_result=sync_result,
                    source=source,
                )
            )
        )
    except Exception:
        logging.exception("Refresh diagnostic summary failed")
