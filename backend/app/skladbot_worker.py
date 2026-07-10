import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import desc, or_, select, text
from sqlalchemy.orm import selectinload

from .audit_identity import AuditActor, set_audit_actor
from .db import SessionLocal
from .google_sheets_pending import queue_google_sheets_export
from .models import AuditLog, Order, OrderItem
from .orders_service import COMPLETED_STATUSES
from .settings import load_settings


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

NOISE_COMPANY_TOKENS = {"ooo", "ооо", "mchj", "мчж", "ip", "ип", "sp", "сп", "склад", "склади"}
NOISE_PRODUCT_TOKENS = {"uz", "kingsize", "king", "size", "superslim", "super", "slim"}
RETURN_REQUEST_TOKENS = {"возврат", "возврата", "return", "returned"}
PRODUCT_COLORS = ("brown", "red", "gold", "green")
PRODUCT_FORMATS = ("op", "ssl")
SKLADBOT_SYNC_LOCK_KEY = 22052631
SKLADBOT_COMPLETED_BACKFILL_STATUSES = ("completed", "done", "closed")
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


class CandidateRequests(list):
    def __init__(
        self,
        items=None,
        complete=True,
        reason="",
        details_checked=0,
        detail_limit=0,
        errors=None,
        checked_request_ids=None,
        last_checked_request_id=0,
        candidate_count=0,
        rotated_after_request_id=0,
    ):
        super().__init__(items or [])
        self.complete = complete
        self.reason = reason
        self.details_checked = details_checked
        self.detail_limit = detail_limit
        self.errors = errors or []
        self.checked_request_ids = checked_request_ids or []
        self.last_checked_request_id = last_checked_request_id
        self.candidate_count = candidate_count
        self.rotated_after_request_id = rotated_after_request_id

    def meta(self):
        return {
            "complete": self.complete,
            "reason": self.reason,
            "details_checked": self.details_checked,
            "detail_limit": self.detail_limit,
            "errors": self.errors,
            "checked_request_ids": self.checked_request_ids,
            "last_checked_request_id": self.last_checked_request_id,
            "candidate_count": self.candidate_count,
            "rotated_after_request_id": self.rotated_after_request_id,
        }


def env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def parse_skladbot_api_tokens(environ=None):
    if environ is None:
        environ = os.environ
    raw_tokens = normalize_text(environ.get("SKLADBOT_API_TOKENS"))
    if raw_tokens:
        candidates = re.split(r"[\s,;]+", raw_tokens)
    else:
        candidates = [environ.get("SKLADBOT_API_TOKEN", "")]
    tokens = []
    seen = set()
    for candidate in candidates:
        token = normalize_text(candidate)
        if not token or token in seen:
            continue
        tokens.append(token)
        seen.add(token)
    if not tokens and raw_tokens:
        fallback_token = normalize_text(environ.get("SKLADBOT_API_TOKEN", ""))
        if fallback_token:
            tokens.append(fallback_token)
    return tokens


def sanitize_skladbot_error(value):
    text = normalize_text(value)
    if not text:
        return ""
    for token in parse_skladbot_api_tokens():
        if token:
            text = text.replace(token, "***")
    text = re.sub(r"(Authorization['\"]?\s*[:=]\s*['\"]?Bearer\s+)[A-Za-z0-9._-]+", r"\1***", text, flags=re.I)
    return text


def normalize_text(value):
    return str(value or "").strip()


def skladbot_response_error_text(response):
    body = ""
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        body_value = next(
            (
                payload.get(key)
                for key in ("detail", "message", "error", "errors")
                if payload.get(key)
            ),
            payload,
        )
    elif payload:
        body_value = payload
    else:
        body_value = getattr(response, "text", "")
    if isinstance(body_value, (dict, list)):
        body = json.dumps(body_value, ensure_ascii=False)
    else:
        body = normalize_text(body_value)
    status_code = normalize_text(getattr(response, "status_code", ""))
    if body:
        return sanitize_skladbot_error(f"SkladBot API HTTP {status_code}: {body}")
    return sanitize_skladbot_error(f"SkladBot API HTTP {status_code}")


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


def date_in_window(value, today=None, lookback_days=1):
    today = today or business_today()
    parsed = parse_date(value)
    if not parsed:
        return False
    return today - timedelta(days=lookback_days) <= parsed <= today


def active_order_unloading_dates(orders=None, today=None):
    today = today or business_today()
    dates = {today + timedelta(days=1)}
    for order in orders or []:
        order_date = getattr(order, "order_date", None)
        if order_date:
            dates.add(order_date)
    return dates


def request_unloading_date_matches_active_orders(request, orders=None, today=None):
    parsed = parse_date(request.get("unloading_date") if isinstance(request, dict) else None)
    if not parsed:
        return False
    return parsed in active_order_unloading_dates(orders=orders, today=today)


def request_created_recently(request, today=None, lookback_days=1):
    dated_values = [
        value
        for value in (request.get("created_at"), request.get("updated_at"))
        if parse_date(value)
    ]
    if not dated_values:
        return False
    return any(date_in_window(value, today=today, lookback_days=lookback_days) for value in dated_values)


def dynamic_skladbot_lookback_days(orders=None, today=None, base_lookback_days=None):
    today = today or business_today()
    base_lookback_days = env_int("SKLADBOT_SYNC_LOOKBACK_DAYS", 1) if base_lookback_days is None else int(base_lookback_days or 0)
    max_lookback_days = max(base_lookback_days, env_int("SKLADBOT_SYNC_MAX_LOOKBACK_DAYS", 7))
    create_lead_days = max(0, env_int("SKLADBOT_ORDER_CREATE_LEAD_DAYS", 3))
    order_dates = [
        order.order_date
        for order in orders or []
        if getattr(order, "order_date", None) is not None
    ]
    if not order_dates:
        return max(0, base_lookback_days)
    oldest_order_date = min(order_dates)
    days_since_oldest_order = (today - oldest_order_date).days
    if days_since_oldest_order < 0:
        return max(0, base_lookback_days)
    required_lookback = days_since_oldest_order + create_lead_days
    return min(max(base_lookback_days, required_lookback), max_lookback_days)


def order_has_skladbot_number(order):
    raw_payload = getattr(order, "raw_payload", None) or {}
    return bool(normalize_text(raw_payload.get("skladbot_request_number")) or normalize_text(raw_payload.get("skladbot_request_id")))


def order_needs_skladbot_backfill(order):
    raw_payload = getattr(order, "raw_payload", None) or {}
    return not (
        normalize_text(raw_payload.get("skladbot_request_number"))
        and normalize_text(raw_payload.get("skladbot_request_id"))
    )


def completed_backfill_days():
    return max(0, env_int("SKLADBOT_COMPLETED_BACKFILL_DAYS", 2))


def completed_backfill_cutoffs(today=None, now=None):
    days = completed_backfill_days()
    now = now or datetime.now(timezone.utc)
    today = today or business_today(now)
    return today - timedelta(days=days), now - timedelta(days=days)


def load_skladbot_sync_orders(db, now=None):
    active_orders = db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .where(~Order.status.in_(COMPLETED_STATUSES))
        .order_by(Order.order_date.asc(), Order.created_at.asc())
    ).scalars().all()

    cutoff_date, cutoff_datetime = completed_backfill_cutoffs(now=now)
    completed_backfill = []
    if completed_backfill_days() > 0:
        completed_backfill = db.execute(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.status.in_(SKLADBOT_COMPLETED_BACKFILL_STATUSES))
            .where(
                or_(
                    Order.updated_at >= cutoff_datetime,
                    Order.order_date >= cutoff_date,
                )
            )
            .order_by(Order.updated_at.desc(), Order.created_at.desc())
        ).scalars().all()
        completed_backfill = [
            order
            for order in completed_backfill
            if order_needs_skladbot_backfill(order)
        ]

    seen = set()
    orders = []
    for order in [*active_orders, *completed_backfill]:
        order_id = str(order.id)
        if order_id in seen:
            continue
        seen.add(order_id)
        orders.append(order)
    return orders, active_orders, completed_backfill


def all_orders_have_candidate_match(orders, requests):
    if not orders:
        return False
    for order in orders:
        if not any(request_matches_order(order, request) for request in requests):
            return False
    return True


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


class SkladBotClient:
    def __init__(self):
        self.tokens = parse_skladbot_api_tokens()
        self.token_index = 0
        self.token_cooldown_until = {}
        self.disabled_token_indexes = set()
        self.base_url = normalize_text(os.environ.get("SKLADBOT_API_BASE_URL")) or "https://api.skladbot.ru/v1"
        self.base_url = self.base_url.rstrip("/")
        self.timeout = env_int("SKLADBOT_API_TIMEOUT_SECONDS", 8)
        self.customer_id = env_int("SKLADBOT_CUSTOMER_ID", 6211)
        self.shipment_type_id = env_int("SKLADBOT_SHIPMENT_TYPE_ID", 3389)
        self.limit = env_int("SKLADBOT_REQUESTS_LIMIT", 500)
        self.request_delay = max(0.0, env_float("SKLADBOT_REQUEST_DELAY_SECONDS", 0.25))
        self.max_retries = max(0, env_int("SKLADBOT_API_MAX_RETRIES", 2))
        self.max_cooldown_wait = max(0.0, env_float("SKLADBOT_MAX_COOLDOWN_WAIT_SECONDS", 5.0))
        self.last_request_at = 0.0

    @property
    def configured(self):
        return bool(self.tokens)

    def pick_token_index(self):
        now = time.monotonic()
        for offset in range(len(self.tokens)):
            index = (self.token_index + offset) % len(self.tokens)
            if index in self.disabled_token_indexes:
                continue
            if self.token_cooldown_until.get(index, 0.0) <= now:
                self.token_index = index
                return index
        return None

    def mark_token_cooldown(self, index, seconds):
        if index is None:
            return
        self.token_cooldown_until[index] = time.monotonic() + max(0.0, float(seconds or 0))

    def mark_token_disabled(self, index):
        if index is not None:
            self.disabled_token_indexes.add(index)

    def wait_for_available_token(self):
        index = self.pick_token_index()
        if index is not None:
            return index
        if len(self.disabled_token_indexes) >= len(self.tokens):
            raise RuntimeError("All SkladBot API tokens are disabled")
        now = time.monotonic()
        cooldowns = [
            cooldown_until
            for index, cooldown_until in self.token_cooldown_until.items()
            if index not in self.disabled_token_indexes and cooldown_until > now
        ]
        if cooldowns:
            sleep_for = max(0.0, min(cooldowns) - now)
            if self.max_cooldown_wait > 0:
                sleep_for = min(sleep_for, self.max_cooldown_wait)
            logging.warning("SkladBot API tokens are in cooldown, wait %.1fs", sleep_for)
            time.sleep(sleep_for)
        index = self.pick_token_index()
        if index is None:
            raise RuntimeError("SkladBot API tokens are not available")
        return index

    def advance_token(self, index):
        if self.tokens:
            self.token_index = (int(index or 0) + 1) % len(self.tokens)

    def wait_between_requests(self):
        if self.request_delay <= 0:
            return
        now = time.monotonic()
        if self.last_request_at > 0:
            elapsed = now - self.last_request_at
            if elapsed < self.request_delay:
                time.sleep(self.request_delay - elapsed)
        self.last_request_at = time.monotonic()

    def retry_sleep(self, seconds):
        sleep_for = max(0.0, float(seconds or 0))
        if sleep_for:
            time.sleep(sleep_for)
        self.last_request_at = 0.0

    def get(self, path, params=None):
        if not self.tokens:
            raise RuntimeError("SKLADBOT_API_TOKEN is not configured")
        url = f"{self.base_url}/{path.lstrip('/')}"
        max_attempts = max(self.max_retries + 1, len(self.tokens))
        last_response = None
        with httpx.Client(timeout=self.timeout) as client:
            for attempt in range(max_attempts):
                token_index = self.wait_for_available_token()
                token = self.tokens[token_index]
                try:
                    self.wait_between_requests()
                    response = client.get(
                        url,
                        params=params or {},
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    )
                except httpx.TimeoutException:
                    cooldown = max(1.0, self.request_delay)
                    self.mark_token_cooldown(token_index, cooldown)
                    self.advance_token(token_index)
                    if attempt + 1 < max_attempts:
                        logging.warning(
                            "SkladBot API timeout with token %s/%s, retry %s/%s",
                            token_index + 1,
                            len(self.tokens),
                            attempt + 1,
                            max_attempts - 1,
                        )
                        self.retry_sleep(cooldown)
                        continue
                    raise
                last_response = response
                if response.status_code in {401, 403}:
                    self.mark_token_disabled(token_index)
                    self.advance_token(token_index)
                    logging.warning(
                        "SkladBot API token %s/%s disabled after HTTP %s",
                        token_index + 1,
                        len(self.tokens),
                        response.status_code,
                    )
                    if attempt + 1 < max_attempts and len(self.disabled_token_indexes) < len(self.tokens):
                        continue
                    raise RuntimeError(skladbot_response_error_text(response))
                if response.status_code == 429 and attempt + 1 < max_attempts:
                    retry_after = parse_int(response.headers.get("Retry-After"))
                    sleep_for = retry_after if retry_after > 0 else max(1.0, self.request_delay * 4 * (attempt + 1))
                    self.mark_token_cooldown(token_index, sleep_for)
                    self.advance_token(token_index)
                    logging.warning(
                        "SkladBot API 429 with token %s/%s, retry %s/%s after %.1fs",
                        token_index + 1,
                        len(self.tokens),
                        attempt + 1,
                        max_attempts - 1,
                        sleep_for,
                    )
                    self.retry_sleep(sleep_for)
                    continue
                if 500 <= response.status_code < 600 and attempt + 1 < max_attempts:
                    sleep_for = max(1.0, self.request_delay)
                    self.mark_token_cooldown(token_index, sleep_for)
                    self.advance_token(token_index)
                    logging.warning(
                        "SkladBot API HTTP %s with token %s/%s, retry %s/%s after %.1fs",
                        response.status_code,
                        token_index + 1,
                        len(self.tokens),
                        attempt + 1,
                        max_attempts - 1,
                        sleep_for,
                    )
                    self.retry_sleep(sleep_for)
                    continue
                if response.status_code >= 400:
                    raise RuntimeError(skladbot_response_error_text(response))
                return response.json()
        if last_response is not None:
            raise RuntimeError(skladbot_response_error_text(last_response))
        raise RuntimeError("SkladBot API request failed")

    def post(self, path, payload=None):
        if not self.tokens:
            raise RuntimeError("SKLADBOT_API_TOKEN is not configured")
        url = f"{self.base_url}/{path.lstrip('/')}"
        with httpx.Client(timeout=self.timeout) as client:
            token_index = self.wait_for_available_token()
            token = self.tokens[token_index]
            try:
                self.wait_between_requests()
                response = client.post(
                    url,
                    json=payload or {},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                )
            except httpx.TimeoutException:
                cooldown = max(1.0, self.request_delay)
                self.mark_token_cooldown(token_index, cooldown)
                self.advance_token(token_index)
                raise
            if response.status_code in {401, 403}:
                self.mark_token_disabled(token_index)
                self.advance_token(token_index)
                logging.warning(
                    "SkladBot API POST token %s/%s disabled after HTTP %s",
                    token_index + 1,
                    len(self.tokens),
                    response.status_code,
                )
            elif response.status_code == 429:
                retry_after = parse_int(response.headers.get("Retry-After"))
                sleep_for = retry_after if retry_after > 0 else max(1.0, self.request_delay * 4)
                self.mark_token_cooldown(token_index, sleep_for)
                self.advance_token(token_index)
                logging.warning(
                    "SkladBot API POST 429 with token %s/%s, request queued for later retry",
                    token_index + 1,
                    len(self.tokens),
                )
            elif 500 <= response.status_code < 600:
                cooldown = max(1.0, self.request_delay)
                self.mark_token_cooldown(token_index, cooldown)
                self.advance_token(token_index)
                logging.warning(
                    "SkladBot API POST HTTP %s with token %s/%s, request queued for later reconcile",
                    response.status_code,
                    token_index + 1,
                    len(self.tokens),
                )
            if response.status_code >= 400:
                raise RuntimeError(skladbot_response_error_text(response))
            return response.json()

    def list_requests(self, type_id=None):
        return extract_list_items(self.get("/requests", {
            "customer_id": self.customer_id,
            "type_id": self.shipment_type_id if type_id is None else type_id,
            "limit": self.limit,
        }))

    def create_request(self, payload):
        return self.post("/requests", payload)

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


def fetch_candidate_requests(today=None, orders=None, client=None, start_after_request_id=0):
    client = client or SkladBotClient()
    if not client.configured:
        logging.info("SkladBot worker disabled: SKLADBOT_API_TOKEN is not configured")
        return CandidateRequests([], complete=True)

    lookback_days = dynamic_skladbot_lookback_days(orders=orders, today=today)
    detail_limit = max(1, env_int("SKLADBOT_DETAIL_LIMIT", 10))
    result = []
    details_checked = 0
    detail_errors = []
    stopped_by_limit = False
    list_items = []
    for item in client.list_requests():
        list_type = normalize_text(request_list_value(item, "type"))
        if list_type and not request_type_matches(list_type):
            continue
        request_id = parse_int(request_list_value(item, "id"))
        if request_id <= 0:
            continue
        list_unloading_date = request_list_value(item, "unloading_date", "unloadingDate")
        list_dates = {
            "created_at": request_list_value(item, "created_at", "createdAt"),
            "updated_at": request_list_value(item, "updated_at", "updatedAt"),
        }
        list_recent = request_created_recently(list_dates, today=today, lookback_days=lookback_days)
        list_unloading_matches = request_unloading_date_matches_active_orders(
            {"unloading_date": list_unloading_date},
            orders=orders,
            today=today,
        )
        has_list_dates = any(parse_date(value) for value in [*list_dates.values(), list_unloading_date])
        if has_list_dates and not list_recent and not list_unloading_matches:
            if parse_date(list_unloading_date) or not orders:
                continue
        freshness_candidates = [
            parse_date(list_dates.get("updated_at")),
            parse_date(list_dates.get("created_at")),
        ]
        freshness_date = max((value for value in freshness_candidates if value), default=None)
        if freshness_date is None:
            freshness_date = parse_date(list_unloading_date)
        priority = 0 if list_unloading_matches else 1 if list_recent else 2
        list_items.append((priority, freshness_date, request_id, item))

    list_items.sort(
        key=lambda value: (
            value[0],
            -(value[1].toordinal() if value[1] else 0),
        )
    )
    list_items = rotate_candidate_list_items(list_items, start_after_request_id)

    candidate_count = len(list_items)
    checked_request_ids = []
    for _priority, _freshness_date, _request_id, item in list_items:
        if details_checked >= detail_limit:
            stopped_by_limit = True
            break
        request_id = parse_int(request_list_value(item, "id"))
        try:
            detail = client.get_request_detail(request_id)
            details_checked += 1
            checked_request_ids.append(request_id)
        except httpx.HTTPStatusError as exc:
            detail_errors.append({"request_id": request_id, "error": f"HTTP {exc.response.status_code if exc.response is not None else 'unknown'}"})
            logging.warning(
                "SkladBot worker: skip request_id=%s after HTTP %s",
                request_id,
                exc.response.status_code if exc.response is not None else "unknown",
            )
            continue
        except Exception as exc:
            detail_errors.append({"request_id": request_id, "error": sanitize_skladbot_error(exc)})
            logging.warning(
                "SkladBot worker: skip request_id=%s after detail fetch error: %s",
                request_id,
                sanitize_skladbot_error(exc),
            )
            continue
        if client.request_delay:
            time.sleep(client.request_delay)
        request = normalize_request_payload(item, detail)
        if not request_type_matches(request.get("type")):
            continue
        if not (
            request_created_recently(request, today=today, lookback_days=lookback_days)
            or request_unloading_date_matches_active_orders(request, orders=orders, today=today)
        ):
            continue
        result.append(request)
        if orders and all_orders_have_candidate_match(orders, result):
            break
    complete = not detail_errors and not (stopped_by_limit and not all_orders_have_candidate_match(orders or [], result))
    reason = ""
    if detail_errors:
        reason = "detail_errors"
    if stopped_by_limit and not all_orders_have_candidate_match(orders or [], result):
        reason = "detail_limit_reached"
    logging.info(
        "SkladBot worker: candidates=%s details_checked=%s lookback_days=%s detail_limit=%s complete=%s reason=%s",
        len(result),
        details_checked,
        lookback_days,
        detail_limit,
        complete,
        reason,
    )
    return CandidateRequests(
        result,
        complete=complete,
        reason=reason,
        details_checked=details_checked,
        detail_limit=detail_limit,
        errors=detail_errors[:20],
        checked_request_ids=checked_request_ids,
        last_checked_request_id=checked_request_ids[-1] if checked_request_ids else 0,
        candidate_count=candidate_count,
        rotated_after_request_id=parse_int(start_after_request_id),
    )


def rotate_candidate_list_items(list_items, start_after_request_id=0):
    cursor = parse_int(start_after_request_id)
    if cursor <= 0 or len(list_items) < 2:
        return list_items
    for index, item in enumerate(list_items):
        if item[2] == cursor:
            return list_items[index + 1:] + list_items[:index + 1]
    return list_items


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


def update_orders_from_skladbot(audit_actor: AuditActor | None = None):
    checked_at = datetime.now(timezone.utc).isoformat()
    updated = 0
    matched = 0
    not_found = 0
    multiple = 0
    incomplete = 0
    pending = 0

    with SessionLocal() as db:
        if audit_actor is not None:
            set_audit_actor(db, audit_actor)
        if not try_acquire_skladbot_sync_lock(db):
            logging.info("SkladBot worker: another sync is already running, skip")
            return {"requests": 0, "updated": 0, "matched": 0, "not_found": 0, "multiple": 0, "busy": True}
        try:
            orders, active_orders, completed_backfill_orders = load_skladbot_sync_orders(db)
            if not orders:
                logging.info("SkladBot worker: no active or recent completed backend orders, skip SkladBot API")
                return {
                    "requests": 0,
                    "updated": 0,
                    "matched": 0,
                    "not_found": 0,
                    "multiple": 0,
                    "completed_backfill_orders": 0,
                }

            orders_to_check = [order for order in orders if order_needs_skladbot_backfill(order)]
            if not orders_to_check:
                logging.info("SkladBot worker: all active/recent orders already have SkladBot numbers, skip SkladBot API")
                google_sheets_result = export_skladbot_numbers_to_google_sheets(db, active_orders)
                db.add(AuditLog(
                    action="skladbot_google_sheets_export",
                    entity_type="skladbot",
                    entity_id="worker",
                    payload=google_sheets_result,
                ))
                db.commit()
                return {
                    "requests": 0,
                    "updated": 0,
                    "matched": 0,
                    "not_found": 0,
                    "multiple": 0,
                    "already_numbered": len(orders),
                    "active_orders": len(active_orders),
                    "completed_backfill_orders": len(completed_backfill_orders),
                    "google_sheets_export": google_sheets_result,
                }

            requests = fetch_candidate_requests(
                orders=orders_to_check,
                start_after_request_id=load_skladbot_fetch_cursor(db),
            )

            for order in orders_to_check:
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
                    raw_payload["skladbot_nearest"] = nearest_request_diagnostics(order, requests)
                    multiple += 1
                elif not getattr(requests, "complete", True):
                    raw_payload["skladbot_status"] = "pending"
                    raw_payload.pop("skladbot_error", None)
                    raw_payload["skladbot_fetch"] = requests.meta() if hasattr(requests, "meta") else {}
                    raw_payload["skladbot_nearest"] = nearest_request_diagnostics(order, requests)
                    incomplete += 1
                    pending += 1
                else:
                    raw_payload["skladbot_status"] = "not_found"
                    raw_payload["skladbot_nearest"] = nearest_request_diagnostics(order, requests)
                    not_found += 1
                order.raw_payload = raw_payload
                updated += 1

            db.add(AuditLog(
                action="skladbot_worker_sync",
                entity_type="skladbot",
                entity_id="worker",
                payload={
                    "requests": len(requests),
                    "orders_checked": len(orders_to_check),
                    "orders_already_numbered": len(orders) - len(orders_to_check),
                    "active_orders": len(active_orders),
                    "completed_backfill_orders": len(completed_backfill_orders),
                    "updated": updated,
                    "matched": matched,
                    "not_found": not_found,
                    "multiple": multiple,
                    "incomplete": incomplete,
                    "pending": pending,
                    "fetch": requests.meta() if hasattr(requests, "meta") else {},
                },
            ))
            include_archive = bool(completed_backfill_orders)
            google_sheets_result = export_skladbot_numbers_to_google_sheets(
                db,
                orders,
                include_inactive=include_archive,
                include_archive=include_archive,
                force=True,
            )
            db.add(AuditLog(
                action="skladbot_google_sheets_export",
                entity_type="skladbot",
                entity_id="worker",
                payload=google_sheets_result,
            ))
            db.commit()
        finally:
            release_skladbot_sync_lock(db)

    logging.info(
        "SkladBot worker: requests=%s orders=%s matched=%s not_found=%s multiple=%s pending=%s",
        len(requests),
        updated,
        matched,
        not_found,
        multiple,
        pending,
    )
    return {
        "requests": len(requests),
        "updated": updated,
        "matched": matched,
        "not_found": not_found,
        "multiple": multiple,
        "incomplete": incomplete,
        "pending": pending,
        "active_orders": len(active_orders),
        "completed_backfill_orders": len(completed_backfill_orders),
        "fetch": requests.meta() if hasattr(requests, "meta") else {},
        "google_sheets_export": google_sheets_result,
    }


def load_skladbot_fetch_cursor(db):
    event = db.execute(
        select(AuditLog)
        .where(AuditLog.action == "skladbot_worker_sync")
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    payload = event.payload if event is not None else {}
    fetch = payload.get("fetch") if isinstance(payload, dict) else {}
    if not isinstance(fetch, dict):
        return 0
    return parse_int(fetch.get("last_checked_request_id"))


def export_skladbot_numbers_to_google_sheets(db, orders, include_inactive=False, include_archive=False, force=False):
    order_ids = [str(order.id) for order in orders or []]
    if not order_ids:
        return {"status": "skipped", "updated": 0, "error": ""}
    if not force and not include_inactive and not include_archive and recent_skladbot_google_export_exists(db):
        return {
            "status": "skipped",
            "queued": False,
            "updated": 0,
            "error": "",
            "reason": "recent_export_cooldown",
        }
    result = {"status": "queued", "queued": True, "updated": 0, "error": ""}
    event = queue_google_sheets_export(
        db,
        "google_sheets_skladbot_export",
        "skladbot",
        "active_orders",
        result=result,
        payload={
            "order_ids": order_ids,
            "include_inactive": bool(include_inactive),
            "include_archive": bool(include_archive),
        },
    )
    return {**result, "pending_event_id": str(event.id) if event else ""}


def recent_skladbot_google_export_exists(db, min_interval_seconds=None):
    min_interval_seconds = (
        skladbot_google_export_min_interval_seconds()
        if min_interval_seconds is None
        else int(min_interval_seconds or 0)
    )
    if min_interval_seconds <= 0:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=min_interval_seconds)
    recent = db.execute(
        select(AuditLog)
        .where(AuditLog.action == "skladbot_google_sheets_export")
        .where(AuditLog.created_at >= cutoff)
        .order_by(desc(AuditLog.created_at), desc(AuditLog.id))
        .limit(1)
    ).scalar_one_or_none()
    return recent is not None


def skladbot_google_export_min_interval_seconds():
    return max(0, env_int("SKLADBOT_GOOGLE_EXPORT_MIN_INTERVAL_SECONDS", 300))


def try_acquire_skladbot_sync_lock(db):
    if db.bind.dialect.name != "postgresql":
        return True
    return bool(db.execute(
        text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
        {"lock_key": SKLADBOT_SYNC_LOCK_KEY},
    ).scalar())


def release_skladbot_sync_lock(db):
    if db.bind.dialect.name != "postgresql":
        return
    return


def main():
    interval = worker_interval_seconds()
    once = normalize_lookup_text(os.environ.get("SKLADBOT_WORKER_ONCE")) in {"1", "true", "yes", "да"}
    while True:
        try:
            from .skladbot_request_dry_run import process_pending_skladbot_request_creates
            from .skladbot_return_requests import process_pending_skladbot_return_request_creates

            with SessionLocal() as db:
                result = process_pending_skladbot_request_creates(db)
                if result.get("checked"):
                    logging.info("SkladBot create worker: %s", result)
                return_result = process_pending_skladbot_return_request_creates(db)
                if return_result.get("checked"):
                    logging.info("SkladBot return create worker: %s", return_result)
        except Exception:
            logging.exception("SkladBot create worker failed")
        try:
            update_orders_from_skladbot()
        except Exception:
            logging.exception("SkladBot worker failed")
        if once:
            return
        time.sleep(max(60, interval))


def worker_interval_seconds():
    return max(60, env_int("SKLADBOT_WORKER_INTERVAL_SECONDS", 60))


if __name__ == "__main__":
    main()
