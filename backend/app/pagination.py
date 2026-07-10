"""Bounded, opaque cursor helpers shared by backend list endpoints."""

from __future__ import annotations

import base64
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any


DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200
NEXT_CURSOR_HEADER = "X-TakSklad-Next-Cursor"
PAGE_LIMIT_HEADER = "X-TakSklad-Page-Limit"
CURSOR_VERSION = 1
MAX_CURSOR_LENGTH = 2048
MAX_CURSOR_KEYS = 8
MAX_CURSOR_TEXT_LENGTH = 512


class CursorError(ValueError):
    """Raised when an opaque cursor cannot be trusted for the requested scope."""


def normalize_page_limit(value: Any, *, default: int = DEFAULT_PAGE_LIMIT, maximum: int = MAX_PAGE_LIMIT) -> int:
    default_value = max(1, int(default))
    maximum_value = max(default_value, int(maximum))
    try:
        parsed = int(value) if value is not None and str(value).strip() else default_value
    except (TypeError, ValueError):
        parsed = default_value
    return max(1, min(parsed, maximum_value))


def encode_cursor(scope: str, keys: Sequence[Any], *, filters: Mapping[str, Any] | None = None) -> str:
    normalized_scope = _normalize_scope(scope)
    normalized_keys = _normalize_cursor_keys(keys)
    payload = {
        "v": CURSOR_VERSION,
        "scope": normalized_scope,
        "filter": cursor_filter_hash(filters),
        "keys": normalized_keys,
    }
    encoded = _canonical_json(payload).encode("utf-8")
    if len(encoded) > MAX_CURSOR_LENGTH:
        raise CursorError("invalid_cursor")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def decode_cursor(
    value: str,
    scope: str,
    *,
    filters: Mapping[str, Any] | None = None,
) -> tuple[Any, ...]:
    try:
        encoded = str(value or "").strip()
        if not encoded or len(encoded) > MAX_CURSOR_LENGTH:
            raise CursorError("invalid_cursor")
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        if len(raw) > MAX_CURSOR_LENGTH:
            raise CursorError("invalid_cursor")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"v", "scope", "filter", "keys"}:
            raise CursorError("invalid_cursor")
        if payload["v"] != CURSOR_VERSION:
            raise CursorError("invalid_cursor")
        if payload["scope"] != _normalize_scope(scope):
            raise CursorError("invalid_cursor")
        if payload["filter"] != cursor_filter_hash(filters):
            raise CursorError("invalid_cursor")
        return tuple(_normalize_cursor_keys(payload["keys"]))
    except CursorError:
        raise
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise CursorError("invalid_cursor") from None


def cursor_filter_hash(filters: Mapping[str, Any] | None = None) -> str:
    normalized = _normalize_filter_value(dict(filters or {}))
    return hashlib.sha256(_canonical_json(normalized).encode("utf-8")).hexdigest()


def set_pagination_headers(response: Any, *, next_cursor: str = "", limit: int) -> None:
    headers = response.headers
    try:
        normalized_limit = max(1, int(limit))
    except (TypeError, ValueError):
        normalized_limit = DEFAULT_PAGE_LIMIT
    headers[PAGE_LIMIT_HEADER] = str(normalized_limit)
    normalized_cursor = str(next_cursor or "").strip()
    if normalized_cursor:
        headers[NEXT_CURSOR_HEADER] = normalized_cursor


def _normalize_scope(scope: Any) -> str:
    normalized = str(scope or "").strip()
    if not normalized or len(normalized) > 128:
        raise CursorError("invalid_cursor")
    return normalized


def _normalize_cursor_keys(keys: Sequence[Any]) -> list[Any]:
    if isinstance(keys, (str, bytes, bytearray)) or not isinstance(keys, Sequence):
        raise CursorError("invalid_cursor")
    values = list(keys)
    if not values or len(values) > MAX_CURSOR_KEYS:
        raise CursorError("invalid_cursor")
    return [_normalize_cursor_scalar(value) for value in values]


def _normalize_cursor_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str) and len(value) <= MAX_CURSOR_TEXT_LENGTH:
        return value
    raise CursorError("invalid_cursor")


def _normalize_filter_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CursorError("invalid_cursor")
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_filter_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize_filter_value(item) for item in value]
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
