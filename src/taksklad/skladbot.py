import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import certifi

from .config import (
    SKLADBOT_API_BASE_URL,
    SKLADBOT_API_TIMEOUT_SECONDS,
    SKLADBOT_COMPLETED_DETAIL_LIMIT,
    SKLADBOT_COMPLETED_LOOKBACK_DAYS,
    SKLADBOT_CUSTOMER_ID,
    SKLADBOT_CUSTOMER_NAME,
    SKLADBOT_REQUEST_DELAY_SECONDS,
    SKLADBOT_REQUESTS_LIMIT,
    SKLADBOT_SHIPMENT_TYPE_ID,
    SKLADBOT_SHIPMENT_TYPE_NAME,
    SKLADBOT_SYNC_LOOKBACK_DAYS,
)
from .storage import load_data_section
from .utils import normalize_lookup_text, normalize_payment_type, normalize_text, parse_date_to_standard, parse_int_value


NOISE_PRODUCT_TOKENS = {
    "uz",
    "kingsize",
    "king",
    "size",
    "superslim",
    "super",
    "slim",
}

PRODUCT_COLORS = ("brown", "red", "gold")
PRODUCT_FORMATS = ("op", "ssl")
RETURN_REQUEST_TOKENS = {"возврат", "возврата", "return", "returned"}

NOISE_COMPANY_TOKENS = {
    "ooo",
    "мчж",
    "mchj",
    "сп",
    "ip",
    "ип",
    "ok",
    "ооо",
}


def load_skladbot_settings():
    settings = {
        "enabled": True,
        "api_token": "",
        "base_url": SKLADBOT_API_BASE_URL,
        "customer_id": SKLADBOT_CUSTOMER_ID,
        "customer_name": SKLADBOT_CUSTOMER_NAME,
        "shipment_type_id": SKLADBOT_SHIPMENT_TYPE_ID,
        "shipment_type_name": SKLADBOT_SHIPMENT_TYPE_NAME,
        "api_timeout_seconds": SKLADBOT_API_TIMEOUT_SECONDS,
        "completed_lookback_days": SKLADBOT_COMPLETED_LOOKBACK_DAYS,
        "sync_lookback_days": SKLADBOT_SYNC_LOOKBACK_DAYS,
        "requests_limit": SKLADBOT_REQUESTS_LIMIT,
        "completed_detail_limit": SKLADBOT_COMPLETED_DETAIL_LIMIT,
        "request_delay_seconds": SKLADBOT_REQUEST_DELAY_SECONDS,
    }

    saved = load_data_section("skladbot_settings", {})
    if isinstance(saved, dict):
        settings.update({key: value for key, value in saved.items() if value is not None})

    env_token = normalize_text(os.environ.get("SKLADBOT_API_TOKEN"))
    if env_token:
        settings["api_token"] = env_token

    settings["api_token"] = normalize_text(
        settings.get("api_token")
        or settings.get("token")
        or settings.get("bearer_token")
    )
    settings["base_url"] = normalize_text(settings.get("base_url")) or SKLADBOT_API_BASE_URL
    settings["enabled"] = bool(settings.get("enabled") and settings["api_token"])
    settings["customer_id"] = parse_int_value(settings.get("customer_id")) or SKLADBOT_CUSTOMER_ID
    settings["shipment_type_id"] = parse_int_value(settings.get("shipment_type_id")) or SKLADBOT_SHIPMENT_TYPE_ID
    settings["api_timeout_seconds"] = max(
        3,
        parse_int_value(settings.get("api_timeout_seconds")) or SKLADBOT_API_TIMEOUT_SECONDS,
    )
    settings["requests_limit"] = max(
        parse_int_value(settings.get("requests_limit")),
        SKLADBOT_REQUESTS_LIMIT,
    )
    settings["completed_detail_limit"] = (
        parse_int_value(settings.get("completed_detail_limit"))
        or SKLADBOT_COMPLETED_DETAIL_LIMIT
    )
    settings["completed_lookback_days"] = (
        parse_int_value(settings.get("completed_lookback_days"))
        or SKLADBOT_COMPLETED_LOOKBACK_DAYS
    )
    settings["sync_lookback_days"] = max(
        0,
        parse_int_value(settings.get("sync_lookback_days")) or SKLADBOT_SYNC_LOOKBACK_DAYS,
    )
    try:
        request_delay = float(str(settings.get("request_delay_seconds", "")).replace(",", "."))
    except ValueError:
        request_delay = SKLADBOT_REQUEST_DELAY_SECONDS
    settings["request_delay_seconds"] = max(0.0, min(request_delay, SKLADBOT_REQUEST_DELAY_SECONDS))
    return settings


def skladbot_is_configured(settings=None):
    settings = settings or load_skladbot_settings()
    return bool(settings.get("enabled") and settings.get("api_token"))


class SkladBotError(RuntimeError):
    pass


def format_skladbot_error(exc):
    message = normalize_text(exc)
    lower_message = message.lower()
    if not message:
        return "SkladBot вернул ошибку без подробностей. Повторите синхронизацию позже."
    if "http 401" in lower_message or "http 403" in lower_message or "unauthorized" in lower_message:
        return "SkladBot отклонил API-токен. Проверьте токен в настройках SkladBot."
    if "http 429" in lower_message or "rate limit" in lower_message or "quota" in lower_message:
        return "SkladBot временно ограничил запросы. Номера заявок подтянутся при следующей фоновой синхронизации."
    if (
        "timed out" in lower_message
        or "timeout" in lower_message
        or "getaddrinfo failed" in lower_message
        or "failed to resolve" in lower_message
        or "connection" in lower_message
        or "ssl" in lower_message
        or "unreachable" in lower_message
    ):
        return "SkladBot временно недоступен. Список заказов остаётся доступен, номера заявок подтянутся позже."
    if "некорректный json" in lower_message or "invalid json" in lower_message:
        return "SkladBot вернул некорректный ответ. Повторите синхронизацию позже."
    return message


class SkladBotClient:
    def __init__(self, token, base_url=SKLADBOT_API_BASE_URL, timeout=SKLADBOT_API_TIMEOUT_SECONDS, request_delay_seconds=SKLADBOT_REQUEST_DELAY_SECONDS):
        self.token = normalize_text(token)
        self.base_url = normalize_text(base_url).rstrip("/")
        self.timeout = timeout
        self.request_delay_seconds = request_delay_seconds
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    def get(self, path, params=None):
        if not self.token:
            raise SkladBotError("Не указан API-токен SkladBot")

        path = "/" + path.lstrip("/")
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = self.base_url + path + (f"?{query}" if query else "")
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="GET",
        )

        raw = ""
        try:
            for attempt in range(3):
                if self.request_delay_seconds:
                    time.sleep(float(self.request_delay_seconds))
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
                        raw = response.read().decode("utf-8", "replace")
                    break
                except urllib.error.HTTPError as exc:
                    body = exc.read().decode("utf-8", "replace")[:500]
                    if exc.code == 429 and attempt < 2:
                        retry_after = parse_int_value(exc.headers.get("Retry-After"))
                        time.sleep(retry_after or (2 + attempt * 3))
                        continue
                    raise SkladBotError(f"SkladBot HTTP {exc.code}: {body}") from exc
        except SkladBotError:
            raise
        except Exception as exc:
            raise SkladBotError(f"SkladBot API недоступен: {exc}") from exc

        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise SkladBotError("SkladBot вернул некорректный JSON") from exc

    def list_requests(self, customer_id, type_id, limit=SKLADBOT_REQUESTS_LIMIT):
        payload = self.get("/requests", {
            "customer_id": customer_id,
            "type_id": type_id,
            "limit": limit,
        })
        return extract_list_items(payload)

    def get_request_detail(self, request_id):
        payload = self.get(f"/requests/show/{request_id}")
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload


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


def parse_date(value):
    text = normalize_text(value)
    if not text:
        return ""
    if "T" in text:
        text = text.split("T", 1)[0]
    if " " in text:
        text = text.split(" ", 1)[0]
    return parse_date_to_standard(text)


def parse_date_obj(value):
    parsed = parse_date(value)
    if not parsed:
        return None
    try:
        return datetime.strptime(parsed, "%d.%m.%Y").date()
    except ValueError:
        return None


def sync_date_window(today=None, lookback_days=SKLADBOT_SYNC_LOOKBACK_DAYS):
    today = today or datetime.now().date()
    lookback_days = max(0, parse_int_value(lookback_days))
    return today - timedelta(days=lookback_days), today


def date_in_sync_window(value, today=None, lookback_days=SKLADBOT_SYNC_LOOKBACK_DAYS):
    request_date = parse_date_obj(value)
    if not request_date:
        return False
    start_date, end_date = sync_date_window(today=today, lookback_days=lookback_days)
    return start_date <= request_date <= end_date


def list_item_in_sync_window(item, today=None, lookback_days=SKLADBOT_SYNC_LOOKBACK_DAYS):
    return date_in_sync_window(
        request_list_value(item, "created_at", "createdAt", "date"),
        today=today,
        lookback_days=lookback_days,
    )


def request_in_sync_window(request, today=None, lookback_days=SKLADBOT_SYNC_LOOKBACK_DAYS):
    # Для уже подтянутого детали заявки фильтруем по дате отгрузки, а не по
    # дате создания: нам нужны заявки, которые отгружаются в нашем окне,
    # независимо от того, когда они были созданы в SkladBot. Если детали
    # нет unloading_date, откатываемся на created_at как раньше.
    candidate_date = request.get("unloading_date") or request.get("created_at")
    return date_in_sync_window(
        candidate_date,
        today=today,
        lookback_days=lookback_days,
    )


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


def first_text_value(*values):
    for value in values:
        text = normalize_text(value)
        if text:
            return text
    return ""


def first_raw_value(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def request_list_value(item, *keys):
    if not isinstance(item, dict):
        return ""
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return ""


def parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = normalize_lookup_text(value)
    if text in ("1", "true", "yes", "да"):
        return True
    if text in ("0", "false", "no", "нет", ""):
        return False
    return bool(text)


def normalize_request_payload(list_item, detail):
    detail = detail if isinstance(detail, dict) else {}
    fields = field_map(detail)
    customer = detail.get("customer") if isinstance(detail.get("customer"), dict) else {}
    logistic = detail.get("logistic") if isinstance(detail.get("logistic"), dict) else {}
    products = detail.get("products") if isinstance(detail.get("products"), list) else []

    return {
        "id": parse_int_value(detail.get("id")) or parse_int_value(request_list_value(list_item, "id")),
        "number": normalize_text(
            detail.get("delivery_number")
            or request_list_value(list_item, "delivery_number", "number")
        ),
        "customer_name": normalize_text(customer.get("name") or request_list_value(list_item, "customer")),
        "type": normalize_text(detail.get("type") or request_list_value(list_item, "type")),
        "is_completed": parse_bool(first_raw_value(
            detail.get("isCompleted"),
            detail.get("is_completed"),
            request_list_value(list_item, "is_completed"),
        )),
        "archived": parse_bool(first_raw_value(detail.get("archived"), request_list_value(list_item, "archived"))),
        "created_at": parse_date(detail.get("createdAt") or request_list_value(list_item, "created_at")),
        "unloading_date": parse_date(get_field(fields, "unloading_date", "Дата выгрузки")),
        "recipient": first_text_value(
            get_field(fields, "company_name", "Название компании/Имя человека"),
            detail.get("company_name"),
        ),
        "address": first_text_value(
            get_field(fields, "address", "Адрес"),
            detail.get("address"),
            logistic.get("address"),
        ),
        "comment": first_text_value(
            detail.get("comment"),
            get_field(fields, "comment", "Комментарий"),
        ),
        "products": [
            {
                "name": normalize_text(product.get("name")),
                "vendor_code": normalize_text(product.get("vendorCode") or product.get("vendor_code")),
                "barcode": normalize_text(product.get("barcode")),
                "amount": parse_int_value(product.get("amount")),
            }
            for product in products
            if isinstance(product, dict)
        ],
        "raw": {
            "list": list_item,
            "detail": detail,
        },
    }


def is_active_or_recent_request(request, today=None, lookback_days=2):
    if not request.get("is_completed") and not request.get("archived"):
        return True

    today = today or datetime.now().date()
    request_date = (
        parse_date_obj(request.get("unloading_date"))
        or parse_date_obj(request.get("created_at"))
    )
    if not request_date:
        return False
    return request_date >= today - timedelta(days=lookback_days)


def list_item_active_or_recent(item, today=None, lookback_days=2):
    if not parse_bool(request_list_value(item, "is_completed")) and not parse_bool(request_list_value(item, "archived")):
        return True

    today = today or datetime.now().date()
    request_date = parse_date_obj(request_list_value(item, "created_at"))
    if not request_date:
        return False
    return request_date >= today - timedelta(days=lookback_days)


def list_item_is_active(item):
    return not parse_bool(request_list_value(item, "is_completed")) and not parse_bool(request_list_value(item, "archived"))


def normalize_company_name(value):
    """Нормализация названия контрагента для строгого, но терпимого сравнения.

    Убираем все небуквенно-цифровые символы (кавычки любых видов "'«»“”„, дефисы,
    точки, запятые, скобки и т.п.), приводим к нижнему регистру, ё→е, схлопываем
    пробелы. Слова и цифры сохраняются и должны совпадать один-в-один.

    Примеры:
        '"MARKET AL-KABIR" MChJ'   → 'market al kabir mchj'
        '«MARKET AL-KABIR» MChJ'    → 'market al kabir mchj'
        'MARKET AL-KABIR MChJ'      → 'market al kabir mchj'
        'ООО "Аэропорт"'            → 'ооо аэропорт'
    """
    text = normalize_lookup_text(value)
    if not text:
        return ""
    # Любой неалфавитно-цифровой символ заменяем на пробел, затем схлопываем.
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def simplify_tokens(value, noise_tokens=None):
    noise_tokens = noise_tokens or set()
    text = normalize_lookup_text(value)
    tokens = re.findall(r"[a-zа-я0-9]+", text)
    return [token for token in tokens if token and token not in noise_tokens]


def text_tokens_match(left, right, noise_tokens=None, min_overlap=0.75):
    left_tokens = simplify_tokens(left, noise_tokens)
    right_tokens = simplify_tokens(right, noise_tokens)
    if not left_tokens or not right_tokens:
        return False

    left_set = set(left_tokens)
    right_set = set(right_tokens)
    shorter, longer = (left_set, right_set) if len(left_set) <= len(right_set) else (right_set, left_set)
    overlap = len(shorter.intersection(longer)) / max(1, len(shorter))
    return overlap >= min_overlap


def product_sku_key(value):
    text = normalize_lookup_text(value)
    tokens = simplify_tokens(text)
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


def product_names_match(order_name, request_product):
    order_key = product_sku_key(order_name)
    candidates = [
        request_product.get("name", ""),
        request_product.get("vendor_code", ""),
        request_product.get("barcode", ""),
    ]
    candidate_keys = [product_sku_key(candidate) for candidate in candidates if candidate]
    candidate_keys = [key for key in candidate_keys if key]
    if order_key and candidate_keys:
        return order_key in candidate_keys
    return any(
        text_tokens_match(order_name, candidate, NOISE_PRODUCT_TOKENS, min_overlap=0.8)
        for candidate in candidates
        if candidate
    )


def address_matches(order_address, request_address):
    if not normalize_text(order_address) or not normalize_text(request_address):
        return False
    return text_tokens_match(order_address, request_address, min_overlap=0.55)


def request_type_matches(value, expected=None):
    actual = normalize_lookup_text(value)
    expected = normalize_lookup_text(expected or SKLADBOT_SHIPMENT_TYPE_NAME)
    if set(actual.split()).intersection(RETURN_REQUEST_TOKENS):
        return False
    if expected and actual == expected:
        return True
    return "3pl" in actual and "отгруз" in actual


def request_matches_order_group(group, request):
    # Строгое сравнение даты: «Дата отгрузки» в листе data должна один-в-один
    # совпадать с «Дата выгрузки» (unloading_date) в SkladBot. Обе даты
    # обязательны — если хотя бы одна пустая, привязку делать нельзя, иначе
    # под одну запись могут «схлопнуться» заявки из разных дней.
    group_date = parse_date_to_standard(group.get("date"))
    request_date = parse_date_to_standard(request.get("unloading_date"))
    if not group_date or not request_date or group_date != request_date:
        return False

    # Строгое сравнение клиента: «Название компании/Имя человека» в SkladBot
    # должно совпадать с «Клиент» в листе data. Семантика строгая (нужен тот
    # же контрагент), но техническая нормализация терпимая: убираем кавычки
    # любых видов, дефисы, точки, запятые, скобки — только это меняется
    # между источниками. Слова, цифры, ё/е и пробелы сравниваются строго.
    group_client = normalize_company_name(group.get("client"))
    request_recipient = normalize_company_name(request.get("recipient"))
    if not group_client or not request_recipient or group_client != request_recipient:
        return False

    if normalize_payment_type(group.get("payment")) != normalize_payment_type(request.get("comment")):
        return False

    request_products = list(request.get("products") or [])
    used_indexes = set()
    order_products = list(group.get("products") or [])
    if not order_products:
        return False
    for order_product in order_products:
        matched_index = None
        for idx, request_product in enumerate(request_products):
            if idx in used_indexes:
                continue
            if request_product.get("amount") != order_product.get("blocks"):
                continue
            if product_names_match(order_product.get("name"), request_product):
                matched_index = idx
                break
        if matched_index is None:
            return False
        used_indexes.add(matched_index)

    return True


def fetch_candidate_requests(settings=None, client=None, today=None):
    settings = settings or load_skladbot_settings()
    if not skladbot_is_configured(settings):
        return []

    sync_lookback_days = settings.get("sync_lookback_days", SKLADBOT_SYNC_LOOKBACK_DAYS)
    client = client or SkladBotClient(
        settings["api_token"],
        settings.get("base_url"),
        timeout=settings.get("api_timeout_seconds", SKLADBOT_API_TIMEOUT_SECONDS),
        request_delay_seconds=settings.get("request_delay_seconds", SKLADBOT_REQUEST_DELAY_SECONDS),
    )
    items = client.list_requests(
        settings["customer_id"],
        settings["shipment_type_id"],
        limit=settings.get("requests_limit", SKLADBOT_REQUESTS_LIMIT),
    )
    requests = []
    filtered_items = [
        item
        for item in items
        if list_item_in_sync_window(
            item,
            today=today,
            lookback_days=sync_lookback_days,
        )
    ]
    active_items = [item for item in filtered_items if list_item_is_active(item)]
    completed_items = [item for item in filtered_items if not list_item_is_active(item)]
    # Диагностика: показываем фактический разброс created_at у всех 500 заявок,
    # чтобы было видно, не отрезает ли первичный фильтр полезные строки.
    item_dates = sorted(
        d for d in (
            parse_date_obj(request_list_value(item, "created_at", "createdAt", "date"))
            for item in items
        ) if d
    )
    sample_dates = (
        f"{item_dates[0].strftime('%d.%m.%Y')}..{item_dates[-1].strftime('%d.%m.%Y')}"
        if item_dates else "—"
    )
    logging.info(
        "SkladBot: список=%s (created_at %s), окно=%s дн., к проверке=%s, активных=%s, завершённых/архивных=%s",
        len(items),
        sample_dates,
        sync_lookback_days,
        len(filtered_items),
        len(active_items),
        len(completed_items),
    )

    for item in active_items + completed_items:
        request_id = parse_int_value(request_list_value(item, "id"))
        if request_id <= 0:
            continue
        detail = client.get_request_detail(request_id)
        request = normalize_request_payload(item, detail)
        if request.get("customer_name") and not text_tokens_match(
            settings.get("customer_name"),
            request.get("customer_name"),
            NOISE_COMPANY_TOKENS,
            min_overlap=0.8,
        ):
            continue
        if request.get("type") and not request_type_matches(request.get("type"), settings.get("shipment_type_name")):
            continue
        if not request_in_sync_window(
            request,
            today=today,
            lookback_days=sync_lookback_days,
        ):
            continue
        requests.append(request)
    return requests
