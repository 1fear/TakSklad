"""Session-bound CSRF proof and strict browser-origin checks."""

from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlsplit

from .web_auth import base64url_encode, session_secret


CSRF_HEADER_NAME = "X-TakSklad-CSRF"
CSRF_ERROR_DETAIL = {"code": "csrf_invalid", "message": "Browser request security check failed"}
ORIGIN_ERROR_DETAIL = {"code": "origin_denied", "message": "Browser request origin denied"}


def csrf_token_for_session(settings, session_token: str) -> str:
    token = str(session_token or "").strip()
    if not token:
        return ""
    digest = hmac.new(
        session_secret(settings),
        b"taksklad-csrf-v1\x00" + token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64url_encode(digest)


def csrf_token_matches(settings, session_token: str, candidate: str | None) -> bool:
    expected = csrf_token_for_session(settings, session_token)
    return bool(expected) and hmac.compare_digest(expected, str(candidate or "").strip())


def expected_browser_origin(request, settings) -> str:
    host = str(request.headers.get("host") or request.url.netloc or "").strip().casefold()
    if not host:
        return ""
    scheme = "https" if settings.web_cookie_secure else str(request.url.scheme or "http").casefold()
    return f"{scheme}://{host}"


def browser_origin_matches(request, settings) -> bool:
    expected = expected_browser_origin(request, settings)
    if not expected:
        return False
    origin = str(request.headers.get("origin") or "").strip()
    if origin:
        return normalize_origin(origin) == expected
    referer = str(request.headers.get("referer") or "").strip()
    if not referer:
        return False
    return normalize_origin(referer) == expected


def normalize_origin(value: str) -> str:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    if parsed.username or parsed.password:
        return ""
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"
