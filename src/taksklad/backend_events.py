import logging
import json
import socket
import threading
from datetime import datetime

from .backend_client import (
    BackendApiError,
    BackendTransportError,
    backend_configured,
    complete_order,
    create_scan,
    undo_scan,
)
from .storage import (
    append_queue_item,
    load_data_section,
    mutate_queue_section,
    reconcile_queue_section,
    save_data_section,
)
from .utils import make_hash, normalize_kiz_code, normalize_text, split_codes


def load_pending_backend_events():
    data = load_data_section("pending_backend_events", [])
    return data if isinstance(data, list) else []


def save_pending_backend_events(items):
    return save_data_section("pending_backend_events", items)


BLOCKED_BACKEND_EVENTS_LIMIT = 500
SCAN_DUPLICATE_ACK_CODE = "scan_duplicate_ack"
KNOWN_NON_RETRYABLE_SCAN_ERROR_CODES = frozenset({
    "kiz_format_invalid",
    "kiz_already_owned",
    "order_item_fully_scanned_new_code",
    "order_closed",
    "transfer_order_irreversible",
    "legal_entity_unresolved",
    "aggregate_box_product_mismatch",
    "aggregate_box_exceeds_plan",
    "scan_product_mismatch",
    "shipment_manifest_mismatch",
})


def load_blocked_backend_events():
    data = load_data_section("blocked_backend_events", [])
    return data if isinstance(data, list) else []


def save_blocked_backend_events(items):
    return save_data_section("blocked_backend_events", items)


def record_blocked_backend_events(items):
    """Persist events the backend refused for good.

    They are removed from the retry queue on purpose, but the blocks they
    describe physically left the warehouse, so the record has to survive the
    process. Without this the only trace was an in-memory list that the UI
    discards unless it happens to match the position the operator has open.
    """
    items = [item for item in (items or []) if isinstance(item, dict)]
    if not items:
        return 0
    stored = load_blocked_backend_events()
    known = {_blocked_event_key(item) for item in stored}
    added = [item for item in items if _blocked_event_key(item) not in known]
    if not added:
        return 0
    save_blocked_backend_events((stored + added)[-BLOCKED_BACKEND_EVENTS_LIMIT:])
    return len(added)


def _blocked_event_key(item):
    payload = item.get("payload") or {}
    return (
        normalize_text(item.get("id")),
        normalize_text(item.get("type")),
        normalize_text(payload.get("order_item_id")),
        normalize_kiz_code(payload.get("code")),
    )


def get_pending_backend_codes():
    codes = set()
    for item in load_pending_backend_events():
        if item.get("type") == "scan":
            code = normalize_kiz_code((item.get("payload") or {}).get("code"))
            if code:
                codes.add(code)
    return codes


def migrate_legacy_pending_saves_to_backend_events():
    """Move convertible pre-DB scan records into the durable backend queue.

    Records without a backend item id are kept untouched and reported as a
    startup blocker so a legacy queue can never be silently discarded.
    """

    legacy = load_data_section("pending_saves", [])
    legacy = legacy if isinstance(legacy, list) else []
    if not legacy:
        return {"migrated": 0, "remaining": 0}

    migrated = 0
    remaining = []
    for item in legacy:
        order = item.get("order") or {}
        order_item_id = normalize_text(order.get("_backend_order_item_id"))
        codes = split_codes(item.get("codes") or [])
        if not order_item_id or not codes:
            remaining.append(item)
            continue
        migrated_item = True
        for code in codes:
            event_id = add_pending_backend_event(
                "scan",
                {
                    "order_item_id": order_item_id,
                    "code": normalize_kiz_code(code),
                    "workstation_id": socket.gethostname(),
                    "scanned_at": item.get("created_at") or datetime.now().astimezone().isoformat(),
                },
            )
            if not event_id:
                migrated_item = False
                break
        if migrated_item:
            migrated += 1
        else:
            remaining.append(item)

    save_data_section("pending_saves", remaining)
    return {"migrated": migrated, "remaining": len(remaining)}


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

    event_id = make_backend_event_id(event_type, payload)
    now = datetime.now().astimezone().isoformat()
    added = append_queue_item("pending_backend_events", {
        "id": event_id,
        "type": event_type,
        "payload": payload,
        "created_at": now,
        "updated_at": now,
        "attempts": 0,
        "last_error": "",
    })
    if added and event_type == "scan":
        # Тот же код поставлен заново, значит прежняя отметка «доставлено»
        # больше не верна. Если такое событие уже стояло в очереди, ключ не трогаем.
        forget_delivered_scan(payload.get("order_item_id"), payload.get("code"))
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
    removed = {"value": False}

    def remove(items):
        result = [item for item in items if item.get("id") != event_id]
        removed["value"] = len(result) != len(items)
        return result

    mutate_queue_section("pending_backend_events", remove)
    forget_delivered_scan(order_item_id, code)
    return removed["value"]


def undo_backend_scan(order, code):
    removed_from_queue = remove_pending_backend_scan(order, code)
    order_item_id = normalize_text(order.get("_backend_order_item_id"))
    code = normalize_kiz_code(code)
    if removed_from_queue:
        return {"status": "removed_from_queue"}
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


def remove_pending_backend_order_complete(order_id):
    order_id = normalize_text(order_id)
    if not order_id:
        return False
    event_id = make_backend_event_id("order_complete", {"order_id": order_id})
    removed = {"value": False}

    def remove(items):
        result = [item for item in items if item.get("id") != event_id]
        removed["value"] = len(result) != len(items)
        return result

    mutate_queue_section("pending_backend_events", remove)
    return removed["value"]


def backend_error_code(exc):
    detail = getattr(exc, "detail", None)
    if not isinstance(detail, dict):
        return ""
    return normalize_text(detail.get("code")).lower()


def is_duplicate_scan_ack(exc):
    if not isinstance(exc, BackendApiError) or exc.status_code != 409:
        return False
    if backend_error_code(exc) == SCAN_DUPLICATE_ACK_CODE:
        return True
    detail = str(exc.detail or exc).lower()
    return "already scanned for this order item" in detail


def is_non_retryable_scan_conflict(exc):
    if not isinstance(exc, BackendApiError):
        return False
    if exc.status_code == 422:
        # Формат кода сервер отбивает 422, а не 409: тот же код не пройдёт
        # никогда, а событие в очереди держало заказ незакрытым
        return backend_error_code(exc) == "kiz_format_invalid"
    if exc.status_code != 409:
        return False
    code = backend_error_code(exc)
    if code == SCAN_DUPLICATE_ACK_CODE:
        return False
    if code in KNOWN_NON_RETRYABLE_SCAN_ERROR_CODES:
        return True
    if code:
        return True
    detail = str(exc.detail or exc).lower()
    return any(
        marker in detail
        for marker in (
            "scan product does not match order item",
            "code already scanned in another order item",
            "code already scanned for another order item",
            "aggregate box product does not match order item",
            "aggregate box exceeds remaining order item blocks",
            "order_item_fully_scanned_new_code",
            "order item is already fully scanned",
        )
    )


def backend_error_detail_payload(exc):
    detail = getattr(exc, "detail", "")
    if isinstance(detail, (dict, list, str, int, float, bool)) or detail is None:
        try:
            json.dumps(detail, ensure_ascii=False)
            return detail
        except (TypeError, ValueError):
            pass
    return normalize_text(detail)


def backend_error_kind(exc):
    # Сетью считается только транспортный обрыв: сервер не ответил вовсе,
    # поэтому такие события ждут связи, а не разбора оператором. Ошибка без
    # кода ответа, но не сетевая (не-JSON от прокси, ошибка клиента), это
    # «server»: она не останавливает проход очереди и не читается как обрыв.
    if isinstance(exc, BackendTransportError):
        return "network"
    return "server"


def mark_untried_tail_as_network(tail):
    """Хвост, до которого проход не дошёл, ждёт связи так же, как упавшее событие.

    Тексты про обрыв требуют сетевой признак у всех событий позиции, а он
    появлялся только у первого. Событие с серверной ошибкой не трогаем: отказ
    сервера не превращается в «ждём связи». Попытки, ошибка и время хвоста не
    меняются, потому что отправить его никто не пытался.
    """
    for item in tail:
        if normalize_text(item.get("last_error_kind")) in {"", "network"}:
            item["last_error_kind"] = "network"
    return tail


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


_SYNC_LOCK = threading.Lock()
_FOREGROUND_WAITERS_LOCK = threading.Lock()
_foreground_waiters = 0

_DELIVERED_SCANS_LOCK = threading.Lock()
_DELIVERED_SCAN_KEYS = set()


def _increment_foreground_waiters():
    global _foreground_waiters
    with _FOREGROUND_WAITERS_LOCK:
        _foreground_waiters += 1


def _decrement_foreground_waiters():
    global _foreground_waiters
    with _FOREGROUND_WAITERS_LOCK:
        _foreground_waiters -= 1


def _foreground_waiters_count():
    with _FOREGROUND_WAITERS_LOCK:
        return _foreground_waiters


def _delivered_scan_key(order_item_id, code):
    return (normalize_text(order_item_id), normalize_kiz_code(code))


def is_scan_delivered(order_item_id, code):
    with _DELIVERED_SCANS_LOCK:
        return _delivered_scan_key(order_item_id, code) in _DELIVERED_SCAN_KEYS


def forget_delivered_scan(order_item_id, code):
    with _DELIVERED_SCANS_LOCK:
        _DELIVERED_SCAN_KEYS.discard(_delivered_scan_key(order_item_id, code))


def _remember_delivered_scan(order_item_id, code):
    with _DELIVERED_SCANS_LOCK:
        _DELIVERED_SCAN_KEYS.add(_delivered_scan_key(order_item_id, code))


def _scan_event_key_parts(item):
    # Для обработчиков исключений: битый payload (не словарь) даёт пустую пару,
    # forget_delivered_scan на ней ничего не делает и не роняет проход повторно.
    payload = item.get("payload") if isinstance(item, dict) else None
    if not isinstance(payload, dict):
        return "", ""
    return payload.get("order_item_id"), payload.get("code")


def _normalize_filter_ids(values, argument_name):
    if values is None:
        return set()
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError(
            f"{argument_name} must be a collection of ids, not {type(values).__name__}: "
            "wrap a single id in a list"
        )
    return {normalize_text(value) for value in values if normalize_text(value)}


def _empty_sync_result(remaining):
    return {
        "synced": 0,
        "failed": 0,
        "remaining": remaining,
        "dropped": 0,
        "blocked": 0,
        "blocked_events": [],
        "enabled": True,
    }


def backend_event_matches_filter(item, order_item_ids, order_ids):
    # Единственный предикат группы: backend_flow.backend_event_matches_group зовёт его,
    # backend_flow сам импортирует backend_events, обратного импорта нет.
    payload = item.get("payload") or {}
    if not isinstance(payload, dict):
        return False
    if item.get("type") == "scan":
        return normalize_text(payload.get("order_item_id")) in order_item_ids
    if item.get("type") == "order_complete":
        return normalize_text(payload.get("order_id")) in order_ids
    return False


def _run_backend_sync_pass(pending, *, background):
    synced = 0
    failed = 0
    dropped = 0
    blocked = 0
    blocked_events = []
    remaining = []
    processed = []
    preempted = False
    for index, item in enumerate(pending):
        if background and _foreground_waiters_count() > 0:
            preempted = True
            break
        processed.append(item)
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
                _remember_delivered_scan(payload.get("order_item_id"), payload.get("code"))
            elif event_type == "order_complete":
                complete_order(payload.get("order_id"))
            else:
                logging.warning("Backend queue: unknown event type skipped: %s", event_type)
            synced += 1
        except BackendApiError as exc:
            key_item_id, key_code = _scan_event_key_parts(item)
            if item.get("type") == "scan" and is_duplicate_scan_ack(exc):
                synced += 1
                _remember_delivered_scan(key_item_id, key_code)
                continue
            if item.get("type") == "scan" and is_non_retryable_scan_conflict(exc):
                dropped += 1
                blocked += 1
                blocked_item = dict(item)
                blocked_item["attempts"] = int(blocked_item.get("attempts") or 0) + 1
                blocked_item["last_error"] = str(exc)
                blocked_item["last_error_detail"] = backend_error_detail_payload(exc)
                blocked_item["updated_at"] = datetime.now().astimezone().isoformat()
                blocked_events.append(blocked_item)
                forget_delivered_scan(key_item_id, key_code)
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
                blocked_item["last_error_detail"] = backend_error_detail_payload(exc)
                blocked_item["updated_at"] = datetime.now().astimezone().isoformat()
                blocked_events.append(blocked_item)
                logging.warning(
                    "Backend queue: dropped incomplete order_complete event for order %s: %s",
                    (item.get("payload") or {}).get("order_id"),
                    exc,
                )
                continue
            failed += 1
            if item.get("type") == "scan":
                forget_delivered_scan(key_item_id, key_code)
            item["attempts"] = int(item.get("attempts") or 0) + 1
            item["last_error"] = str(exc)
            item["last_error_kind"] = backend_error_kind(exc)
            item["updated_at"] = datetime.now().astimezone().isoformat()
            remaining.append(item)
            if item["last_error_kind"] == "network":
                # Канал лежит для всей очереди сразу: остальные события ждут
                # связи, а не своей порции таймаутов на глазах у оператора.
                # Хвост берётся из списка ЭТОГО прохода (с фильтром это только
                # события фильтра) и идёт и в processed, и в remaining, чтобы
                # сверка записала помеченную версию, а чужие события не тронула.
                tail = mark_untried_tail_as_network(pending[index + 1:])
                processed.extend(tail)
                remaining.extend(tail)
                break
        except Exception as exc:
            failed += 1
            if item.get("type") == "scan":
                forget_delivered_scan(*_scan_event_key_parts(item))
            item["attempts"] = int(item.get("attempts") or 0) + 1
            item["last_error"] = str(exc)
            item["last_error_kind"] = backend_error_kind(exc)
            item["updated_at"] = datetime.now().astimezone().isoformat()
            remaining.append(item)

    current = reconcile_queue_section("pending_backend_events", processed, remaining)
    record_blocked_backend_events(blocked_events)
    result = {
        "synced": synced,
        "failed": failed,
        "remaining": len(current),
        "dropped": dropped,
        "blocked": blocked,
        "blocked_events": blocked_events,
        "enabled": True,
    }
    if preempted:
        result["preempted"] = True
    return result


def sync_pending_backend_events(order_item_ids=None, order_ids=None, *, background=False):
    """Доставить события очереди на backend

    Без фильтра (оба аргумента None) обрабатывается вся очередь
    С фильтром обрабатываются только scan с order_item_id из order_item_ids
    и order_complete с order_id из order_ids, остальные события не трогаются
    Пустая коллекция при активном фильтре значит «ничего не отправлять» для этого
    вида событий: order_item_ids=[] без order_ids не отправит ни одного события
    Строка или bytes вместо коллекции это TypeError, один id передают списком
    background=True: захват без ожидания, занято значит ответ со skipped, и проход
    уступает экрану, если тот ждёт (ответ с preempted)
    """
    filter_active = order_item_ids is not None or order_ids is not None
    normalized_item_ids = _normalize_filter_ids(order_item_ids, "order_item_ids")
    normalized_order_ids = _normalize_filter_ids(order_ids, "order_ids")

    if not backend_configured():
        return {"synced": 0, "failed": 0, "remaining": len(load_pending_backend_events()), "enabled": False}

    if background:
        if not _SYNC_LOCK.acquire(blocking=False):
            skipped = _empty_sync_result(len(load_pending_backend_events()))
            skipped["skipped"] = True
            return skipped
    else:
        _increment_foreground_waiters()
        try:
            _SYNC_LOCK.acquire()
        finally:
            _decrement_foreground_waiters()

    try:
        full_pending = load_pending_backend_events()
        if not full_pending:
            return _empty_sync_result(0)

        if filter_active:
            to_process = [
                item for item in full_pending
                if backend_event_matches_filter(item, normalized_item_ids, normalized_order_ids)
            ]
        else:
            to_process = full_pending

        if not to_process:
            return _empty_sync_result(len(full_pending))

        return _run_backend_sync_pass(to_process, background=background)
    finally:
        _SYNC_LOCK.release()
