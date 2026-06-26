import logging

from .backend_events import load_pending_backend_events
from .orders import get_order_date_value, order_group_key
from .pending_store import load_pending_prints, load_pending_saves
from .telegram_service import load_pending_telegram
from .utils import normalize_text


def _count_list(value):
    return len(value) if isinstance(value, list) else 0


def _int_value(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def backend_event_diagnostic_counts(events):
    events = events if isinstance(events, list) else []
    result = {
        "pending_backend_scan_events": 0,
        "pending_backend_order_complete_events": 0,
        "pending_backend_other_events": 0,
        "pending_backend_failed_events": 0,
        "pending_backend_attempted_events": 0,
        "pending_backend_max_attempts": 0,
    }
    for item in events:
        if not isinstance(item, dict):
            result["pending_backend_other_events"] += 1
            continue
        event_type = normalize_text(item.get("type"))
        if event_type == "scan":
            result["pending_backend_scan_events"] += 1
        elif event_type == "order_complete":
            result["pending_backend_order_complete_events"] += 1
        else:
            result["pending_backend_other_events"] += 1
        if normalize_text(item.get("last_error")):
            result["pending_backend_failed_events"] += 1
        attempts = _int_value(item.get("attempts"))
        if attempts > 0:
            result["pending_backend_attempted_events"] += 1
        result["pending_backend_max_attempts"] = max(
            result["pending_backend_max_attempts"],
            attempts,
        )
    return result


def build_refresh_diagnostic_summary(orders, all_existing_codes, sync_result=None, source="google"):
    orders = orders if isinstance(orders, list) else []
    sync_result = sync_result if isinstance(sync_result, dict) else {}
    skladbot_result = sync_result.get("skladbot") if isinstance(sync_result.get("skladbot"), dict) else {}
    backend_result = sync_result.get("backend") if isinstance(sync_result.get("backend"), dict) else {}
    google_pending_result = (
        sync_result.get("google_sheets_pending")
        if isinstance(sync_result.get("google_sheets_pending"), dict)
        else {}
    )
    pending_backend_events = load_pending_backend_events()
    groups = {order_group_key(order) for order in orders if isinstance(order, dict)}
    order_dates = {
        normalize_text(get_order_date_value(order))
        for order in orders
        if isinstance(order, dict) and normalize_text(get_order_date_value(order))
    }

    primary_source = normalize_text(sync_result.get("primary_source")) or normalize_text(source) or "google"

    return {
        "source": primary_source,
        "primary_source": primary_source,
        "backend_only_refresh": bool(sync_result.get("backend_only_refresh")),
        "emergency_google_fallback": bool(sync_result.get("emergency_google_fallback")),
        "orders": len(orders),
        "groups": len(groups),
        "order_dates": len(order_dates),
        "known_codes": len(all_existing_codes or []),
        "pending_saves": _count_list(load_pending_saves()),
        "pending_prints": _count_list(load_pending_prints()),
        "pending_backend_events": _count_list(pending_backend_events),
        **backend_event_diagnostic_counts(pending_backend_events),
        "pending_telegram": _count_list(load_pending_telegram()),
        "sync_synced": int(sync_result.get("synced") or 0),
        "sync_failed": int(sync_result.get("failed") or 0),
        "sync_remaining": int(sync_result.get("remaining") or 0),
        "backend_enabled": bool(backend_result.get("enabled")),
        "backend_synced": int(backend_result.get("synced") or 0),
        "backend_failed": int(backend_result.get("failed") or 0),
        "backend_remaining": int(backend_result.get("remaining") or 0),
        "google_mirror_status": normalize_text(google_pending_result.get("status")) or "unknown",
        "google_mirror_synced_exports": int(google_pending_result.get("synced") or 0),
        "google_mirror_failed_exports": int(google_pending_result.get("failed") or 0),
        "google_mirror_pending_exports": int(google_pending_result.get("remaining") or 0),
        "skladbot_enabled": bool(skladbot_result.get("enabled")),
        "skladbot_matched": int(skladbot_result.get("matched") or 0),
        "skladbot_not_found": int(skladbot_result.get("not_found") or 0),
        "skladbot_multiple": int(skladbot_result.get("multiple") or 0),
        "skladbot_errors": int(skladbot_result.get("errors") or 0),
    }


def format_refresh_diagnostic_summary(summary):
    ordered_keys = [
        "source",
        "primary_source",
        "backend_only_refresh",
        "emergency_google_fallback",
        "orders",
        "groups",
        "order_dates",
        "known_codes",
        "pending_saves",
        "pending_prints",
        "pending_backend_events",
        "pending_backend_scan_events",
        "pending_backend_order_complete_events",
        "pending_backend_other_events",
        "pending_backend_failed_events",
        "pending_backend_attempted_events",
        "pending_backend_max_attempts",
        "pending_telegram",
        "sync_synced",
        "sync_failed",
        "sync_remaining",
        "backend_enabled",
        "backend_synced",
        "backend_failed",
        "backend_remaining",
        "google_mirror_status",
        "google_mirror_synced_exports",
        "google_mirror_failed_exports",
        "google_mirror_pending_exports",
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
