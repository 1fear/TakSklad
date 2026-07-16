import hashlib
import json
import logging
import os
from datetime import datetime

from .config import BACKUP_DIR, SKLADBOT_REQUEST_NUMBER_COLUMN
from .orders import get_order_date_value
from .storage import (
    append_queue_item,
    load_data_section,
    mutate_queue_section,
    save_data_section,
)


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
