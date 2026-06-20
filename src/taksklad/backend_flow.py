from .backend_client import (
    BackendApiError,
    backend_configured,
    backend_enabled,
    complete_order,
)
from .desktop_scan_rules import (
    format_duplicate_scan_message,
    format_scan_product_mismatch_message,
)
from .utils import normalize_kiz_code, normalize_text


def order_uses_backend_scan_path(order):
    return bool(backend_enabled() and normalize_text(order.get("_backend_order_item_id")))


def backend_event_matches_item(item, order_item_id):
    order_item_id = normalize_text(order_item_id)
    if not order_item_id or item.get("type") != "scan":
        return False
    return normalize_text((item.get("payload") or {}).get("order_item_id")) == order_item_id


def backend_event_matches_group(item, order_item_ids, order_ids):
    event_type = item.get("type")
    payload = item.get("payload") or {}
    if event_type == "scan":
        return normalize_text(payload.get("order_item_id")) in order_item_ids
    if event_type == "order_complete":
        return normalize_text(payload.get("order_id")) in order_ids
    return False


def backend_event_error_message(item):
    return normalize_text(item.get("last_error")) or "Backend не принял событие"


def backend_event_error_detail(item):
    detail = item.get("last_error_detail")
    return detail if isinstance(detail, dict) else {}


def backend_blocked_scan_events_for_item(sync_result, order_item_id):
    return [
        item for item in (sync_result.get("blocked_events") or [])
        if backend_event_matches_item(item, order_item_id)
    ]


def backend_blocked_scan_code(item):
    return normalize_kiz_code((item.get("payload") or {}).get("code"))


def format_backend_blocked_scan_message(blocked_events):
    first_event = (blocked_events or [{}])[0]
    code = backend_blocked_scan_code(first_event)
    detail_payload = backend_event_error_detail(first_event)
    detail_message = normalize_text(detail_payload.get("message")) or backend_event_error_message(first_event)
    detail = detail_message.lower()
    suffix = f": {code[:24]}..." if code else ""
    if "already scanned in another order item" in detail or "already scanned for another order item" in detail:
        return format_duplicate_scan_message(code, detail_payload.get("existing_order"))
    if "does not match order item" in detail:
        return format_scan_product_mismatch_message(
            code,
            detail_payload.get("product") or "",
            expected_product_key=detail_payload.get("expected_product_key") or "",
            scan_product_key=detail_payload.get("scan_product_key") or "",
        )
    if "exceeds remaining order item blocks" in detail:
        return f"Код короба превышает остаток позиции{suffix}"
    return f"Backend отклонил КИЗ. Сканируйте другой код{suffix}"


def backend_sync_item_blocker(sync_result, order_item_id, pending_events):
    for item in sync_result.get("blocked_events") or []:
        if backend_event_matches_item(item, order_item_id):
            return backend_event_error_message(item)
    current_pending = [item for item in pending_events if backend_event_matches_item(item, order_item_id)]
    if current_pending:
        first_error = backend_event_error_message(current_pending[0])
        return f"Backend не принял КИЗы текущей позиции. Осталось по позиции: {len(current_pending)}. {first_error}"
    return ""


def backend_sync_group_blocker(sync_result, order_item_ids, order_ids, pending_events):
    order_item_ids = {normalize_text(item_id) for item_id in order_item_ids if normalize_text(item_id)}
    order_ids = {normalize_text(order_id) for order_id in order_ids if normalize_text(order_id)}
    for item in sync_result.get("blocked_events") or []:
        if backend_event_matches_group(item, order_item_ids, order_ids):
            return backend_event_error_message(item)
    current_pending = [
        item for item in pending_events
        if backend_event_matches_group(item, order_item_ids, order_ids)
    ]
    if current_pending:
        first_error = backend_event_error_message(current_pending[0])
        return f"Backend не принял события текущего заказа. Осталось по заказу: {len(current_pending)}. {first_error}"
    return ""


def unsaved_backend_scan_codes(order, scanned_codes):
    existing_codes = {
        normalize_kiz_code(code)
        for code in (order.get("_existing_scanned_codes") or [])
        if normalize_kiz_code(code)
    }
    return [
        code for code in scanned_codes
        if normalize_kiz_code(code) and normalize_kiz_code(code) not in existing_codes
    ]


def is_backend_order_already_completed_error(exc):
    if not isinstance(exc, BackendApiError) or exc.retryable:
        return False
    detail = normalize_text(exc.detail or exc).lower()
    return any(
        marker in detail
        for marker in (
            "already completed",
            "already complete",
            "already closed",
            "order completed",
            "order is completed",
            "order closed",
            "заказ уже заверш",
            "заказ заверш",
            "заказ уже закрыт",
            "заказ закрыт",
        )
    )


def complete_backend_orders_or_raise(order_ids):
    order_ids = [normalize_text(order_id) for order_id in order_ids if normalize_text(order_id)]
    if not order_ids:
        return {"completed": 0, "already_completed": 0}
    if not backend_configured():
        raise RuntimeError("Backend включён, но URL сервера не настроен. Заказ не архивирован.")

    result = {"completed": 0, "already_completed": 0}
    for order_id in order_ids:
        try:
            complete_order(order_id)
            result["completed"] += 1
        except BackendApiError as exc:
            if is_backend_order_already_completed_error(exc):
                result["already_completed"] += 1
                continue
            raise
    return result


def format_print_failure_after_backend_complete(exc, backend_complete_result):
    completed = int(backend_complete_result.get("completed") or 0)
    already_completed = int(backend_complete_result.get("already_completed") or 0)
    if completed or already_completed:
        return (
            "Backend уже завершил заказ, но сводный лист не напечатался. "
            "Статус на сервере не откатываю. Повторите печать через завершение заказа или очередь печати. "
            f"Причина: {exc}"
        )
    return f"Сводный лист не напечатался. Заказ не архивирован. Причина: {exc}"
