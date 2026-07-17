"""Secret storage primitives for the TakSklad desktop application.

The production Windows provider encrypts the complete store with user-scoped
DPAPI and keeps the store directory/file restricted to the current user and
LOCAL SYSTEM.  Non-Windows providers are deliberately opt-in and intended for
development or tests only; there is no plaintext file fallback.
"""

from __future__ import annotations

import argparse
import base64
import csv
import ctypes
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Mapping, MutableMapping, Optional


TELEGRAM_BOT_TOKEN_SECRET = "telegram_bot_token"
BACKEND_API_TOKEN_SECRET = "backend_api_token"
BACKEND_PRINCIPAL_IDENTIFIER_SECRET = "backend_principal_identifier"
BACKEND_AUTH_BUNDLE_SECRET = "backend_auth_bundle"
GEOCODER_API_KEY_SECRET = "geocoder_api_key"

_SYNTHETIC_SECRET = "phase11_synthetic_secret"
_STORE_FORMAT = "taksklad.dpapi.v1"
_STORE_PAYLOAD_VERSION = 1
_SYSTEM_SID = "S-1-5-18"
_DPAPI_DESCRIPTION = "TakSklad desktop secret store"
_DPAPI_ENTROPY = b"TakSklad/desktop-secret-store/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_ERROR_ACCESS_DENIED = 5
_LOGON32_LOGON_INTERACTIVE = 2
_LOGON32_PROVIDER_DEFAULT = 0
_MAX_SECRET_TEXT_BYTES = 2 * 1024 * 1024
_MAX_STORE_PAYLOAD_BYTES = 3 * 1024 * 1024
_MAX_STORE_FILE_BYTES = 6 * 1024 * 1024
_MAX_SECRET_COUNT = 64
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_BACKEND_AUTH_BUNDLE_VERSION = 1
_BACKEND_AUTH_BUNDLE_KEYS = {"version", "credential", "principal_identifier"}


class SecretStoreError(RuntimeError):
    """Base error that never contains a secret value."""


class SecretStoreUnavailable(SecretStoreError):
    """The requested provider is not available in the current environment."""


class SecretStoreAccessDenied(SecretStoreError):
    """The current identity cannot decrypt or read the protected store."""


class SecretStoreCorrupt(SecretStoreError):
    """The protected store is malformed or cannot be validated."""


def encode_backend_auth_bundle(credential: str, principal_identifier: str) -> str:
    """Serialize the desktop backend identity as one protected atomic record."""
    credential = _validate_text(credential)
    principal_identifier = _validate_text(principal_identifier)
    if not credential or not principal_identifier:
        raise SecretStoreError("backend auth bundle fields are required")
    return json.dumps(
        {
            "version": _BACKEND_AUTH_BUNDLE_VERSION,
            "credential": credential,
            "principal_identifier": principal_identifier,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def decode_backend_auth_bundle(value: str) -> tuple[str, str]:
    """Parse one protected backend identity without accepting partial records."""
    try:
        payload = json.loads(_validate_text(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SecretStoreCorrupt("backend auth bundle is malformed") from exc
    if not isinstance(payload, dict) or set(payload) != _BACKEND_AUTH_BUNDLE_KEYS:
        raise SecretStoreCorrupt("backend auth bundle shape is invalid")
    if payload.get("version") != _BACKEND_AUTH_BUNDLE_VERSION:
        raise SecretStoreCorrupt("backend auth bundle version is unsupported")
    credential = payload.get("credential")
    identifier = payload.get("principal_identifier")
    if not isinstance(credential, str) or not credential:
        raise SecretStoreCorrupt("backend auth bundle credential is invalid")
    if not isinstance(identifier, str) or not identifier:
        raise SecretStoreCorrupt("backend auth bundle identifier is invalid")
    return credential, identifier


def load_backend_auth_bundle(store=None) -> tuple[str, str]:
    """Load only the authoritative atomic bundle; legacy fields are never mixed."""
    secret_store = store or get_secret_store()
    value = secret_store.get_text(BACKEND_AUTH_BUNDLE_SECRET)
    if value is None:
        raise SecretStoreError("backend auth bundle is missing")
    return decode_backend_auth_bundle(value)


def _validate_name(name: str) -> str:
    normalized = str(name or "").strip()
    if not _NAME_PATTERN.fullmatch(normalized):
        raise SecretStoreError("invalid secret name")
    return normalized


def _validate_text(value: str) -> str:
    if not isinstance(value, str):
        raise SecretStoreError("secret value must be text")
    if len(value.encode("utf-8")) > _MAX_SECRET_TEXT_BYTES:
        raise SecretStoreError("secret value exceeds the storage limit")
    return value


class MemorySecretStore:
    """Explicit in-memory provider for tests and local development."""

    provider_name = "memory"

    def __init__(self, initial: Optional[Mapping[str, str]] = None):
        self._values = {
            _validate_name(name): _validate_text(value)
            for name, value in dict(initial or {}).items()
        }
        self._lock = threading.RLock()

    def get_text(self, name: str) -> Optional[str]:
        with self._lock:
            return self._values.get(_validate_name(name))

    def set_text(self, name: str, value: str) -> bool:
        with self._lock:
            self._values[_validate_name(name)] = _validate_text(value)
        return True

    def delete(self, name: str) -> bool:
        with self._lock:
            return self._values.pop(_validate_name(name), None) is not None

    def status(self) -> dict:
        with self._lock:
            return {
                "provider": self.provider_name,
                "available": True,
                "persistent": False,
                "configured": bool(self._values),
            }


class EnvironmentSecretStore:
    """Read-only environment provider selected explicitly for dev/test use."""

    provider_name = "environment"
    DEFAULT_ENVIRONMENT_MAPPING = {
        TELEGRAM_BOT_TOKEN_SECRET: "TAKSKLAD_TELEGRAM_BOT_TOKEN",
        BACKEND_API_TOKEN_SECRET: "TAKSKLAD_BACKEND_API_TOKEN",
        BACKEND_AUTH_BUNDLE_SECRET: "TAKSKLAD_BACKEND_AUTH_BUNDLE",
        GEOCODER_API_KEY_SECRET: "YANDEX_GEOCODER_API_KEY",
        _SYNTHETIC_SECRET: "TAKSKLAD_SYNTHETIC_SECRET",
    }

    def __init__(
        self,
        mapping: Optional[Mapping[str, str]] = None,
        environ: Optional[MutableMapping[str, str]] = None,
    ):
        self._mapping = {
            _validate_name(name): str(env_name or "").strip()
            for name, env_name in dict(mapping or self.DEFAULT_ENVIRONMENT_MAPPING).items()
        }
        if any(not env_name for env_name in self._mapping.values()):
            raise SecretStoreError("environment variable name is required")
        self._environ = os.environ if environ is None else environ

    def get_text(self, name: str) -> Optional[str]:
        env_name = self._mapping.get(_validate_name(name))
        if not env_name:
            return None
        value = self._environ.get(env_name)
        return value if isinstance(value, str) else None

    def set_text(self, name: str, value: str) -> bool:
        _validate_name(name)
        _validate_text(value)
        raise SecretStoreError("environment secret store is read-only")

    def delete(self, name: str) -> bool:
        _validate_name(name)
        raise SecretStoreError("environment secret store is read-only")

    def status(self) -> dict:
        configured = any(bool(self._environ.get(env_name)) for env_name in self._mapping.values())
        return {
            "provider": self.provider_name,
            "available": True,
            "persistent": False,
            "read_only": True,
            "configured": configured,
        }


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob_from_bytes(value: bytes):
    buffer = ctypes.create_string_buffer(value, len(value))
    blob = _DataBlob(
        len(value),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return blob, buffer


def _windows_libraries():
    if os.name != "nt":
        raise SecretStoreUnavailable("Windows DPAPI is unavailable on this platform")
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError) as exc:
        raise SecretStoreUnavailable("Windows DPAPI libraries are unavailable") from exc

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _dpapi_protect(plaintext: bytes) -> bytes:
    crypt32, kernel32 = _windows_libraries()
    input_blob, input_buffer = _blob_from_bytes(plaintext)
    entropy_blob, entropy_buffer = _blob_from_bytes(_DPAPI_ENTROPY)
    output_blob = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        _DPAPI_DESCRIPTION,
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        error = ctypes.get_last_error()
        raise SecretStoreError(f"DPAPI encryption failed (winerror={error})")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if output_blob.pbData:
            kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def _dpapi_unprotect(ciphertext: bytes) -> bytes:
    crypt32, kernel32 = _windows_libraries()
    input_blob, input_buffer = _blob_from_bytes(ciphertext)
    entropy_blob, entropy_buffer = _blob_from_bytes(_DPAPI_ENTROPY)
    output_blob = _DataBlob()
    description = wintypes.LPWSTR()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        ctypes.byref(description),
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        # Windows returns different codes for an alternate profile depending on
        # the OS build and profile state (for example ERROR_INVALID_DATA or an
        # NTE key-state error).  The ciphertext wrapper has already been
        # validated, so all CryptUnprotectData failures are fail-closed access
        # denial at this boundary; no raw system message is surfaced.
        raise SecretStoreAccessDenied("DPAPI current-user decryption denied")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if description:
            kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))
        if output_blob.pbData:
            kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def _current_user_sid() -> str:
    if os.name != "nt":
        raise SecretStoreUnavailable("Windows ACL is unavailable on this platform")
    try:
        completed = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SecretStoreUnavailable("cannot resolve the current Windows SID") from exc
    if completed.returncode != 0:
        raise SecretStoreUnavailable("cannot resolve the current Windows SID")
    try:
        fields = next(csv.reader([completed.stdout.strip()]))
    except (csv.Error, StopIteration) as exc:
        raise SecretStoreUnavailable("cannot parse the current Windows SID") from exc
    for field in reversed(fields):
        candidate = str(field or "").strip()
        if re.fullmatch(r"S-\d(?:-\d+)+", candidate, flags=re.IGNORECASE):
            return candidate
    raise SecretStoreUnavailable("cannot resolve the current Windows SID")


def _run_icacls(arguments) -> None:
    try:
        completed = subprocess.run(
            ["icacls", *arguments],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SecretStoreError("Windows ACL update failed") from exc
    if completed.returncode != 0:
        raise SecretStoreError(f"Windows ACL update failed (exit={completed.returncode})")


def _restrict_windows_acl(path: Path, *, directory: bool) -> None:
    sid = _current_user_sid()
    rights = "(OI)(CI)F" if directory else "F"
    _run_icacls(
        [
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"*{sid}:{rights}",
            f"*{_SYSTEM_SID}:{rights}",
            "/q",
        ]
    )
    _run_icacls([str(path), "/setowner", f"*{sid}", "/q"])


class WindowsDpapiSecretStore:
    """Persistent Windows secret store encrypted for the current user only."""

    provider_name = "windows_dpapi"

    def __init__(self, store_file: Optional[os.PathLike] = None):
        if os.name != "nt":
            raise SecretStoreUnavailable("Windows DPAPI secret store requires Windows")
        default_root = Path(os.environ.get("LOCALAPPDATA") or "") / "TakSklad" / "secrets"
        if store_file is None:
            if not os.environ.get("LOCALAPPDATA"):
                raise SecretStoreUnavailable("LOCALAPPDATA is unavailable")
            self.store_file = default_root / "secret_store.v1.dpapi"
        else:
            self.store_file = Path(store_file).expanduser().absolute()
        self._lock = threading.RLock()

    def _assert_safe_paths(self) -> None:
        for candidate in (self.store_file.parent, self.store_file):
            if candidate.exists() and candidate.is_symlink():
                raise SecretStoreError("secret store path must not be a symbolic link")

    def _read_all(self) -> dict:
        self._assert_safe_paths()
        if not self.store_file.exists():
            return {}
        try:
            if self.store_file.stat().st_size > _MAX_STORE_FILE_BYTES:
                raise SecretStoreCorrupt("secret store exceeds the storage limit")
            wrapper = json.loads(self.store_file.read_text(encoding="utf-8"))
            if (
                not isinstance(wrapper, dict)
                or wrapper.get("format") != _STORE_FORMAT
                or wrapper.get("scope") != "current_user"
            ):
                raise SecretStoreCorrupt("secret store format is invalid")
            encoded = wrapper.get("ciphertext")
            if not isinstance(encoded, str) or not encoded:
                raise SecretStoreCorrupt("secret store ciphertext is missing")
            ciphertext = base64.b64decode(encoded.encode("ascii"), validate=True)
            plaintext = _dpapi_unprotect(ciphertext)
            if len(plaintext) > _MAX_STORE_PAYLOAD_BYTES:
                raise SecretStoreCorrupt("secret store payload exceeds the storage limit")
            payload = json.loads(plaintext.decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != _STORE_PAYLOAD_VERSION:
                raise SecretStoreCorrupt("secret store payload version is invalid")
            values = payload.get("secrets")
            if not isinstance(values, dict):
                raise SecretStoreCorrupt("secret store payload is invalid")
            if len(values) > _MAX_SECRET_COUNT:
                raise SecretStoreCorrupt("secret store contains too many entries")
            normalized = {}
            for name, value in values.items():
                normalized[_validate_name(name)] = _validate_text(value)
            return normalized
        except SecretStoreError:
            raise
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise SecretStoreCorrupt("secret store cannot be decoded") from exc

    def _write_all(self, values: Mapping[str, str]) -> None:
        self._assert_safe_paths()
        parent = self.store_file.parent
        parent.mkdir(parents=True, exist_ok=True)
        _restrict_windows_acl(parent, directory=True)
        try:
            original_bytes = self.store_file.read_bytes() if self.store_file.exists() else None
        except OSError as exc:
            raise SecretStoreError("secret store original cannot be read") from exc

        normalized = {
            _validate_name(name): _validate_text(value)
            for name, value in dict(values).items()
        }
        if len(normalized) > _MAX_SECRET_COUNT:
            raise SecretStoreError("secret store contains too many entries")
        payload = json.dumps(
            {"version": _STORE_PAYLOAD_VERSION, "secrets": normalized},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > _MAX_STORE_PAYLOAD_BYTES:
            raise SecretStoreError("secret store payload exceeds the storage limit")
        wrapper = {
            "format": _STORE_FORMAT,
            "scope": "current_user",
            "ciphertext": base64.b64encode(_dpapi_protect(payload)).decode("ascii"),
        }

        temp_path = None
        try:
            descriptor, raw_temp_path = tempfile.mkstemp(
                prefix=self.store_file.name + ".",
                suffix=".tmp",
                dir=str(parent),
                text=True,
            )
            temp_path = Path(raw_temp_path)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file_obj:
                json.dump(wrapper, file_obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                file_obj.flush()
                os.fsync(file_obj.fileno())
            _restrict_windows_acl(temp_path, directory=False)
            os.replace(str(temp_path), str(self.store_file))
            temp_path = None
            _restrict_windows_acl(self.store_file, directory=False)
            if self._read_all() != normalized:
                raise SecretStoreError("secret store encrypted round-trip verification failed")
        except (SecretStoreError, OSError) as exc:
            try:
                if original_bytes is None:
                    self.store_file.unlink(missing_ok=True)
                else:
                    restore_descriptor, restore_name = tempfile.mkstemp(
                        prefix=self.store_file.name + ".restore.",
                        suffix=".tmp",
                        dir=str(parent),
                    )
                    restore_path = Path(restore_name)
                    try:
                        with os.fdopen(restore_descriptor, "wb") as restore_file:
                            restore_file.write(original_bytes)
                            restore_file.flush()
                            os.fsync(restore_file.fileno())
                        _restrict_windows_acl(restore_path, directory=False)
                        os.replace(str(restore_path), str(self.store_file))
                        _restrict_windows_acl(self.store_file, directory=False)
                    finally:
                        restore_path.unlink(missing_ok=True)
            except (OSError, SecretStoreError) as restore_exc:
                raise SecretStoreError("secret store write rollback failed") from restore_exc
            if isinstance(exc, SecretStoreError):
                raise
            raise SecretStoreError("secret store atomic write failed") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def get_text(self, name: str) -> Optional[str]:
        with self._lock:
            return self._read_all().get(_validate_name(name))

    def set_text(self, name: str, value: str) -> bool:
        with self._lock:
            values = self._read_all()
            values[_validate_name(name)] = _validate_text(value)
            self._write_all(values)
        return True

    def delete(self, name: str) -> bool:
        with self._lock:
            values = self._read_all()
            normalized_name = _validate_name(name)
            if normalized_name not in values:
                return False
            del values[normalized_name]
            self._write_all(values)
        return True

    def status(self) -> dict:
        try:
            with self._lock:
                configured = bool(self._read_all())
            return {
                "provider": self.provider_name,
                "available": True,
                "persistent": True,
                "scope": "current_user",
                "location": "windows_user_profile",
                "configured": configured,
                "state": "ok",
            }
        except SecretStoreError:
            return {
                "provider": self.provider_name,
                "available": False,
                "persistent": True,
                "scope": "current_user",
                "location": "windows_user_profile",
                "configured": False,
                "state": "failed_closed",
            }


_SECRET_STORE_LOCK = threading.RLock()
_SECRET_STORE_SINGLETON = None
_SECRET_STORE_TEST_OVERRIDE = None


def _development_provider_allowed() -> bool:
    if bool(getattr(sys, "frozen", False)):
        return False
    return os.environ.get("TAKSKLAD_SECRET_STORE_MODE", "").strip().lower() in {
        "development",
        "test",
    }


def get_secret_store():
    global _SECRET_STORE_SINGLETON
    with _SECRET_STORE_LOCK:
        if _SECRET_STORE_TEST_OVERRIDE is not None:
            return _SECRET_STORE_TEST_OVERRIDE
        if _SECRET_STORE_SINGLETON is not None:
            return _SECRET_STORE_SINGLETON

        selected = os.environ.get("TAKSKLAD_SECRET_STORE_PROVIDER", "").strip().lower()
        if selected in {"environment", "env", "memory", "mock"}:
            if not _development_provider_allowed():
                raise SecretStoreUnavailable(
                    "non-Windows secret provider requires explicit development/test mode"
                )
            if selected in {"environment", "env"}:
                _SECRET_STORE_SINGLETON = EnvironmentSecretStore()
            else:
                _SECRET_STORE_SINGLETON = MemorySecretStore()
            return _SECRET_STORE_SINGLETON

        if selected not in {"", "windows", "windows_dpapi", "dpapi"}:
            raise SecretStoreUnavailable("unknown secret store provider")
        if os.name != "nt":
            raise SecretStoreUnavailable(
                "production secret store requires Windows DPAPI; no plaintext fallback is available"
            )
        _SECRET_STORE_SINGLETON = WindowsDpapiSecretStore()
        return _SECRET_STORE_SINGLETON


def set_secret_store_for_tests(store) -> None:
    global _SECRET_STORE_TEST_OVERRIDE
    if store is None:
        raise SecretStoreError("test secret store override is required")
    for method in ("get_text", "set_text", "delete", "status"):
        if not callable(getattr(store, method, None)):
            raise SecretStoreError("test secret store override is invalid")
    with _SECRET_STORE_LOCK:
        _SECRET_STORE_TEST_OVERRIDE = store


def reset_secret_store_for_tests() -> None:
    global _SECRET_STORE_SINGLETON, _SECRET_STORE_TEST_OVERRIDE
    with _SECRET_STORE_LOCK:
        _SECRET_STORE_SINGLETON = None
        _SECRET_STORE_TEST_OVERRIDE = None


def load_secret(name: str) -> Optional[str]:
    return get_secret_store().get_text(_validate_name(name))


def save_secret(name: str, value: str) -> bool:
    return bool(get_secret_store().set_text(_validate_name(name), _validate_text(value)))


def delete_secret(name: str) -> bool:
    return bool(get_secret_store().delete(_validate_name(name)))


def _require_synthetic_path(raw_path: str) -> Path:
    if os.environ.get("TAKSKLAD_SYNTHETIC_ONLY") != "1":
        raise SecretStoreError("synthetic probe guard is not enabled")
    root_value = os.environ.get("TAKSKLAD_SYNTHETIC_ROOT", "").strip()
    if not root_value:
        raise SecretStoreError("synthetic probe root is not configured")
    root = Path(root_value).expanduser().absolute()
    path = Path(raw_path).expanduser().absolute()
    try:
        if os.path.commonpath([str(root), str(path)]) != str(root):
            raise SecretStoreError("synthetic probe path is outside the test root")
    except ValueError as exc:
        raise SecretStoreError("synthetic probe path is outside the test root") from exc
    return path


def _write_digest(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(hashlib.sha256(value.encode("utf-8")).hexdigest(), encoding="ascii")


def _read_digest(path: Path) -> str:
    digest = path.read_text(encoding="ascii").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SecretStoreError("synthetic digest is invalid")
    return digest


def _synthetic_probe(arguments) -> int:
    if arguments.command == "synthetic-expect-alternate-denied":
        private_store_file = _require_synthetic_path(arguments.private_store_file)
        copied_store_file = _require_synthetic_path(arguments.copied_store_file)
        username = os.environ.get("TAKSKLAD_SYNTHETIC_ALT_USERNAME", "").strip()
        password = os.environ.get("TAKSKLAD_SYNTHETIC_ALT_PASSWORD", "")
        if not username or not password or os.name != "nt":
            return 20
        domain = "."
        if "\\" in username:
            domain, username = username.split("\\", 1)
        if not domain or not username:
            return 20

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32.LogonUserW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.LogonUserW.restype = wintypes.BOOL
        advapi32.ImpersonateLoggedOnUser.argtypes = [wintypes.HANDLE]
        advapi32.ImpersonateLoggedOnUser.restype = wintypes.BOOL
        advapi32.RevertToSelf.argtypes = []
        advapi32.RevertToSelf.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        token = wintypes.HANDLE()
        if not advapi32.LogonUserW(
            username,
            domain,
            password,
            _LOGON32_LOGON_INTERACTIVE,
            _LOGON32_PROVIDER_DEFAULT,
            ctypes.byref(token),
        ):
            return 21
        impersonating = False
        try:
            if not advapi32.ImpersonateLoggedOnUser(token):
                return 22
            impersonating = True
            try:
                with private_store_file.open("rb") as file_obj:
                    file_obj.read(1)
            except PermissionError:
                pass
            except OSError as exc:
                if getattr(exc, "winerror", None) != _ERROR_ACCESS_DENIED:
                    return 13
            else:
                return 11

            try:
                with copied_store_file.open("rb") as file_obj:
                    if not file_obj.read(1):
                        return 13
            except OSError:
                return 13
            try:
                WindowsDpapiSecretStore(copied_store_file).get_text(_SYNTHETIC_SECRET)
            except SecretStoreAccessDenied:
                return 0
            except SecretStoreError:
                return 14
            return 12
        finally:
            if impersonating and not advapi32.RevertToSelf():
                kernel32.CloseHandle(token)
                raise SecretStoreError("synthetic alternate-user revert failed")
            kernel32.CloseHandle(token)

    store_file = _require_synthetic_path(arguments.store_file)
    if arguments.command == "synthetic-expect-acl-denied":
        try:
            with store_file.open("rb") as file_obj:
                file_obj.read(1)
        except PermissionError:
            return 0
        except OSError as exc:
            if getattr(exc, "winerror", None) == _ERROR_ACCESS_DENIED:
                return 0
            return 4
        return 3

    store = WindowsDpapiSecretStore(store_file)
    if arguments.command == "synthetic-write":
        digest_file = _require_synthetic_path(arguments.digest_file)
        value = secrets.token_urlsafe(72)
        store.set_text(_SYNTHETIC_SECRET, value)
        _write_digest(digest_file, value)
        return 0
    if arguments.command == "synthetic-verify":
        digest_file = _require_synthetic_path(arguments.digest_file)
        value = store.get_text(_SYNTHETIC_SECRET)
        if not isinstance(value, str):
            return 3
        actual_digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return 0 if secrets.compare_digest(actual_digest, _read_digest(digest_file)) else 3
    if arguments.command == "synthetic-expect-dpapi-denied":
        try:
            store.get_text(_SYNTHETIC_SECRET)
        except SecretStoreAccessDenied:
            return 0
        except SecretStoreError:
            return 4
        return 3
    return 2


def _build_probe_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in (
        "synthetic-write",
        "synthetic-verify",
        "synthetic-expect-dpapi-denied",
        "synthetic-expect-acl-denied",
    ):
        child = subparsers.add_parser(command, add_help=False)
        child.add_argument("--store-file", required=True)
        if command in {"synthetic-write", "synthetic-verify"}:
            child.add_argument("--digest-file", required=True)
    alternate = subparsers.add_parser("synthetic-expect-alternate-denied", add_help=False)
    alternate.add_argument("--private-store-file", required=True)
    alternate.add_argument("--copied-store-file", required=True)
    return parser


def _main(argv=None) -> int:
    try:
        arguments = _build_probe_parser().parse_args(argv)
        return _synthetic_probe(arguments)
    except (SecretStoreError, OSError, ValueError):
        return 2


if __name__ == "__main__":
    sys.exit(_main())
