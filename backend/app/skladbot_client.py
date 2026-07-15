"""Explicit HTTP client boundary for the SkladBot API."""

import json
import logging
import os
import re
import time
from enum import Enum
from typing import Protocol, runtime_checkable

import httpx

from .observability_context import current_correlation_id

from .skladbot_contracts import extract_list_items, is_stock_shortage_text, normalize_text, parse_int


class SkladBotErrorKind(str, Enum):
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    STOCK_SHORTAGE = "stock_shortage"
    CLIENT = "client"
    SERVER = "server"
    TIMEOUT = "timeout"
    NETWORK = "network"
    UNKNOWN = "unknown"


class SkladBotApiError(RuntimeError):
    """Typed SkladBot failure with explicit write-ambiguity semantics."""

    def __init__(
        self,
        message,
        *,
        kind=SkladBotErrorKind.UNKNOWN,
        status_code=None,
        ambiguous=False,
    ):
        super().__init__(sanitize_skladbot_error(message))
        self.kind = SkladBotErrorKind(kind)
        self.status_code = int(status_code) if status_code is not None else None
        self.ambiguous = bool(ambiguous)


@runtime_checkable
class SkladBotClientContract(Protocol):
    configured: bool

    def get(self, path, params=None): ...

    def post(self, path, payload=None): ...

    def list_requests(self, type_id=None): ...

    def create_request(self, payload): ...

    def get_request_detail(self, request_id): ...


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
    environ = os.environ if environ is None else environ
    raw_pool = normalize_text(environ.get("SKLADBOT_API_TOKENS"))
    raw_single = normalize_text(environ.get("SKLADBOT_API_TOKEN"))
    tokens = []
    if raw_pool:
        for value in re.split(r"[,;\s]+", raw_pool):
            token = normalize_text(value)
            if token and token not in tokens:
                tokens.append(token)
    if not tokens and raw_single:
        tokens.append(raw_single)
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


def skladbot_response_error(response, *, write=False):
    status_code = int(getattr(response, "status_code", 0) or 0)
    message = skladbot_response_error_text(response)
    if status_code in {401, 403}:
        kind = SkladBotErrorKind.AUTH
    elif status_code == 429:
        kind = SkladBotErrorKind.RATE_LIMIT
    elif status_code >= 500:
        kind = SkladBotErrorKind.SERVER
    elif 400 <= status_code < 500 and is_stock_shortage_text(message):
        kind = SkladBotErrorKind.STOCK_SHORTAGE
    elif 400 <= status_code < 500:
        kind = SkladBotErrorKind.CLIENT
    else:
        kind = SkladBotErrorKind.UNKNOWN
    return SkladBotApiError(
        message,
        kind=kind,
        status_code=status_code or None,
        ambiguous=bool(write and status_code >= 500),
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
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Accept": "application/json",
                            "X-Correlation-ID": current_correlation_id(),
                        },
                    )
                except httpx.TimeoutException as exc:
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
                    raise SkladBotApiError(
                        "SkladBot API timeout",
                        kind=SkladBotErrorKind.TIMEOUT,
                        ambiguous=False,
                    ) from exc
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
                    raise skladbot_response_error(response)
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
                    raise skladbot_response_error(response)
                return response.json()
        if last_response is not None:
            raise skladbot_response_error(last_response)
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
                        "X-Correlation-ID": current_correlation_id(),
                    },
                )
            except httpx.TimeoutException as exc:
                cooldown = max(1.0, self.request_delay)
                self.mark_token_cooldown(token_index, cooldown)
                self.advance_token(token_index)
                raise SkladBotApiError(
                    "SkladBot API POST timeout",
                    kind=SkladBotErrorKind.TIMEOUT,
                    ambiguous=True,
                ) from exc
            except httpx.TransportError as exc:
                cooldown = max(1.0, self.request_delay)
                self.mark_token_cooldown(token_index, cooldown)
                self.advance_token(token_index)
                raise SkladBotApiError(
                    "SkladBot API POST network error",
                    kind=SkladBotErrorKind.NETWORK,
                    ambiguous=True,
                ) from exc
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
                raise skladbot_response_error(response, write=True)
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
