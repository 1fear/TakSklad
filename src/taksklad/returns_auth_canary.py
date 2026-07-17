"""Credentialed read-only verification for the desktop returns path."""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


MAX_CREDENTIAL_CHARACTERS = 4096
SCOPED_SERVICE_TOKEN_RE = re.compile(r"^tks\.[0-9a-f]{32}\.[A-Za-z0-9_-]{32,}$")
PRINCIPAL_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,119}$")
CANARY_IDENTIFIER_HEADER = "X-TakSklad-Canary-Identifier"
CANARY_PATHS = {
    "acceptance": "/api/v1/returns/auth-canary/acceptance",
    "desktop": "/api/v1/returns/auth-canary/desktop",
}
PRODUCTION_BACKEND_ORIGIN = "https://api.taksklad.uz"


class ReturnsAuthCanaryError(RuntimeError):
    """Sanitized canary failure that never includes a credential or response body."""


@dataclass(frozen=True)
class ReturnsAuthCanaryResult:
    status: int
    canary_kind: str


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler()).open


def validate_credential(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_CREDENTIAL_CHARACTERS:
        raise ReturnsAuthCanaryError("credential_missing_or_invalid")
    if value != value.strip() or any(character in value for character in ("\r", "\n", "\x00")):
        raise ReturnsAuthCanaryError("credential_missing_or_invalid")
    return value


def validate_scoped_credential(value: str) -> str:
    credential = validate_credential(value)
    if not SCOPED_SERVICE_TOKEN_RE.fullmatch(credential):
        raise ReturnsAuthCanaryError("scoped_credential_required")
    return credential


def read_credential_from_stdin(stream) -> str:
    value = stream.read(MAX_CREDENTIAL_CHARACTERS + 2)
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    return validate_credential(value)


def validate_base_url(base_url: str, *, allow_test_localhost: bool = False) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(normalized)
    local_http = (
        allow_test_localhost
        and parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    )
    if (
        not normalized
        or (parsed.scheme != "https" and not local_http)
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ReturnsAuthCanaryError("base_url_invalid")
    if not local_http and normalized != PRODUCTION_BACKEND_ORIGIN:
        raise ReturnsAuthCanaryError("base_url_not_approved")
    return normalized


def validate_principal_identifier(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized != value or not PRINCIPAL_IDENTIFIER_RE.fullmatch(normalized):
        raise ReturnsAuthCanaryError("principal_identifier_invalid")
    return normalized


def _request_status(
    opener,
    *,
    base_url: str,
    path: str,
    token: str,
    identifier: str,
    timeout: int,
) -> int:
    request = urllib.request.Request(
        f"{base_url}{path}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            CANARY_IDENTIFIER_HEADER: identifier,
            "User-Agent": "TakSklad-returns-auth-canary",
        },
        method="GET",
    )
    try:
        response = opener(request, timeout=timeout)
        try:
            return int(getattr(response, "status", response.getcode()))
        finally:
            response.close()
    except urllib.error.HTTPError as exc:
        try:
            return int(exc.code)
        finally:
            exc.close()
    except Exception as exc:
        raise ReturnsAuthCanaryError("transport_error") from exc


def run_returns_auth_canary(
    base_url: str,
    token: str,
    *,
    timeout: int = 8,
    opener=None,
    require_scoped: bool = False,
    canary_kind: str = "desktop",
    identifier: str | None = None,
    allow_test_localhost: bool = False,
    allow_missing_endpoint: bool = False,
) -> ReturnsAuthCanaryResult:
    normalized_url = validate_base_url(
        base_url,
        allow_test_localhost=allow_test_localhost,
    )
    credential = (
        validate_scoped_credential(token)
        if require_scoped
        else validate_credential(token)
    )
    timeout = int(timeout)
    if timeout < 1 or timeout > 60:
        raise ReturnsAuthCanaryError("timeout_invalid")
    normalized_kind = str(canary_kind or "").strip().casefold()
    try:
        canary_path = CANARY_PATHS[normalized_kind]
    except KeyError as exc:
        raise ReturnsAuthCanaryError("canary_kind_invalid") from exc
    normalized_identifier = validate_principal_identifier(identifier)
    opener = opener or _NO_REDIRECT_OPENER

    canary_status = _request_status(
        opener,
        base_url=normalized_url,
        path=canary_path,
        token=credential,
        identifier=normalized_identifier,
        timeout=timeout,
    )
    if canary_status == 404 and allow_missing_endpoint:
        return ReturnsAuthCanaryResult(status=canary_status, canary_kind=normalized_kind)
    if canary_status != 204:
        raise ReturnsAuthCanaryError(f"auth_canary_http_{canary_status}")
    return ReturnsAuthCanaryResult(status=canary_status, canary_kind=normalized_kind)
