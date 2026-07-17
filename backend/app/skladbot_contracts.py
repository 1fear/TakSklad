"""Pure SkladBot value mapping and matching contracts.

This module deliberately has no dependency on order services or worker
orchestration.  API clients and queue processors can share these helpers
without creating a worker/service import cycle.
"""

import hashlib
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .settings import load_settings


NOISE_COMPANY_TOKENS = {"ooo", "ооо", "mchj", "мчж", "ip", "ип", "sp", "сп", "склад", "склади"}
NOISE_PRODUCT_TOKENS = {"uz", "kingsize", "king", "size", "superslim", "super", "slim"}
RETURN_REQUEST_TOKENS = {"возврат", "возврата", "return", "returned"}
PRODUCT_COLORS = ("brown", "red", "gold", "green")
PRODUCT_FORMATS = ("op", "ssl")
SMARTUP_ID_KEYS = (
    "smartup_id",
    "smartupId",
    "smartup_deal_id",
    "smartupDealId",
    "Smartup deal_id",
    "Smartup ID",
    "ID Smartup",
    "ID заявки Smartup",
)
SMARTUP_COMMENT_ID_RE = re.compile(
    r"(?im)^\s*(?:smartup(?:\s+deal[_ ]?id|\s+id)?|id\s+smartup|id\s+заявки\s+smartup)\s*[:#-]\s*([^\s;]+)\s*$"
)
INTERNAL_SMARTUP_SOURCE_ID_RE = re.compile(r"(?i)^smartup:([1-9][0-9]{0,39})$")
CANONICAL_REMOTE_REQUEST_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
CANONICAL_SKLADBOT_REQUEST_NUMBER_RE = re.compile(
    r"^(?:WH-R|WR)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$"
)
TAKSKLAD_MARKER_RE = re.compile(r"(?im)^\s*TakSklad\s+ref:\s*(TSF-[A-F0-9]{24})\s*$")


def normalize_text(value):
    return str(value or "").strip()


def build_taksklad_marker(reference):
    """Build a stable, non-sensitive marker for exact create reconciliation."""
    normalized = normalize_text(reference)
    if not normalized:
        return ""
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24].upper()
    return f"TakSklad ref: TSF-{digest}"


def taksklad_marker_from_comment(comment):
    match = TAKSKLAD_MARKER_RE.search(normalize_text(comment))
    return f"TakSklad ref: {match.group(1).upper()}" if match else ""


def request_has_exact_taksklad_marker(request, marker):
    expected = taksklad_marker_from_comment(marker)
    actual = taksklad_marker_from_comment((request or {}).get("comment"))
    return bool(expected and actual and expected == actual)


def is_stock_shortage_text(value):
    text = normalize_text(value).casefold().replace("ё", "е")
    if not text:
        return False
    direct_phrases = (
        "не хватает",
        "не хватило",
        "недостаточно",
        "insufficient stock",
        "not enough stock",
        "not enough quantity",
        "not enough products",
    )
    if any(phrase in text for phrase in direct_phrases):
        return True
    if "недостат" in text and any(word in text for word in ("товар", "остат", "склад", "количеств")):
        return True
    return "остат" in text and "меньш" in text and any(
        word in text for word in ("товар", "количеств", "заявк")
    )


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


def normalized_token_set(value, noise_tokens=None):
    return set(simplify_tokens(value, noise_tokens or set()))


def client_matches(left, right):
    left_tokens = normalized_token_set(left, NOISE_COMPANY_TOKENS)
    right_tokens = normalized_token_set(right, NOISE_COMPANY_TOKENS)
    if not left_tokens or not right_tokens:
        return False
    return left_tokens == right_tokens


def product_sku_key(value):
    text = normalize_lookup_text(value)
    tokens = text.split()
    compact = "".join(tokens)
    color = next((item for item in PRODUCT_COLORS if item in tokens or item in compact), "")
    product_format = next((
        item
        for item in PRODUCT_FORMATS
        if item in tokens or (color and f"{color}{item}" in compact)
    ), "")
    if color and product_format:
        return f"{color}:{product_format}"
    return ""


def product_matches(left, right):
    left_key = product_sku_key(left)
    right_key = product_sku_key(right)
    if left_key and right_key:
        return left_key == right_key
    return text_tokens_match(left, right, NOISE_PRODUCT_TOKENS, min_overlap=0.8)


def request_type_matches(value):
    expected = normalize_lookup_text(os.environ.get("SKLADBOT_REQUEST_TYPE_NAME") or "3PL отгрузка")
    actual = normalize_lookup_text(value)
    if normalized_token_set(actual).intersection(RETURN_REQUEST_TOKENS):
        return False
    if expected and expected == actual:
        return True
    return "3pl" in actual and "отгруз" in actual


def address_soft_match(left, right):
    if not normalize_text(left) or not normalize_text(right):
        return False
    return text_tokens_match(left, right, min_overlap=0.55)


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
    parsed_datetime = parse_datetime_value(text)
    if parsed_datetime is not None:
        if parsed_datetime.tzinfo is None:
            parsed_datetime = parsed_datetime.replace(tzinfo=business_timezone())
        return parsed_datetime.astimezone(business_timezone()).date()
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


def parse_datetime_value(value):
    text = normalize_text(value)
    if not text:
        return None
    has_time = "T" in text or (":" in text and " " in text)
    if not has_time:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in (
        "%d.%m.%Y %H:%M:%S%z",
        "%d.%m.%Y %H:%M%z",
        "%d/%m/%Y %H:%M:%S%z",
        "%d/%m/%Y %H:%M%z",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def business_timezone():
    timezone_name = load_settings().timezone
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Tashkent")


def business_today(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(business_timezone()).date()


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


def request_value(detail, list_item, *keys):
    for key in keys:
        value = detail.get(key) if isinstance(detail, dict) else None
        if value not in (None, ""):
            return value
        value = request_list_value(list_item, key) if isinstance(list_item, dict) else ""
        if value not in (None, ""):
            return value
    return ""


def normalize_smartup_id(value, *, explicit=False):
    text = normalize_text(value)
    if not text:
        return ""
    if text.startswith("smartup:"):
        parts = text.split(":")
        return ":".join(parts[:2]) if len(parts) >= 2 and parts[1] else text
    if explicit:
        return f"smartup:{text}"
    return ""


def internal_smartup_id_from_source(value):
    """Return the numeric Smartup deal id from TakSklad's canonical order source."""
    match = INTERNAL_SMARTUP_SOURCE_ID_RE.fullmatch(normalize_text(value))
    return match.group(1) if match else ""


def internal_smartup_ids_from_sources(values) -> tuple[str, ...]:
    ids = {
        smartup_id
        for value in values
        if (smartup_id := internal_smartup_id_from_source(value))
    }
    return tuple(sorted(ids, key=lambda value: (len(value), value)))


def format_internal_smartup_ids(values) -> str:
    return ", ".join(internal_smartup_ids_from_sources(values))


def canonical_remote_request_id(value):
    """Return a bounded positive SkladBot id, or fail closed for display/correlation."""
    text = normalize_text(value)
    return text if CANONICAL_REMOTE_REQUEST_ID_RE.fullmatch(text) else ""


def canonical_skladbot_request_number(value):
    """Return a bounded canonical WH-R/WR number, or fail closed."""
    text = normalize_text(value)
    if not text or len(text) > 80:
        return ""
    return text if CANONICAL_SKLADBOT_REQUEST_NUMBER_RE.fullmatch(text) else ""


def canonical_skladbot_request_link(request_id, request_number):
    """Return one complete canonical SkladBot link pair or two empty values."""
    canonical_id = canonical_remote_request_id(request_id)
    canonical_number = canonical_skladbot_request_number(request_number)
    if not canonical_id or not canonical_number:
        return "", ""
    return canonical_id, canonical_number


def canonical_skladbot_request_evidence_link(
    request,
    *,
    allow_missing_raw=False,
    allow_single_raw_side=False,
):
    """Validate one normalized link against its strict list/detail evidence."""
    request = request if isinstance(request, dict) else {}
    canonical_id, canonical_number = canonical_skladbot_request_link(
        request.get("id"),
        request.get("number"),
    )
    if not canonical_id or not canonical_number:
        return "", ""

    raw = request.get("raw")
    if not isinstance(raw, dict) or not ({"list", "detail"} & set(raw)):
        return (canonical_id, canonical_number) if allow_missing_raw else ("", "")

    list_present = "list" in raw
    detail_present = "detail" in raw
    list_item = raw.get("list") if isinstance(raw.get("list"), dict) else {}
    detail = raw.get("detail") if isinstance(raw.get("detail"), dict) else {}
    list_id = canonical_remote_request_id(request_list_value(list_item, "id"))
    detail_id = canonical_remote_request_id(detail.get("id"))
    list_number_value = request_list_value(list_item, "delivery_number", "number")
    detail_number_value = detail.get("delivery_number") or detail.get("number")
    list_number_text = normalize_text(list_number_value)
    detail_number_text = normalize_text(detail_number_value)
    list_number = canonical_skladbot_request_number(list_number_value)
    detail_number = canonical_skladbot_request_number(detail_number_value)

    if (list_number_text and not list_number) or (detail_number_text and not detail_number):
        return "", ""

    if list_present and detail_present:
        if not list_id or not detail_id or list_id != detail_id or detail_id != canonical_id:
            return "", ""
        evidence_numbers = {number for number in (list_number, detail_number) if number}
        if evidence_numbers != {canonical_number}:
            return "", ""
        return canonical_id, canonical_number
    if not allow_single_raw_side:
        return "", ""
    if list_present and list_id == canonical_id and list_number == canonical_number:
        return canonical_id, canonical_number
    if detail_present and detail_id == canonical_id and detail_number == canonical_number:
        return canonical_id, canonical_number
    return "", ""


def smartup_id_from_comment(comment):
    text = normalize_text(comment)
    if not text:
        return ""
    match = SMARTUP_COMMENT_ID_RE.search(text)
    if not match:
        return ""
    return normalize_smartup_id(match.group(1), explicit=True)


def request_smartup_id(list_item, detail, fields):
    explicit = normalize_smartup_id(request_value(detail, list_item, *SMARTUP_ID_KEYS), explicit=True)
    if explicit:
        return explicit
    field_value = normalize_smartup_id(get_field(fields, *SMARTUP_ID_KEYS), explicit=True)
    if field_value:
        return field_value
    return smartup_id_from_comment(
        request_value(detail, list_item, "comment", "commentary")
        or get_field(fields, "comment", "Комментарий")
    )


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
        "updated_at": normalize_text(detail.get("updatedAt") or request_list_value(list_item, "updated_at")),
        "completed_at": normalize_text(request_value(
            detail,
            list_item,
            "completedAt",
            "completed_at",
            "closedAt",
            "closed_at",
            "doneAt",
            "done_at",
            "finishedAt",
            "finished_at",
            "processedAt",
            "processed_at",
            "acceptedAt",
            "accepted_at",
        )),
        "archived_at": normalize_text(request_value(
            detail,
            list_item,
            "archivedAt",
            "archived_at",
            "archiveAt",
            "archive_at",
        )),
        "unloading_date": normalize_text(get_field(fields, "unloading_date", "Дата выгрузки")),
        "recipient": normalize_text(get_field(fields, "company_name", "Название компании/Имя человека") or detail.get("company_name")),
        "address": normalize_text(get_field(fields, "address", "Адрес") or detail.get("address") or logistic.get("address")),
        "comment": normalize_text(detail.get("comment") or get_field(fields, "comment", "Комментарий")),
        "smartup_id": request_smartup_id(list_item, detail, fields),
        "products": [
            {
                "name": normalize_text(product.get("name")),
                "vendor_code": normalize_text(product.get("vendorCode") or product.get("vendor_code")),
                "barcode": normalize_text(product.get("barcode")),
                "amount": parse_int(product.get("amount")),
                "accepted_amount": parse_int(product.get("acceptedAmount") or product.get("accepted_amount")),
                "accepted_amount_present": "acceptedAmount" in product or "accepted_amount" in product,
            }
            for product in products
            if isinstance(product, dict)
        ],
        "raw": {"list": list_item, "detail": detail},
    }


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
    return request_match_diagnostics(order, request)["matched"]


def nearest_request_diagnostics(order, requests, limit=3):
    diagnostics = []
    for request in requests:
        diagnostic = request_match_diagnostics(order, request)
        checks = diagnostic.get("checks") or {}
        diagnostics.append((
            diagnostic.get("score", 0),
            {
                "id": request.get("id"),
                "number": request.get("number") or "",
                "unloading_date": request.get("unloading_date") or "",
                "recipient": request.get("recipient") or "",
                "payment": normalize_payment_type(request.get("comment")),
                "score": diagnostic.get("score", 0),
                "matched": diagnostic.get("matched", False),
                "failed_checks": [name for name, ok in checks.items() if not ok],
                "products": diagnostic.get("products") or [],
            },
        ))
    diagnostics.sort(key=lambda item: (item[0], item[1].get("matched")), reverse=True)
    return [payload for _score, payload in diagnostics[:max(1, int(limit or 3))]]


def request_match_diagnostics(order, request):
    group = order_group_payload(order)
    request_date = parse_date(request.get("unloading_date"))
    date_ok = bool(order.order_date and request_date and order.order_date == request_date)
    client_ok = client_matches(group["client"], request.get("recipient"))
    payment_ok = normalize_payment_type(group["payment"]) == normalize_payment_type(request.get("comment"))
    address_soft_ok = address_soft_match(group["address"], request.get("address"))

    request_products = list(request.get("products") or [])
    used_indexes = set()
    product_results = []
    for order_product in group["products"]:
        matched_index = None
        matched_request_product = None
        for index, request_product in enumerate(request_products):
            if index in used_indexes:
                continue
            if request_product.get("amount") != order_product.get("blocks"):
                continue
            if any(
                product_matches(order_product["name"], candidate)
                for candidate in (request_product.get("name"), request_product.get("vendor_code"), request_product.get("barcode"))
                if candidate
            ):
                matched_index = index
                matched_request_product = request_product
                break
        if matched_index is not None:
            used_indexes.add(matched_index)
        product_results.append({
            "order_product": order_product.get("name"),
            "order_blocks": order_product.get("blocks"),
            "matched": matched_index is not None,
            "request_product": (matched_request_product or {}).get("name") or "",
            "request_blocks": (matched_request_product or {}).get("amount") or 0,
        })
    products_ok = bool(product_results) and all(item["matched"] for item in product_results)
    checks = {
        "date": date_ok,
        "client": client_ok,
        "payment": payment_ok,
        "products": products_ok,
    }
    score = sum(1 for ok in checks.values() if ok)
    return {
        "matched": all(checks.values()),
        "score": score,
        "checks": checks,
        "address_soft_match": address_soft_ok,
        "products": product_results,
        "extra_request_products": max(0, len(request_products) - len(used_indexes)),
    }
