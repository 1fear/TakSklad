"""One-time, secret-safe bootstrap of a Windows desktop identity."""

from __future__ import annotations

import io
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import tkinter as tk
from tkinter import ttk

from .config import APP_NAME, APP_VERSION, TAKSKLAD_BACKEND_TIMEOUT_SECONDS
from .credential_lock import (
    acquire_credential_mutation_lock,
    release_credential_mutation_lock,
)
from .desktop_auth import _production_dpapi_store, install_scoped_backend_bundle
from .returns_auth_canary import (
    PRODUCTION_BACKEND_ORIGIN,
    ReturnsAuthCanaryError,
    validate_principal_identifier,
    validate_scoped_credential,
)
from .secret_store import (
    BACKEND_AUTH_BUNDLE_SECRET,
    SecretStoreError,
    decode_backend_auth_bundle,
    get_secret_store,
)


REDEEM_PATH = "/api/v1/auth/desktop-pairing/redeem"
ACK_PATH_TEMPLATE = "/api/v1/auth/desktop-pairing/{pairing_id}/ack"
MAX_RESPONSE_BYTES = 32 * 1024
SETUP_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
ACK_MAX_ATTEMPTS = 3


class DesktopPairingError(RuntimeError):
    """Sanitized bootstrap failure; it must never contain a code or token."""

    def __init__(self, reason: str, *, retryable: bool = False):
        self.reason = str(reason or "unexpected_failure")
        self.retryable = bool(retryable)
        super().__init__(self.reason)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _system_tls_opener():
    context = ssl.create_default_context()
    return urllib.request.build_opener(
        _NoRedirectHandler(),
        urllib.request.HTTPSHandler(context=context),
    ).open


def _call_opener(opener, request, timeout):
    if hasattr(opener, "open"):
        return opener.open(request, timeout=timeout)
    return opener(request, timeout=timeout)


def _validate_setup_code(value: str) -> str:
    code = str(value or "")
    if code != code.strip() or not SETUP_CODE_RE.fullmatch(code):
        raise DesktopPairingError("setup_code_invalid")
    return code


def _validate_pairing_id(value: str) -> str:
    normalized = str(value or "").strip()
    try:
        parsed = uuid.UUID(normalized)
    except (ValueError, TypeError, AttributeError) as exc:
        raise DesktopPairingError("pairing_response_invalid") from exc
    if str(parsed) != normalized.casefold():
        raise DesktopPairingError("pairing_response_invalid")
    return normalized


def _parse_deadline(value: str, *, now_func=None) -> datetime:
    rendered = str(value or "").strip()
    try:
        deadline = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise DesktopPairingError("pairing_response_invalid") from exc
    if deadline.tzinfo is None:
        raise DesktopPairingError("pairing_response_invalid")
    now = datetime.fromtimestamp((now_func or time.time)(), tz=timezone.utc)
    if deadline.astimezone(timezone.utc) <= now:
        raise DesktopPairingError("pairing_ack_deadline_expired")
    return deadline.astimezone(timezone.utc)


def _json_request(
    method: str,
    path: str,
    payload: dict,
    *,
    credential: str | None = None,
    timeout: int,
    opener,
) -> dict:
    if not path.startswith("/api/v1/") or "://" in path:
        raise DesktopPairingError("pairing_path_invalid")
    headers = {
        "Accept": "application/json",
        "Cache-Control": "no-store",
        "Content-Type": "application/json",
        "User-Agent": f"{APP_NAME}-desktop-pairing/{APP_VERSION}",
    }
    if credential is not None:
        headers["Authorization"] = f"Bearer {validate_scoped_credential(credential)}"
    request = urllib.request.Request(
        PRODUCTION_BACKEND_ORIGIN + path,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method=method,
    )
    try:
        response = _call_opener(opener, request, timeout)
        try:
            status = int(getattr(response, "status", response.getcode()))
            if status < 200 or status >= 300:
                raise DesktopPairingError(
                    f"pairing_http_{status}",
                    retryable=status in {408, 429, 500, 502, 503, 504},
                )
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        finally:
            response.close()
    except DesktopPairingError:
        raise
    except urllib.error.HTTPError as exc:
        try:
            status = int(exc.code)
        finally:
            exc.close()
        raise DesktopPairingError(
            f"pairing_http_{status}",
            retryable=status in {408, 429, 500, 502, 503, 504},
        ) from exc
    except Exception as exc:
        raise DesktopPairingError("pairing_transport_failed", retryable=True) from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise DesktopPairingError("pairing_response_too_large")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DesktopPairingError("pairing_response_invalid") from exc
    if not isinstance(result, dict):
        raise DesktopPairingError("pairing_response_invalid")
    return result


@dataclass(frozen=True)
class RedeemedDesktopIdentity:
    pairing_id: str
    credential: str
    principal_identifier: str
    ack_deadline: datetime


def redeem_setup_code(
    setup_code: str,
    *,
    timeout: int = TAKSKLAD_BACKEND_TIMEOUT_SECONDS,
    opener=None,
    now_func=None,
) -> RedeemedDesktopIdentity:
    code = _validate_setup_code(setup_code)
    result = _json_request(
        "POST",
        REDEEM_PATH,
        {"setup_code": code, "desktop_version": APP_VERSION},
        timeout=timeout,
        opener=opener or _system_tls_opener(),
    )
    try:
        return RedeemedDesktopIdentity(
            pairing_id=_validate_pairing_id(result.get("pairing_id")),
            credential=validate_scoped_credential(result.get("credential")),
            principal_identifier=validate_principal_identifier(result.get("principal_identifier")),
            ack_deadline=_parse_deadline(result.get("ack_deadline"), now_func=now_func),
        )
    except ReturnsAuthCanaryError as exc:
        raise DesktopPairingError("pairing_response_invalid") from exc


def acknowledge_pairing(
    identity: RedeemedDesktopIdentity,
    *,
    timeout: int = TAKSKLAD_BACKEND_TIMEOUT_SECONDS,
    opener=None,
    now_func=None,
    sleep_func=None,
) -> None:
    opener = opener or _system_tls_opener()
    now_func = now_func or time.time
    sleep_func = sleep_func or time.sleep
    path = ACK_PATH_TEMPLATE.format(pairing_id=identity.pairing_id)
    last_error = None
    for attempt in range(ACK_MAX_ATTEMPTS):
        if datetime.fromtimestamp(now_func(), tz=timezone.utc) >= identity.ack_deadline:
            raise DesktopPairingError("pairing_ack_deadline_expired")
        try:
            result = _json_request(
                "POST",
                path,
                {},
                credential=identity.credential,
                timeout=timeout,
                opener=opener,
            )
            if (
                result.get("pairing_id") != identity.pairing_id
                or result.get("status") != "acked"
                or not isinstance(result.get("credential_expires_at"), str)
                or not result.get("credential_expires_at", "").strip()
            ):
                raise DesktopPairingError("pairing_ack_response_invalid")
            return
        except DesktopPairingError as exc:
            last_error = exc
            if not exc.retryable or attempt + 1 >= ACK_MAX_ATTEMPTS:
                raise DesktopPairingError("pairing_ack_failed") from exc
            remaining = identity.ack_deadline.timestamp() - now_func()
            delay = min(0.5 * (2**attempt), max(0.0, remaining - 0.1))
            if delay <= 0:
                break
            sleep_func(delay)
    raise DesktopPairingError("pairing_ack_failed") from last_error


def _locally_valid_bundle(store) -> bool:
    try:
        value = store.get_text(BACKEND_AUTH_BUNDLE_SECRET)
        credential, identifier = decode_backend_auth_bundle(value)
        validate_scoped_credential(credential)
        validate_principal_identifier(identifier)
        return True
    except (SecretStoreError, ReturnsAuthCanaryError, TypeError):
        return False


def desktop_pairing_required(*, store=None) -> bool:
    secret_store = store or get_secret_store()
    return not _locally_valid_bundle(secret_store)


def pair_desktop_from_setup_code(
    setup_code: str,
    *,
    store=None,
    timeout: int = TAKSKLAD_BACKEND_TIMEOUT_SECONDS,
    opener=None,
    canary_opener=None,
    now_func=None,
    sleep_func=None,
    credential_lock_held: bool = False,
    lock_acquirer=None,
    lock_releaser=None,
) -> bool:
    """Redeem, persist, verify and acknowledge one new desktop identity."""

    _validate_setup_code(setup_code)
    lock_result = None
    if not credential_lock_held:
        acquire = lock_acquirer or acquire_credential_mutation_lock
        try:
            lock_result = acquire()
        except Exception as exc:
            raise DesktopPairingError("workstation_lock_unavailable") from exc
        if not getattr(lock_result, "acquired", False):
            raise DesktopPairingError("workstation_in_use")
    try:
        return _pair_desktop_from_setup_code_locked(
            setup_code,
            store=store,
            timeout=timeout,
            opener=opener,
            canary_opener=canary_opener,
            now_func=now_func,
            sleep_func=sleep_func,
        )
    finally:
        if lock_result is not None and getattr(lock_result, "acquired", False):
            (lock_releaser or release_credential_mutation_lock)(lock_result.lock)


def _pair_desktop_from_setup_code_locked(
    setup_code: str,
    *,
    store=None,
    timeout: int,
    opener=None,
    canary_opener=None,
    now_func=None,
    sleep_func=None,
) -> bool:
    secret_store = store or get_secret_store()
    if not _production_dpapi_store(secret_store):
        raise DesktopPairingError("secure_store_unavailable")
    # A locally valid current identity is authoritative.  Pairing never rotates
    # or overwrites it; rotation remains an explicit administrator operation.
    if _locally_valid_bundle(secret_store):
        return True

    network_opener = opener or _system_tls_opener()
    identity = redeem_setup_code(
        setup_code,
        timeout=timeout,
        opener=network_opener,
        now_func=now_func,
    )

    def ack_verifier(_credential, _identifier):
        acknowledge_pairing(
            identity,
            timeout=timeout,
            opener=network_opener,
            now_func=now_func,
            sleep_func=sleep_func,
        )

    sanitized_output = io.StringIO()
    status = install_scoped_backend_bundle(
        identity.credential,
        identity.principal_identifier,
        timeout=timeout,
        store=secret_store,
        opener=canary_opener or network_opener,
        post_install_verifier=ack_verifier,
        output_stream=sanitized_output,
        error_stream=sanitized_output,
    )
    if status != 0:
        raise DesktopPairingError("credential_install_failed")
    return True


def _pairing_error_message(reason: str) -> str:
    if reason == "setup_code_invalid":
        return "Проверьте код подключения и попробуйте снова."
    if reason in {"pairing_http_401", "pairing_http_403", "pairing_http_404", "pairing_http_409"}:
        return "Код подключения недействителен или уже использован."
    if reason == "secure_store_unavailable":
        return "Защищённое хранилище Windows недоступно."
    return "Не удалось подключить этот компьютер. Проверьте интернет и повторите попытку."


def run_desktop_pairing_dialog(
    *,
    store=None,
    pairing_func=None,
    credential_lock_held: bool = False,
) -> bool:
    """Show pairing UI only when the local DPAPI identity is absent/invalid."""

    secret_store = store or get_secret_store()
    if _locally_valid_bundle(secret_store):
        return True
    if not _production_dpapi_store(secret_store):
        return False

    pair = pairing_func or (
        lambda code: pair_desktop_from_setup_code(
            code,
            store=secret_store,
            credential_lock_held=credential_lock_held,
        )
    )
    result = {"paired": False, "busy": False}
    window = tk.Tk()
    window.title("Подключение TakSklad")
    window.resizable(False, False)
    window.configure(bg="#f6f4ef")

    frame = ttk.Frame(window, padding=24)
    frame.grid(row=0, column=0, sticky="nsew")
    ttk.Label(frame, text="Подключение складского компьютера", font=("Segoe UI", 15, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w"
    )
    ttk.Label(
        frame,
        text="Введите одноразовый код из панели TakSklad.",
        font=("Segoe UI", 10),
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 14))
    code_var = tk.StringVar()
    entry = ttk.Entry(frame, textvariable=code_var, show="•", width=46, font=("Consolas", 11))
    entry.grid(row=2, column=0, columnspan=2, sticky="ew")
    status_var = tk.StringVar(value="")
    status = ttk.Label(frame, textvariable=status_var, foreground="#a52a1a", wraplength=430)
    status.grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 4))
    cancel_button = ttk.Button(frame, text="Отмена")
    submit_button = ttk.Button(frame, text="Подключить")
    cancel_button.grid(row=4, column=0, sticky="w", pady=(12, 0))
    submit_button.grid(row=4, column=1, sticky="e", pady=(12, 0))

    def close():
        if not result["busy"]:
            window.destroy()

    def finish_success():
        result["paired"] = True
        result["busy"] = False
        window.destroy()

    def finish_failure(reason):
        result["busy"] = False
        status_var.set(_pairing_error_message(reason))
        submit_button.configure(state="normal")
        cancel_button.configure(state="normal")
        entry.configure(state="normal")
        entry.focus_set()

    def worker(code):
        try:
            pair(code)
        except DesktopPairingError as exc:
            window.after(0, finish_failure, exc.reason)
        except Exception:
            window.after(0, finish_failure, "unexpected_failure")
        else:
            window.after(0, finish_success)

    def submit(_event=None):
        if result["busy"]:
            return
        code = code_var.get()
        code_var.set("")
        try:
            _validate_setup_code(code)
        except DesktopPairingError as exc:
            status_var.set(_pairing_error_message(exc.reason))
            return
        result["busy"] = True
        status_var.set("Подключаем компьютер…")
        submit_button.configure(state="disabled")
        cancel_button.configure(state="disabled")
        entry.configure(state="disabled")
        threading.Thread(target=worker, args=(code,), daemon=True).start()

    cancel_button.configure(command=close)
    submit_button.configure(command=submit)
    window.bind("<Return>", submit)
    window.protocol("WM_DELETE_WINDOW", close)
    window.update_idletasks()
    width = max(window.winfo_width(), 510)
    height = max(window.winfo_height(), 230)
    x = max(0, (window.winfo_screenwidth() - width) // 2)
    y = max(0, (window.winfo_screenheight() - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")
    entry.focus_set()
    window.mainloop()
    return bool(result["paired"])
