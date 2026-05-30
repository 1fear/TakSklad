import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .db import SessionLocal
from .models import AuditLog, Order, OrderItem
from .orders_service import COMPLETED_STATUSES


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

NOISE_COMPANY_TOKENS = {"ooo", "ооо", "mchj", "мчж", "ip", "ип", "sp", "сп"}
NOISE_PRODUCT_TOKENS = {"uz", "kingsize", "king", "size", "superslim", "super", "slim"}


def env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def normalize_text(value):
    return str(value or "").strip()


def normalize_lookup_text(value):
    text = normalize_text(value).lower().replace("ё", "е")
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def simplify_tokens(value, noise_tokens=None):
    noise_tokens = noise_tokens or set()
    return [token for token in normalize_lookup_text(value).split() if token and token not in noise_tokens]


def text_tokens_match(left, right, noise_tokens=None, min_overlap=0.75):
    left_tokens = set(simplify_tokens(left, noise_tokens))
    right_tokens = set(simplify_tokens(right, noise_tokens))
    if not left_tokens or not right_tokens:
        return False
    shorter, longer = (left_tokens, right_tokens) if len(left_tokens) <= len(right_tokens) else (right_tokens, left_tokens)
    return len(shorter.intersection(longer)) / max(1, len(shorter)) >= min_overlap


def normalize_payment_type(value):
    text = normalize_lookup_text(value)
    if "терминал" in text:
        return "terminal"
    if "перечис" in text or "безнал" in text:
        return "transfer"
    return "unknown"


def parse_int(value):
    text = normalize_text(value).replace(" ", "").replace(",", ".")
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return normalize_lookup_text(value) in {"1", "true", "yes", "да"}


def parse_date(value):
    text = normalize_text(value)
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    if " " in text:
        text = text.split(" ", 1)[0]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def date_in_window(value, today=None, lookback_days=1):
    today = today or datetime.now().date()
    parsed = parse_date(value)
    if not parsed:
        return False
    return today - timedelta(days=lookback_days) <= parsed <= today


def extract_list_items(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("items", "data", "requests", "result"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = extract_list_items(value)
            if nested:
                return nested
    return []


def field_map(detail):
    result = {}
    for item in detail.get("fields", []) if isinstance(detail, dict) else []:
        if not isinstance(item, dict):
            continue
        value = normalize_text(item.get("value"))
        for key in (item.get("field"), item.get("name")):
            normalized = normalize_lookup_text(key)
            if normalized:
                result[normalized] = value
    return result


def get_field(fields, *names):
    for name in names:
        value = fields.get(normalize_lookup_text(name))
        if value:
            return value
    return ""


def request_list_value(item, *keys):
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return ""


class SkladBotClient:
    def __init__(self):
        self.token = normalize_text(os.environ.get("SKLADBOT_API_TOKEN"))
        self.base_url = normalize_text(os.environ.get("SKLADBOT_API_BASE_URL")) or "https://api.skladbot.ru/v1"
        self.base_url = self.base_url.rstrip("/")
        self.timeout = env_int("SKLADBOT_API_TIMEOUT_SECONDS", 8)
        self.customer_id = env_int("SKLADBOT_CUSTOMER_ID", 6211)
        self.shipment_type_id = env_int("SKLADBOT_SHIPMENT_TYPE_ID", 3389)
        self.limit = env_int("SKLADBOT_REQUESTS_LIMIT", 500)

    @property
    def configured(self):
        return bool(self.token)

    def get(self, path, params=None):
        if not self.token:
            raise RuntimeError("SKLADBOT_API_TOKEN is not configured")
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/{path.lstrip('/')}",
                params=params or {},
                headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            )
            response.raise_for_status()
            return response.json()

    def list_requests(self):
        return extract_list_items(self.get("/requests", {
            "customer_id": self.customer_id,
            "type_id": self.shipment_type_id,
            "limit": self.limit,
        }))

    def get_request_detail(self, request_id):
        payload = self.get(f"/requests/show/{request_id}")
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload


def normalize_request_payload(list_item, detail):
    detail = detail if isinstance(detail, dict) else {}
    fields = field_map(detail)
    customer = detail.get("customer") if isinstance(detail.get("customer"), dict) else {}
    logistic = detail.get("logistic") if isinstance(detail.get("logistic"), dict) else {}
    products = detail.get("products") if isinstance(detail.get("products"), list) else []
    return {
        "id": parse_int(detail.get("id")) or parse_int(request_list_value(list_item, "id")),
        "number": normalize_text(detail.get("delivery_number") or request_list_value(list_item, "delivery_number", "number")),
        "customer_name": normalize_text(customer.get("name") or request_list_value(list_item, "customer")),
        "type": normalize_text(detail.get("type") or request_list_value(list_item, "type")),
        "is_completed": parse_bool(detail.get("isCompleted") or detail.get("is_completed") or request_list_value(list_item, "is_completed")),
        "archived": parse_bool(detail.get("archived") or request_list_value(list_item, "archived")),
        "created_at": normalize_text(detail.get("createdAt") or request_list_value(list_item, "created_at")),
        "unloading_date": normalize_text(get_field(fields, "unloading_date", "Дата выгрузки")),
        "recipient": normalize_text(get_field(fields, "company_name", "Название компании/Имя человека") or detail.get("company_name")),
        "address": normalize_text(get_field(fields, "address", "Адрес") or detail.get("address") or logistic.get("address")),
        "comment": normalize_text(detail.get("comment") or get_field(fields, "comment", "Комментарий")),
        "products": [
            {
                "name": normalize_text(product.get("name")),
                "vendor_code": normalize_text(product.get("vendorCode") or product.get("vendor_code")),
                "barcode": normalize_text(product.get("barcode")),
                "amount": parse_int(product.get("amount")),
            }
            for product in products
            if isinstance(product, dict)
        ],
        "raw": {"list": list_item, "detail": detail},
    }


def fetch_candidate_requests(today=None):
    client = SkladBotClient()
    if not client.configured:
        logging.info("SkladBot worker disabled: SKLADBOT_API_TOKEN is not configured")
        return []

    lookback_days = env_int("SKLADBOT_SYNC_LOOKBACK_DAYS", 1)
    result = []
    for item in client.list_requests():
        if not date_in_window(request_list_value(item, "created_at", "createdAt", "date"), today=today, lookback_days=lookback_days):
            continue
        request_id = parse_int(request_list_value(item, "id"))
        if request_id <= 0:
            continue
        detail = client.get_request_detail(request_id)
        request = normalize_request_payload(item, detail)
        if not date_in_window(request.get("unloading_date") or request.get("created_at"), today=today, lookback_days=lookback_days):
            continue
        result.append(request)
    return result


def order_group_payload(order):
    return {
        "date": order.order_date.isoformat() if order.order_date else "",
        "payment": order.payment_type,
        "client": order.client,
        "address": order.address,
        "products": [
            {"name": item.product, "blocks": item.quantity_blocks}
            for item in order.items
        ],
    }


def request_matches_order(order, request):
    group = order_group_payload(order)
    request_date = parse_date(request.get("unloading_date"))
    if not order.order_date or not request_date or order.order_date != request_date:
        return False
    if normalize_lookup_text(group["client"]) != normalize_lookup_text(request.get("recipient")):
        return False
    if normalize_payment_type(group["payment"]) != normalize_payment_type(request.get("comment")):
        return False
    if not text_tokens_match(group["address"], request.get("address"), min_overlap=0.55):
        return False

    request_products = list(request.get("products") or [])
    used_indexes = set()
    for order_product in group["products"]:
        matched_index = None
        for index, request_product in enumerate(request_products):
            if index in used_indexes:
                continue
            if request_product.get("amount") != order_product.get("blocks"):
                continue
            if any(
                text_tokens_match(order_product["name"], candidate, NOISE_PRODUCT_TOKENS, min_overlap=0.8)
                for candidate in (request_product.get("name"), request_product.get("vendor_code"), request_product.get("barcode"))
                if candidate
            ):
                matched_index = index
                break
        if matched_index is None:
            return False
        used_indexes.add(matched_index)
    return len(used_indexes) == len(request_products)


def update_orders_from_skladbot():
    requests = fetch_candidate_requests()
    checked_at = datetime.now(timezone.utc).isoformat()
    updated = 0
    matched = 0
    not_found = 0
    multiple = 0

    with SessionLocal() as db:
        orders = db.execute(
            select(Order)
            .options(selectinload(Order.items))
            .where(~Order.status.in_(COMPLETED_STATUSES))
        ).scalars().all()

        for order in orders:
            matches = [request for request in requests if request_matches_order(order, request)]
            raw_payload = dict(order.raw_payload or {})
            raw_payload["skladbot_checked_at"] = checked_at
            if len(matches) == 1:
                request = matches[0]
                raw_payload["skladbot_request_number"] = request.get("number") or ""
                raw_payload["skladbot_request_id"] = str(request.get("id") or "")
                raw_payload["skladbot_status"] = "found"
                raw_payload["skladbot_raw"] = request.get("raw") or {}
                matched += 1
            elif len(matches) > 1:
                raw_payload["skladbot_status"] = "multiple"
                raw_payload["skladbot_candidates"] = [
                    {"id": request.get("id"), "number": request.get("number")}
                    for request in matches[:10]
                ]
                multiple += 1
            else:
                raw_payload["skladbot_status"] = "not_found"
                not_found += 1
            order.raw_payload = raw_payload
            updated += 1

        db.add(AuditLog(
            action="skladbot_worker_sync",
            entity_type="skladbot",
            entity_id="worker",
            payload={
                "requests": len(requests),
                "orders_checked": len(orders),
                "updated": updated,
                "matched": matched,
                "not_found": not_found,
                "multiple": multiple,
            },
        ))
        db.commit()

    logging.info(
        "SkladBot worker: requests=%s orders=%s matched=%s not_found=%s multiple=%s",
        len(requests),
        updated,
        matched,
        not_found,
        multiple,
    )
    return {"requests": len(requests), "updated": updated, "matched": matched, "not_found": not_found, "multiple": multiple}


def main():
    interval = env_int("SKLADBOT_WORKER_INTERVAL_SECONDS", 600)
    once = normalize_lookup_text(os.environ.get("SKLADBOT_WORKER_ONCE")) in {"1", "true", "yes", "да"}
    while True:
        try:
            update_orders_from_skladbot()
        except Exception:
            logging.exception("SkladBot worker failed")
        if once:
            return
        time.sleep(max(60, interval))


if __name__ == "__main__":
    main()
