"""Bounded correlation identifiers for request, import and event traces."""

from contextlib import contextmanager
from contextvars import ContextVar
import logging
import re
from time import monotonic
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send


CORRELATION_HEADER = b"x-correlation-id"
CORRELATION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_correlation_id: ContextVar[str] = ContextVar("taksklad_correlation_id", default="")
SAFE_LOG_TOKEN_RE = re.compile(r"^[a-z0-9_.:-]{1,80}$")


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def sanitize_correlation_id(value: object) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if CORRELATION_ID_RE.fullmatch(candidate) else new_correlation_id()


def current_correlation_id() -> str:
    existing = _correlation_id.get()
    if existing:
        return existing
    generated = new_correlation_id()
    _correlation_id.set(generated)
    return generated


def bind_correlation_id(value: object = ""):
    return _correlation_id.set(sanitize_correlation_id(value))


def reset_correlation_id(token) -> None:
    _correlation_id.reset(token)


def payload_with_correlation(payload: dict | None = None) -> dict:
    value = dict(payload or {})
    value["correlation_id"] = sanitize_correlation_id(
        value.get("correlation_id") or current_correlation_id()
    )
    return value


def pending_event_correlation_id(event) -> str:
    """Return and persist the event trace id without exposing payload contents."""
    payload = dict(getattr(event, "payload", None) or {})
    correlation_id = sanitize_correlation_id(payload.get("correlation_id") or current_correlation_id())
    if payload.get("correlation_id") != correlation_id:
        payload["correlation_id"] = correlation_id
        event.payload = payload
    return correlation_id


@contextmanager
def bind_pending_event(event):
    """Rebind the producer trace while one queued event is processed."""
    token = bind_correlation_id(pending_event_correlation_id(event))
    try:
        yield current_correlation_id()
    finally:
        reset_correlation_id(token)


@contextmanager
def bind_event_payload(payload: dict | None):
    """Rebind a detached/leased event payload before an external call."""
    value = dict(payload or {})
    token = bind_correlation_id(value.get("correlation_id") or current_correlation_id())
    try:
        yield current_correlation_id()
    finally:
        reset_correlation_id(token)


def correlation_log_fields(**fields) -> dict:
    return {"correlation_id": current_correlation_id(), **fields}


def log_trace(logger: logging.Logger, event: str, **bounded_fields) -> None:
    safe_event = _safe_log_token(event)
    safe_fields = " ".join(
        f"{_safe_log_token(key)}={_safe_log_value(value)}"
        for key, value in sorted(bounded_fields.items())
    )
    logger.info(
        "trace event=%s correlation_id=%s%s",
        safe_event,
        current_correlation_id(),
        f" {safe_fields}" if safe_fields else "",
    )


def _safe_log_token(value: object) -> str:
    candidate = str(value or "").strip().casefold()
    return candidate if SAFE_LOG_TOKEN_RE.fullmatch(candidate) else "redacted"


def _safe_log_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return _safe_log_token(value)


class CorrelationIdMiddleware:
    """Set one sanitized UUID per HTTP request and return it to the caller."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or ())
        token = bind_correlation_id(headers.get(CORRELATION_HEADER, b"").decode("ascii", errors="ignore"))
        method = _bounded_method(scope.get("method"))
        route = _bounded_route_group(scope.get("path"))
        started = monotonic()
        response_status = 500
        log_trace(logging.getLogger("taksklad.request"), "request_start", method=method, route_group=route)

        async def send_with_correlation(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = int(message.get("status") or 500)
                response_headers = list(message.get("headers") or ())
                response_headers.append((CORRELATION_HEADER, current_correlation_id().encode("ascii")))
                message = {**message, "headers": response_headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_correlation)
        finally:
            log_trace(
                logging.getLogger("taksklad.request"),
                "request_end",
                method=method,
                route_group=route,
                status=response_status,
                duration_ms=min(300_000, max(0, int((monotonic() - started) * 1000))),
            )
            reset_correlation_id(token)


def _bounded_method(value: object) -> str:
    candidate = str(value or "").upper()
    return candidate if candidate in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"} else "OTHER"


def _bounded_route_group(value: object) -> str:
    segments = str(value or "").strip("/").split("/")
    if segments[:2] == ["api", "v1"] and len(segments) >= 3:
        candidate = segments[2]
        if candidate in {"admin", "auth", "imports", "orders", "reports", "returns", "scans"}:
            return candidate
    if segments and segments[0] in {"health", "ready"}:
        return "health" if segments[0] == "health" else "readiness"
    return "other"
