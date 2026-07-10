import json
import math
from collections import deque
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from starlette.responses import JSONResponse

from .spreadsheet_safety import SpreadsheetSafetyError, normalize_spreadsheet_filename


MAX_REQUEST_BODY_BYTES = 24 * 1024 * 1024
MAX_IMPORT_ROWS = 5_000
MAX_IMPORT_ROW_FIELDS = 64
MAX_IMPORT_KEY_CHARS = 128
MAX_IMPORT_CELL_CHARS = 16_384
MAX_IMPORT_ROW_BYTES = 64 * 1024
MAX_IMPORT_PAYLOAD_BYTES = 20 * 1024 * 1024
MAX_RAW_DEPTH = 4
MAX_RAW_KEYS = 256
MAX_RAW_ITEMS = 512
MAX_RAW_KEY_CHARS = 128
MAX_RAW_STRING_CHARS = 16_384
MAX_RAW_PAYLOAD_BYTES = 64 * 1024


class InputSafetyError(ValueError):
    """Stable, non-sensitive validation failure."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


def json_encoded_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=lambda item: item.isoformat() if isinstance(item, (date, datetime)) else str(item),
        ).encode("utf-8")
    )


def validate_import_row(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputSafetyError("import_row_type_exceeded")
    if len(value) > MAX_IMPORT_ROW_FIELDS:
        raise InputSafetyError("import_row_fields_exceeded")
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > MAX_IMPORT_KEY_CHARS:
            raise InputSafetyError("import_row_key_exceeded")
        if isinstance(item, (dict, list, tuple, set, bytes, bytearray)):
            raise InputSafetyError("import_row_nested_value")
        if item is not None and not isinstance(item, (str, int, float, bool, date, datetime, Decimal)):
            raise InputSafetyError("import_row_value_type")
        if isinstance(item, str) and len(item) > MAX_IMPORT_CELL_CHARS:
            raise InputSafetyError("import_row_cell_exceeded")
        if isinstance(item, float) and not math.isfinite(item):
            raise InputSafetyError("import_row_number_invalid")
        if isinstance(item, Decimal) and not item.is_finite():
            raise InputSafetyError("import_row_number_invalid")
    if json_encoded_size(value) > MAX_IMPORT_ROW_BYTES:
        raise InputSafetyError("import_row_bytes_exceeded")
    return value


def validate_bounded_json_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputSafetyError("raw_payload_type")
    queue = deque([(value, 1)])
    keys = 0
    items = 0
    while queue:
        current, depth = queue.popleft()
        if isinstance(current, dict):
            if depth > MAX_RAW_DEPTH:
                raise InputSafetyError("raw_payload_depth_exceeded")
            keys += len(current)
            items += len(current)
            if keys > MAX_RAW_KEYS:
                raise InputSafetyError("raw_payload_keys_exceeded")
            for key, item in current.items():
                if not isinstance(key, str) or not key or len(key) > MAX_RAW_KEY_CHARS:
                    raise InputSafetyError("raw_payload_key_exceeded")
                queue.append((item, depth + 1))
        elif isinstance(current, list):
            if depth > MAX_RAW_DEPTH:
                raise InputSafetyError("raw_payload_depth_exceeded")
            items += len(current)
            for item in current:
                queue.append((item, depth + 1))
        elif current is None or isinstance(current, (bool, int)):
            pass
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise InputSafetyError("raw_payload_number_invalid")
        elif isinstance(current, str):
            if len(current) > MAX_RAW_STRING_CHARS:
                raise InputSafetyError("raw_payload_string_exceeded")
        else:
            raise InputSafetyError("raw_payload_value_type")
        if items > MAX_RAW_ITEMS:
            raise InputSafetyError("raw_payload_items_exceeded")
    if json_encoded_size(value) > MAX_RAW_PAYLOAD_BYTES:
        raise InputSafetyError("raw_payload_bytes_exceeded")
    return value


def normalize_upload_filename(value: Any) -> str:
    try:
        return normalize_spreadsheet_filename(value)
    except SpreadsheetSafetyError as exc:
        raise InputSafetyError(exc.code) from None


class RequestBodyLimitMiddleware:
    """Buffer bounded request bodies before authentication, parsing, or DB dependencies."""

    def __init__(self, app, max_bytes: int = MAX_REQUEST_BODY_BYTES):
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method", "GET").upper() not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", ())}
        content_length = headers.get(b"content-length", b"")
        try:
            declared_length = int(content_length) if content_length else None
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > self.max_bytes:
            await self._reject(scope, receive, send)
            return

        messages = []
        total = 0
        while True:
            message = await receive()
            messages.append(message)
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") != "http.request":
                continue
            total += len(message.get("body", b""))
            if total > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def replay_receive():
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope, receive, send):
        response = JSONResponse(
            status_code=413,
            content={"detail": "request_too_large"},
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)
