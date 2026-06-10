import logging
import socket
from datetime import datetime

from .backend_client import (
    BackendApiError,
    backend_configured,
    complete_order,
    create_scan,
    undo_scan,
)
from .storage import load_data_section, save_data_section
from .utils import make_hash, normalize_kiz_code, normalize_text, split_codes


def load_pending_backend_events():
    data = load_data_section("pending_backend_events", [])
    return data if isinstance(data, list) else []


def save_pending_backend_events(items):
    return save_data_section("pending_backend_events", items)


def get_pending_backend_codes():
    codes = set()
    for item in load_pending_backend_events():
        if item.get("type") == "scan":
            code = normalize_kiz_code((item.get("payload") or {}).get("code"))
            if code:
                codes.add(code)
    return codes


def make_backend_event_id(event_type, payload):
    return make_hash({
        "type": event_type,
        "order_item_id": payload.get("order_item_id"),
        "order_id": payload.get("order_id"),
        "code": payload.get("code"),
    })


def add_pending_backend_event(event_type, payload):
    if not backend_configured():
        return ""

    pending = load_pending_backend_events()
    event_id = make_backend_event_id(event_type, payload)
    for item in pending:
        if item.get("id") == event_id:
            return event_id

    now = datetime.now().astimezone().isoformat()
    pending.append({
        "id": event_id,
        "type": event_type,
        "payload": payload,
        "created_at": now,
        "updated_at": now,
        "attempts": 0,
        "last_error": "",
    })
    save_pending_backend_events(pending)
    return event_id


def queue_backend_scan(order, code, scanned_at=None):
    order_item_id = normalize_text(order.get("_backend_order_item_id"))
    code = normalize_kiz_code(code)
    if not order_item_id or not code:
        return ""
    return add_pending_backend_event(
        "scan",
        {
            "order_item_id": order_item_id,
            "code": code,
            "workstation_id": socket.gethostname(),
            "scanned_at": scanned_at or datetime.now().astimezone().isoformat(),
        },
    )


def queue_backend_scans_for_order(order):
    queued = 0
    for code in split_codes(order.get("Отсканированные коды")):
        if queue_backend_scan(order, code):
            queued += 1
    return queued


def remove_pending_backend_scan(order, code):
    order_item_id = normalize_text(order.get("_backend_order_item_id"))
    if not order_item_id:
        return False
    code = normalize_kiz_code(code)
    if not code:
        return False
    event_id = make_backend_event_id("scan", {"order_item_id": order_item_id, "code": code})
    pending = load_pending_backend_events()
    new_pending = [item for item in pending if item.get("id") != event_id]
    if len(new_pending) == len(pending):
        return False
    save_pending_backend_events(new_pending)
    return True


def undo_backend_scan(order, code):
    if remove_pending_backend_scan(order, code):
        return {"status": "removed_from_queue"}
    order_item_id = normalize_text(order.get("_backend_order_item_id"))
    code = normalize_kiz_code(code)
    if not order_item_id or not code:
        return {"status": "skipped"}
    if not backend_configured():
        raise BackendApiError("Backend URL не настроен")
    return undo_scan(
        order_item_id,
        code,
        workstation_id=socket.gethostname(),
        actor="desktop",
    )


def queue_backend_order_complete(order_id):
    order_id = normalize_text(order_id)
    if not order_id:
        return ""
    return add_pending_backend_event("order_complete", {"order_id": order_id})


def is_duplicate_scan_ack(exc):
    if not isinstance(exc, BackendApiError) or exc.status_code != 409:
        return False
    detail = str(exc.detail or exc).lower()
    return "already scanned for this order item" in detail


def is_fully_scanned_item_ack(exc):
    if not isinstance(exc, BackendApiError) or exc.status_code != 409:
        return False
    detail = str(exc.detail or exc).lower()
    return "order item is already fully scanned" in detail


def is_non_retryable_scan_conflict(exc):
    if not isinstance(exc, BackendApiError) or exc.status_code != 409:
        return False
    detail = str(exc.detail or exc).lower()
    return any(
        marker in detail
        for marker in (
            "scan product does not match order item",
            "code already scanned in another order item",
            "code already scanned for another order item",
            "aggregate box product does not match order item",
            "aggregate box exceeds remaining order item blocks",
        )
    )


def is_stale_backend_event_ack(item, exc):
    if not isinstance(exc, BackendApiError) or exc.retryable:
        return False
    detail = str(exc.detail or exc).lower()
    return item.get("type") == "order_complete" and exc.status_code == 404 and "order not found" in detail


def is_incomplete_order_complete_ack(item, exc):
    if not isinstance(exc, BackendApiError) or exc.status_code != 409:
        return False
    if item.get("type") != "order_complete":
        return False
    detail = str(exc.detail or exc).lower()
    return "order has incomplete required items" in detail


def sync_pending_backend_events():
    if not backend_configured():
        return {"synced": 0, "failed": 0, "remaining": len(load_pending_backend_events()), "enabled": False}

    pending = load_pending_backend_events()
    if not pending:
        return {"synced": 0, "failed": 0, "remaining": 0, "enabled": True}

    synced = 0
    failed = 0
    dropped = 0
    blocked = 0
    blocked_events = []
    remaining = []
    for item in pending:
        try:
            event_type = item.get("type")
            payload = item.get("payload") or {}
            if event_type == "scan":
                create_scan(
                    payload.get("order_item_id"),
                    payload.get("code"),
                    workstation_id=payload.get("workstation_id"),
                    scanned_at=payload.get("scanned_at"),
                )
            elif event_type == "order_complete":
                complete_order(payload.get("order_id"))
            else:
                logging.warning("Backend queue: unknown event type skipped: %s", event_type)
            synced += 1
        except BackendApiError as exc:
            if item.get("type") == "scan" and (is_duplicate_scan_ack(exc) or is_fully_scanned_item_ack(exc)):
                synced += 1
                continue
            if item.get("type") == "scan" and is_non_retryable_scan_conflict(exc):
                dropped += 1
                blocked += 1
                blocked_item = dict(item)
                blocked_item["attempts"] = int(blocked_item.get("attempts") or 0) + 1
                blocked_item["last_error"] = str(exc)
                blocked_item["updated_at"] = datetime.now().astimezone().isoformat()
                blocked_events.append(blocked_item)
                logging.warning(
                    "Backend queue: dropped blocked scan event for item %s: %s",
                    (item.get("payload") or {}).get("order_item_id"),
                    exc,
                )
                continue
            if is_stale_backend_event_ack(item, exc):
                dropped += 1
                logging.warning(
                    "Backend queue: dropped stale event %s: %s",
                    item.get("type"),
                    exc,
                )
                continue
            if is_incomplete_order_complete_ack(item, exc):
                dropped += 1
                blocked += 1
                blocked_item = dict(item)
                blocked_item["attempts"] = int(blocked_item.get("attempts") or 0) + 1
                blocked_item["last_error"] = str(exc)
                blocked_item["updated_at"] = datetime.now().astimezone().isoformat()
                blocked_events.append(blocked_item)
                logging.warning(
                    "Backend queue: dropped incomplete order_complete event for order %s: %s",
                    (item.get("payload") or {}).get("order_id"),
                    exc,
                )
                continue
            failed += 1
            item["attempts"] = int(item.get("attempts") or 0) + 1
            item["last_error"] = str(exc)
            item["updated_at"] = datetime.now().astimezone().isoformat()
            remaining.append(item)
        except Exception as exc:
            failed += 1
            item["attempts"] = int(item.get("attempts") or 0) + 1
            item["last_error"] = str(exc)
            item["updated_at"] = datetime.now().astimezone().isoformat()
            remaining.append(item)

    save_pending_backend_events(remaining)
    return {
        "synced": synced,
        "failed": failed,
        "remaining": len(remaining),
        "dropped": dropped,
        "blocked": blocked,
        "blocked_events": blocked_events,
        "enabled": True,
    }
