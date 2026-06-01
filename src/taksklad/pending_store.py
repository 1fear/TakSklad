import hashlib
import json
import logging
import os
from datetime import datetime

from .config import BACKUP_DIR, SHEET_NAME, SKLADBOT_REQUEST_NUMBER_COLUMN, SPREADSHEET_ID
from .orders import get_order_date_value
from .sheets import get_google_client, update_scanned_codes_to_gsheet
from .storage import load_data_section, save_data_section
from .utils import make_hash, normalize_text


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
    pending = load_pending_prints()
    pending_id = make_pending_print_id(address, products)
    for item in pending:
        if item.get("id") == pending_id:
            return pending_id

    pending.append({
        "id": pending_id,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "address": address,
        "products": products,
    })
    save_pending_prints(pending)
    return pending_id


def remove_pending_print(pending_id):
    pending = load_pending_prints()
    new_pending = [item for item in pending if item.get("id") != pending_id]
    if len(new_pending) != len(pending):
        save_pending_prints(new_pending)


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
    pending = load_pending_saves()
    pending_id = make_pending_save_id(order, scanned_codes)
    for item in pending:
        if item.get("id") == pending_id:
            item["last_error"] = reason
            item["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_pending_saves(pending)
            return pending_id

    pending.append({
        "id": pending_id,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "order": {key: value for key, value in order.items() if not key.startswith("_existing")},
        "codes": scanned_codes,
        "last_error": reason,
    })
    save_pending_saves(pending)
    return pending_id


def remove_pending_save(pending_id):
    pending = load_pending_saves()
    new_pending = [item for item in pending if item.get("id") != pending_id]
    if len(new_pending) != len(pending):
        save_pending_saves(new_pending)


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

    save_pending_saves(remaining)
    return {"synced": synced, "failed": failed, "remaining": len(remaining), "dropped": dropped}
