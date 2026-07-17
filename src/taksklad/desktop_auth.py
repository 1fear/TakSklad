"""Safe Windows DPAPI provisioning and returns-auth acceptance commands."""

from __future__ import annotations

import hmac
import sys

from .returns_auth_canary import (
    PRODUCTION_BACKEND_ORIGIN,
    ReturnsAuthCanaryError,
    read_credential_from_stdin,
    run_returns_auth_canary,
    validate_principal_identifier,
    validate_scoped_credential,
)
from .secret_store import (
    BACKEND_AUTH_BUNDLE_SECRET,
    BACKEND_API_TOKEN_SECRET,
    BACKEND_PRINCIPAL_IDENTIFIER_SECRET,
    SecretStoreError,
    decode_backend_auth_bundle,
    encode_backend_auth_bundle,
    get_secret_store,
)
from .credential_lock import (
    acquire_credential_mutation_lock,
    release_credential_mutation_lock,
)


def _production_dpapi_store(store):
    status = store.status()
    return (
        isinstance(status, dict)
        and status.get("provider") == "windows_dpapi"
        and status.get("available") is True
        and status.get("persistent") is True
        and status.get("scope") == "current_user"
        and status.get("state") == "ok"
    )


def _same_optional_secret(left, right) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return isinstance(left, str) and isinstance(right, str) and hmac.compare_digest(left, right)


def _same_bundle(left, right) -> bool:
    return _same_optional_secret(left, right)


def _validated_bundle(value: str) -> tuple[str, str]:
    token, identifier = decode_backend_auth_bundle(value)
    validate_scoped_credential(token)
    return token, validate_principal_identifier(identifier)


def _restore_bundle(store, previous) -> None:
    if previous is None:
        store.delete(BACKEND_AUTH_BUNDLE_SECRET)
    else:
        store.set_text(BACKEND_AUTH_BUNDLE_SECRET, previous)
    if not _same_bundle(store.get_text(BACKEND_AUTH_BUNDLE_SECRET), previous):
        raise SecretStoreError("secure store rollback verification failed")


def _cleanup_legacy_pair(store) -> None:
    """Remove legacy records only after an authoritative bundle is durable."""
    store.delete(BACKEND_API_TOKEN_SECRET)
    store.delete(BACKEND_PRINCIPAL_IDENTIFIER_SECRET)


def _read_or_migrate_bundle(store) -> tuple[str, str]:
    """Read the bundle, or atomically publish one from a complete legacy pair."""
    bundle = store.get_text(BACKEND_AUTH_BUNDLE_SECRET)
    if bundle is not None:
        pair = _validated_bundle(bundle)
        # A crash during cleanup cannot create a mixed identity: every reader
        # above prefers the already-verified bundle.
        _cleanup_legacy_pair(store)
        return pair

    legacy_token = store.get_text(BACKEND_API_TOKEN_SECRET)
    legacy_identifier = store.get_text(BACKEND_PRINCIPAL_IDENTIFIER_SECRET)
    if legacy_token is None and legacy_identifier is None:
        raise SecretStoreError("backend auth bundle is missing")
    if legacy_token is None or legacy_identifier is None:
        raise SecretStoreError("legacy backend auth pair is incomplete")
    validate_scoped_credential(legacy_token)
    legacy_identifier = validate_principal_identifier(legacy_identifier)
    encoded = encode_backend_auth_bundle(legacy_token, legacy_identifier)
    store.set_text(BACKEND_AUTH_BUNDLE_SECRET, encoded)
    stored = store.get_text(BACKEND_AUTH_BUNDLE_SECRET)
    if not _same_bundle(stored, encoded):
        raise SecretStoreError("backend auth bundle migration round trip failed")
    pair = _validated_bundle(stored)
    _cleanup_legacy_pair(store)
    return pair


def _install_scoped_backend_token_from_stdin(
    base_url: str = PRODUCTION_BACKEND_ORIGIN,
    *,
    expected_identifier: str,
    timeout: int = 8,
    input_stream=None,
    output_stream=None,
    error_stream=None,
    store=None,
    opener=None,
) -> int:
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    error_stream = error_stream or sys.stderr
    try:
        secret_store = store or get_secret_store()
        if not _production_dpapi_store(secret_store):
            raise SecretStoreError("production DPAPI store unavailable")
        expected_identifier = validate_principal_identifier(expected_identifier)
        token = read_credential_from_stdin(input_stream)
        validate_scoped_credential(token)
    except (ReturnsAuthCanaryError, SecretStoreError):
        print("DESKTOP_AUTH_INSTALL_BLOCKED reason=secure_store_or_credential_invalid", file=error_stream)
        return 2
    except Exception:
        print("DESKTOP_AUTH_INSTALL_BLOCKED reason=unexpected_failure", file=error_stream)
        return 2

    canary_kwargs = {
        "timeout": timeout,
        "require_scoped": True,
        "canary_kind": "desktop",
        "identifier": expected_identifier,
    }
    if opener is not None:
        canary_kwargs["opener"] = opener
    try:
        run_returns_auth_canary(base_url, token, **canary_kwargs)
    except Exception:
        print("DESKTOP_AUTH_INSTALL_BLOCKED reason=backend_preflight_failed", file=error_stream)
        return 2

    try:
        previous = secret_store.get_text(BACKEND_AUTH_BUNDLE_SECRET)
    except Exception:
        print("DESKTOP_AUTH_INSTALL_BLOCKED reason=secure_store_read_failed", file=error_stream)
        return 2

    encoded_bundle = encode_backend_auth_bundle(token, expected_identifier)
    mutation_attempted = False
    bundle_verified = False
    try:
        mutation_attempted = True
        secret_store.set_text(BACKEND_AUTH_BUNDLE_SECRET, encoded_bundle)
        stored_bundle = secret_store.get_text(BACKEND_AUTH_BUNDLE_SECRET)
        if not _same_bundle(stored_bundle, encoded_bundle):
            raise SecretStoreError("secure store round trip failed")
        stored = _validated_bundle(stored_bundle)
        run_returns_auth_canary(
            base_url,
            stored[0],
            **{**canary_kwargs, "identifier": stored[1]},
        )
        bundle_verified = True
    except Exception:
        if mutation_attempted:
            try:
                current = secret_store.get_text(BACKEND_AUTH_BUNDLE_SECRET)
                if _same_bundle(current, previous):
                    print(
                        "DESKTOP_AUTH_INSTALL_BLOCKED reason=secure_store_write_failed_unchanged",
                        file=error_stream,
                    )
                    return 2
                if not _same_bundle(current, encoded_bundle):
                    print(
                        "DESKTOP_AUTH_INSTALL_FATAL reason=concurrent_store_change_manual_recovery_required",
                        file=error_stream,
                    )
                    return 4
                _restore_bundle(secret_store, previous)
            except Exception:
                print("DESKTOP_AUTH_INSTALL_FATAL reason=rollback_failed", file=error_stream)
                return 4
        print("DESKTOP_AUTH_INSTALL_BLOCKED reason=stored_credential_verification_failed", file=error_stream)
        return 2

    try:
        _cleanup_legacy_pair(secret_store)
    except Exception:
        # The verified bundle remains authoritative. Rolling it back here could
        # expose a partially-cleaned legacy pair after a crash or write error.
        if bundle_verified:
            print(
                "DESKTOP_AUTH_INSTALL_FATAL reason=legacy_cleanup_manual_recovery_required",
                file=error_stream,
            )
            return 4
        raise

    print(
        "DESKTOP_AUTH_INSTALL_OK provider=windows_dpapi scope=current_user credential=scoped",
        file=output_stream,
    )
    return 0


def install_scoped_backend_token_from_stdin(
    base_url: str = PRODUCTION_BACKEND_ORIGIN,
    *,
    expected_identifier: str,
    timeout: int = 8,
    input_stream=None,
    output_stream=None,
    error_stream=None,
    store=None,
    opener=None,
    lock_acquirer=None,
    lock_releaser=None,
) -> int:
    error_stream = error_stream or sys.stderr
    acquire = lock_acquirer or acquire_credential_mutation_lock
    release = lock_releaser or release_credential_mutation_lock
    try:
        lock_result = acquire()
    except Exception:
        print("DESKTOP_AUTH_INSTALL_BLOCKED reason=workstation_lock_unavailable", file=error_stream)
        return 2
    if not getattr(lock_result, "acquired", False):
        print("DESKTOP_AUTH_INSTALL_BLOCKED reason=workstation_in_use", file=error_stream)
        return 2
    try:
        return _install_scoped_backend_token_from_stdin(
            base_url,
            expected_identifier=expected_identifier,
            timeout=timeout,
            input_stream=input_stream,
            output_stream=output_stream,
            error_stream=error_stream,
            store=store,
            opener=opener,
        )
    finally:
        release(lock_result.lock)


def run_desktop_returns_auth_canary(
    base_url: str = PRODUCTION_BACKEND_ORIGIN,
    *,
    timeout: int = 8,
    output_stream=None,
    error_stream=None,
    store=None,
    opener=None,
) -> int:
    output_stream = output_stream or sys.stdout
    error_stream = error_stream or sys.stderr
    try:
        secret_store = store or get_secret_store()
        if not _production_dpapi_store(secret_store):
            raise SecretStoreError("production DPAPI store unavailable")
        token, identifier = _read_or_migrate_bundle(secret_store)
        kwargs = {"timeout": timeout, "canary_kind": "desktop", "identifier": identifier}
        if opener is not None:
            kwargs["opener"] = opener
        result = run_returns_auth_canary(base_url, token, require_scoped=True, **kwargs)
    except (ReturnsAuthCanaryError, SecretStoreError):
        print("DESKTOP_RETURNS_AUTH_CANARY_BLOCKED reason=credential_or_read_path_failed", file=error_stream)
        return 3
    except Exception:
        print("DESKTOP_RETURNS_AUTH_CANARY_BLOCKED reason=unexpected_failure", file=error_stream)
        return 3

    print(
        "DESKTOP_RETURNS_AUTH_CANARY_OK "
        f"status={result.status} credentialed=1 read_only=1 data_free=1 "
        "kind=desktop source=windows_dpapi",
        file=output_stream,
    )
    return 0
