import hashlib
import json
import logging
import os
from datetime import datetime

from .config import BACKUP_DIR, SHEET_NAME, SKLADBOT_REQUEST_NUMBER_COLUMN, SPREADSHEET_ID
from .orders import get_order_date_value
from .sheets import get_google_client, update_scanned_codes_to_gsheet
from .storage import (
    append_queue_item,
    load_data_section,
    mutate_queue_section,
    reconcile_queue_section,
    save_data_section,
)
from .utils import make_hash, normalize_text, split_codes


def write_scan_backup(action, order, code=None, codes=None):
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        filename = os.path.join(BACKUP_DIR, f"scan_backup_{datetime.now().strftime('%d.%m.%Y')}.jsonl")
        payload = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "row_number": order.get("_row_number"),
            "date": get_order_date_value(order) or "",
            "client": order.get("Клиент", ""),
            "representative": order.get("Торговый представитель", ""),
            "address": order.get("Адрес", ""),
            "product": order.get("Товары", ""),
            "payment_type": order.get("Тип оплаты", ""),
            "skladbot_request_number": order.get(SKLADBOT_REQUEST_NUMBER_COLUMN, ""),
            "code": code,
            "codes": codes or [],
        }
        with open(filename, "a", encoding="utf-8") as backup_file:
            backup_file.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return True
    except Exception:
        logging.exception("Не удалось записать локальный backup")
        return False


def load_pending_prints():
    data = load_data_section("pending_prints", [])
    return data if isinstance(data, list) else []


def save_pending_prints(items):
    return save_data_section("pending_prints", items)


def make_pending_print_id(address, products):
    payload = {
        "address": address,
        "products": [
            {
                "client": product.get("Клиент", ""),
                "address": product.get("Адрес", ""),
                "product": product.get("Товары", ""),
                "codes": product.get("Коды", []),
            }
            for product in products
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def add_pending_print(address, products):
    pending_id = make_pending_print_id(address, products)
    try:
        append_queue_item("pending_prints", {
            "id": pending_id,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "address": address,
            "products": products,
        })
    except Exception:
        logging.exception("Не удалось поставить сводный лист в durable очередь печати")
        return ""
    return pending_id


def remove_pending_print(pending_id):
    if not pending_id:
        return False
    removed = {"value": False}

    def remove(items):
        result = [item for item in items if item.get("id") != pending_id]
        removed["value"] = len(result) != len(items)
        return result

    try:
        mutate_queue_section("pending_prints", remove)
    except Exception:
        logging.exception("Не удалось удалить сводный лист из durable очереди печати")
        return False
    return removed["value"]


def load_pending_saves():
    data = load_data_section("pending_saves", [])
    return data if isinstance(data, list) else []


def save_pending_saves(items):
    return save_data_section("pending_saves", items)


def make_pending_save_id(order, scanned_codes):
    return make_hash({
        "order_id": order.get("ID заказа", ""),
        "row_number": order.get("_row_number", ""),
        "date": get_order_date_value(order) or "",
        "client": order.get("Клиент", ""),
        "address": order.get("Адрес", ""),
        "product": order.get("Товары", ""),
        "codes": scanned_codes,
    })


def add_pending_save(order, scanned_codes, reason):
    pending_id = make_pending_save_id(order, scanned_codes)
    item = {
        "id": pending_id,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "order": {key: value for key, value in order.items() if not key.startswith("_existing")},
        "codes": scanned_codes,
        "last_error": reason,
    }

    def update_existing(existing):
        existing["last_error"] = reason
        existing["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return existing

    try:
        append_queue_item("pending_saves", item, update_existing=update_existing)
    except Exception:
        logging.exception("Не удалось поставить сканы в durable очередь Google Sheets")
        return ""
    return pending_id


def remove_pending_save(pending_id):
    mutate_queue_section(
        "pending_saves",
        lambda items: [item for item in items if item.get("id") != pending_id],
    )


def order_matches_pending_save(order, pending_order):
    for field in ("ID импорта", "ID заказа", "_row_number"):
        left = normalize_text(order.get(field))
        right = normalize_text(pending_order.get(field))
        if left and right and left == right:
            return True

    checks = []
    for field in ("Дата отгрузки", "Дата заказа", "Тип оплаты", "Клиент", "Адрес", "Товары"):
        left = normalize_text(order.get(field))
        right = normalize_text(pending_order.get(field))
        if left or right:
            checks.append(left == right)
    return bool(checks) and all(checks)


def update_pending_save_codes_for_undo(order, previous_codes, remaining_codes, reason):
    previous_codes = split_codes("\n".join(previous_codes))
    remaining_codes = split_codes("\n".join(remaining_codes))
    previous_id = make_pending_save_id(order, previous_codes)
    changed = {"value": False}

    def update(items):
        result = []
        for item in items:
            pending_order = item.get("order", {})
            item_codes = split_codes("\n".join(item.get("codes", [])))
            is_target = item.get("id") == previous_id or (
                item_codes == previous_codes and order_matches_pending_save(order, pending_order)
            )
            if not is_target:
                result.append(item)
                continue
            changed["value"] = True
            if remaining_codes:
                item = dict(item)
                item["id"] = make_pending_save_id(order, remaining_codes)
                item["codes"] = remaining_codes
                item["last_error"] = reason
                item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                result.append(item)
        return result

    mutate_queue_section("pending_saves", update)
    return changed["value"]


def get_pending_codes():
    codes = set()
    for item in load_pending_saves():
        for code in item.get("codes", []):
            if code:
                codes.add(code)
    return codes


def is_retryable_save_error(message):
    text = normalize_text(message).lower()
    non_retryable = [
        "повтор",
        "другой строке",
        "не найдена строка",
        "обязательные колонки",
        "уже есть другие",
        "лист google sheets пустой",
        "нет отсканированных кодов",
    ]
    if any(marker in text for marker in non_retryable):
        return False

    retryable = [
        "timeout",
        "timed out",
        "connection",
        "network",
        "temporary",
        "ssl",
        "socket",
        "503",
        "502",
        "500",
        "429",
        "quota",
        "unavailable",
        "service",
        "broken pipe",
    ]
    return any(marker in text for marker in retryable) or bool(text)


def sync_pending_saves(sheet=None):
    pending = load_pending_saves()
    if not pending:
        return {"synced": 0, "failed": 0, "remaining": 0}

    if sheet is None:
        client = get_google_client()
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)

    synced = 0
    failed = 0
    dropped = 0
    remaining = []
    for item in pending:
        order = item.get("order", {})
        codes = item.get("codes", [])
        ok, message = update_scanned_codes_to_gsheet(sheet, order, codes)
        if ok:
            synced += 1
            write_scan_backup("pending_save_synced", order, codes=codes)
            continue

        if not is_retryable_save_error(message):
            dropped += 1
            logging.warning("Очередь Google Sheets: удалена неретрабельная запись: %s", message)
            write_scan_backup("pending_save_dropped", order, codes=codes)
            continue

        failed += 1
        item["last_error"] = message
        item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        remaining.append(item)

    current = reconcile_queue_section("pending_saves", pending, remaining)
    return {"synced": synced, "failed": failed, "remaining": len(current), "dropped": dropped}
