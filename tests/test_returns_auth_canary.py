import io
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

from taksklad.desktop_auth import (
    install_scoped_backend_token_from_stdin,
    run_desktop_returns_auth_canary,
)
from taksklad import credential_lock
from taksklad.returns_auth_canary import (
    PRODUCTION_BACKEND_ORIGIN,
    ReturnsAuthCanaryError,
    run_returns_auth_canary,
)
from taksklad.secret_store import (
    BACKEND_AUTH_BUNDLE_SECRET,
    BACKEND_API_TOKEN_SECRET,
    BACKEND_PRINCIPAL_IDENTIFIER_SECRET,
    decode_backend_auth_bundle,
    encode_backend_auth_bundle,
)
from tools import credentialed_returns_canary


SCOPED_TOKEN = "tks." + "a" * 32 + "." + "b" * 43
OLD_SCOPED_TOKEN = "tks." + "c" * 32 + "." + "d" * 43
DESKTOP_IDENTIFIER = "desktop.alpha"
ACCEPTANCE_IDENTIFIER = "acceptance.release"


class FakeResponse:
    def __init__(self, status):
        self.status = status
        self.closed = False

    def getcode(self):
        return self.status

    def close(self):
        self.closed = True


class FakeDpapiStore:
    def __init__(self, token=None, identifier=DESKTOP_IDENTIFIER, *, legacy=False):
        self.values = {}
        self.set_calls = 0
        self.delete_calls = 0
        self.read_override = None
        self.fail_rollback = False
        self.fail_bundle_write_before = False
        self.fail_bundle_write_after = False
        self.fail_legacy_cleanup = False
        if token is not None:
            if legacy:
                self.values[BACKEND_API_TOKEN_SECRET] = token
                if identifier is not None:
                    self.values[BACKEND_PRINCIPAL_IDENTIFIER_SECRET] = identifier
            elif identifier is not None:
                self.values[BACKEND_AUTH_BUNDLE_SECRET] = encode_backend_auth_bundle(token, identifier)
            else:
                self.values[BACKEND_API_TOKEN_SECRET] = token

    def status(self):
        return {
            "provider": "windows_dpapi",
            "available": True,
            "persistent": True,
            "scope": "current_user",
            "state": "ok",
        }

    def get_text(self, name):
        if self.read_override is not None:
            return self.read_override
        return self.values.get(name)

    def set_text(self, name, value):
        self.set_calls += 1
        if self.fail_bundle_write_before and name == BACKEND_AUTH_BUNDLE_SECRET:
            self.fail_bundle_write_before = False
            raise RuntimeError("synthetic bundle write failure")
        if self.fail_rollback and name == BACKEND_AUTH_BUNDLE_SECRET and value and OLD_SCOPED_TOKEN in value:
            raise RuntimeError("synthetic rollback failure")
        self.values[name] = value
        if self.fail_bundle_write_after and name == BACKEND_AUTH_BUNDLE_SECRET:
            self.fail_bundle_write_after = False
            raise RuntimeError("synthetic crash after atomic replace")
        return True

    def delete(self, name):
        self.delete_calls += 1
        if self.fail_legacy_cleanup and name in {
            BACKEND_API_TOKEN_SECRET,
            BACKEND_PRINCIPAL_IDENTIFIER_SECRET,
        }:
            raise RuntimeError("synthetic legacy cleanup failure")
        if self.fail_rollback:
            raise RuntimeError("synthetic rollback failure")
        return self.values.pop(name, None) is not None


def stored_pair(store):
    value = store.get_text(BACKEND_AUTH_BUNDLE_SECRET)
    return decode_backend_auth_bundle(value) if value is not None else (None, None)


class ReturnsAuthCanaryTests(unittest.TestCase):
    def approved_opener(self, statuses, requests):
        remaining = list(statuses)

        def opener(request, timeout):
            requests.append((request, timeout))
            status = remaining.pop(0)
            return FakeResponse(status)

        return opener

    def test_credential_lock_is_stable_across_working_directories_and_recovers_stale_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            local_app_data = Path(temporary) / "LocalAppData"
            first_cwd = Path(temporary) / "first"
            second_cwd = Path(temporary) / "second"
            first_cwd.mkdir()
            second_cwd.mkdir()
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}, clear=False):
                old_cwd = Path.cwd()
                try:
                    os.chdir(first_cwd)
                    first = credential_lock.acquire_credential_mutation_lock()
                    self.assertTrue(first.acquired)
                    os.chdir(second_cwd)
                    blocked = credential_lock.acquire_credential_mutation_lock()
                    self.assertFalse(blocked.acquired)
                    self.assertEqual(Path(first.lock.path).parent, local_app_data / "TakSklad" / "secrets")
                    credential_lock.release_credential_mutation_lock(first.lock)
                    after_exit = credential_lock.acquire_credential_mutation_lock()
                    self.assertTrue(after_exit.acquired)
                    lock_path = Path(after_exit.lock.path)
                    credential_lock.release_credential_mutation_lock(after_exit.lock)
                    lock_path.write_text('{"pid": 999999999, "owner_id": "stale"}', encoding="utf-8")
                    recovered = credential_lock.acquire_credential_mutation_lock(
                        process_running_func=lambda _pid: False
                    )
                    self.assertTrue(recovered.acquired)
                    self.assertTrue(recovered.recovered)
                    credential_lock.release_credential_mutation_lock(recovered.lock)
                finally:
                    os.chdir(old_cwd)

    def test_non_windows_lock_is_available_only_for_explicit_non_frozen_dev_test_mode(self):
        with mock.patch.dict(os.environ, {"TAKSKLAD_SECRET_STORE_MODE": "test"}, clear=True):
            result = credential_lock.acquire_credential_mutation_lock()
            self.assertTrue(result.acquired)
            credential_lock.release_credential_mutation_lock(result.lock)
        for mode in ("", "production"):
            with mock.patch.dict(os.environ, {"TAKSKLAD_SECRET_STORE_MODE": mode}, clear=True):
                with self.assertRaises(Exception):
                    credential_lock.acquire_credential_mutation_lock()

    def test_canary_is_data_free_exact_204_and_never_reads_body(self):
        requests = []
        result = run_returns_auth_canary(
            PRODUCTION_BACKEND_ORIGIN,
            SCOPED_TOKEN,
            timeout=7,
            opener=self.approved_opener([204], requests),
            require_scoped=True,
            canary_kind="desktop",
            identifier=DESKTOP_IDENTIFIER,
        )
        self.assertEqual((result.status, result.canary_kind), (204, "desktop"))
        self.assertEqual(len(requests), 1)
        request, timeout = requests[0]
        self.assertEqual(timeout, 7)
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(request.headers["X-taksklad-canary-identifier"], DESKTOP_IDENTIFIER)
        self.assertEqual(
            request.full_url,
            PRODUCTION_BACKEND_ORIGIN + "/api/v1/returns/auth-canary/desktop",
        )

    def test_canary_rejects_every_status_except_204_and_redacts(self):
        for status in (200, 301, 302, 401, 403, 404, 500):
            with self.subTest(status=status):
                with self.assertRaises(ReturnsAuthCanaryError) as captured:
                    run_returns_auth_canary(
                        PRODUCTION_BACKEND_ORIGIN,
                        SCOPED_TOKEN,
                        opener=lambda request, timeout, status=status: FakeResponse(status),
                        require_scoped=True,
                        identifier=DESKTOP_IDENTIFIER,
                    )
                rendered = str(captured.exception)
                self.assertIn(str(status), rendered)
                self.assertNotIn(SCOPED_TOKEN, rendered)
                self.assertNotIn("body", rendered)

    def test_missing_endpoint_bootstrap_allows_only_404(self):
        with self.assertRaises(ReturnsAuthCanaryError):
            run_returns_auth_canary(
                PRODUCTION_BACKEND_ORIGIN,
                SCOPED_TOKEN,
                opener=lambda request, timeout: FakeResponse(404),
                require_scoped=True,
                identifier=ACCEPTANCE_IDENTIFIER,
            )
        result = run_returns_auth_canary(
            PRODUCTION_BACKEND_ORIGIN,
            SCOPED_TOKEN,
            opener=lambda request, timeout: FakeResponse(404),
            require_scoped=True,
            canary_kind="acceptance",
            identifier=ACCEPTANCE_IDENTIFIER,
            allow_missing_endpoint=True,
        )
        self.assertEqual(result.status, 404)
        for status in (200, 301, 302, 401, 403, 500):
            with self.subTest(status=status), self.assertRaises(ReturnsAuthCanaryError):
                run_returns_auth_canary(
                    PRODUCTION_BACKEND_ORIGIN,
                    SCOPED_TOKEN,
                    opener=lambda request, timeout, status=status: FakeResponse(status),
                    require_scoped=True,
                    canary_kind="acceptance",
                    identifier=ACCEPTANCE_IDENTIFIER,
                    allow_missing_endpoint=True,
                )

    def test_origin_is_pinned_before_network_and_localhost_is_test_only(self):
        calls = []
        blocked = (
            "https://evil.example",
            "https://api.taksklad.uz.evil.example",
            "https://api-taksklad.uz",
            "https://user@api.taksklad.uz",
            "http://api.taksklad.uz",
            "http://127.0.0.1:8000",
        )
        for value in blocked:
            with self.subTest(value=value), self.assertRaises(ReturnsAuthCanaryError):
                run_returns_auth_canary(
                    value,
                    SCOPED_TOKEN,
                    opener=lambda request, timeout: calls.append(request),
                    require_scoped=True,
                    identifier=DESKTOP_IDENTIFIER,
                )
        self.assertEqual(calls, [])
        result = run_returns_auth_canary(
            "http://127.0.0.1:8000",
            SCOPED_TOKEN,
            opener=lambda request, timeout: FakeResponse(204),
            require_scoped=True,
            identifier=DESKTOP_IDENTIFIER,
            allow_test_localhost=True,
        )
        self.assertEqual(result.status, 204)

    def test_desktop_packaged_canary_blocks_legacy_before_network(self):
        calls = []
        legacy_store = FakeDpapiStore("legacy-synthetic-token")
        status = run_desktop_returns_auth_canary(
            store=legacy_store,
            output_stream=io.StringIO(),
            error_stream=io.StringIO(),
            opener=lambda request, timeout: calls.append(request),
        )
        self.assertEqual(status, 3)
        self.assertEqual(calls, [])

        scoped_calls = []
        status = run_desktop_returns_auth_canary(
            store=FakeDpapiStore(SCOPED_TOKEN),
            output_stream=io.StringIO(),
            error_stream=io.StringIO(),
            opener=self.approved_opener([204], scoped_calls),
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(scoped_calls), 1)

    def test_desktop_packaged_canary_requires_separately_stored_identifier_before_network(self):
        calls = []
        store = FakeDpapiStore(SCOPED_TOKEN, identifier=None)
        status = run_desktop_returns_auth_canary(
            store=store,
            output_stream=io.StringIO(),
            error_stream=io.StringIO(),
            opener=lambda request, timeout: calls.append(request),
        )
        self.assertEqual(status, 3)
        self.assertEqual(calls, [])
        self.assertNotIn(BACKEND_AUTH_BUNDLE_SECRET, store.values)

    def test_complete_legacy_pair_migrates_to_one_bundle_before_canary(self):
        store = FakeDpapiStore(SCOPED_TOKEN, legacy=True)
        requests = []
        status = run_desktop_returns_auth_canary(
            store=store,
            output_stream=io.StringIO(),
            error_stream=io.StringIO(),
            opener=self.approved_opener([204], requests),
        )
        self.assertEqual(status, 0)
        self.assertEqual(stored_pair(store), (SCOPED_TOKEN, DESKTOP_IDENTIFIER))
        self.assertNotIn(BACKEND_API_TOKEN_SECRET, store.values)
        self.assertNotIn(BACKEND_PRINCIPAL_IDENTIFIER_SECRET, store.values)
        self.assertEqual(len(requests), 1)

    def test_partial_or_corrupt_legacy_bundle_blocks_before_network(self):
        stores = [
            FakeDpapiStore(SCOPED_TOKEN, identifier=None),
            FakeDpapiStore(),
        ]
        stores[1].values[BACKEND_AUTH_BUNDLE_SECRET] = "{malformed"
        for store in stores:
            with self.subTest(values=sorted(store.values)):
                requests = []
                output = io.StringIO()
                status = run_desktop_returns_auth_canary(
                    store=store,
                    output_stream=output,
                    error_stream=output,
                    opener=lambda request, timeout: requests.append(request),
                )
                self.assertEqual(status, 3)
                self.assertEqual(requests, [])
                self.assertNotIn(SCOPED_TOKEN, output.getvalue())

    def test_verified_bundle_is_authoritative_over_stale_legacy_pair(self):
        store = FakeDpapiStore(SCOPED_TOKEN)
        store.values[BACKEND_API_TOKEN_SECRET] = OLD_SCOPED_TOKEN
        store.values[BACKEND_PRINCIPAL_IDENTIFIER_SECRET] = "desktop.stale"
        requests = []
        status = run_desktop_returns_auth_canary(
            store=store,
            output_stream=io.StringIO(),
            error_stream=io.StringIO(),
            opener=self.approved_opener([204], requests),
        )
        self.assertEqual(status, 0)
        self.assertEqual(stored_pair(store), (SCOPED_TOKEN, DESKTOP_IDENTIFIER))
        self.assertNotIn(BACKEND_API_TOKEN_SECRET, store.values)
        self.assertNotIn(BACKEND_PRINCIPAL_IDENTIFIER_SECRET, store.values)

    def install(self, store, statuses, token=SCOPED_TOKEN):
        stdout = io.StringIO()
        stderr = io.StringIO()
        requests = []
        status = install_scoped_backend_token_from_stdin(
            expected_identifier=DESKTOP_IDENTIFIER,
            input_stream=io.StringIO(token + "\n"),
            output_stream=stdout,
            error_stream=stderr,
            store=store,
            opener=self.approved_opener(statuses, requests),
            lock_acquirer=lambda: SimpleNamespace(acquired=True, lock=object()),
            lock_releaser=lambda lock: True,
        )
        return status, requests, stdout.getvalue() + stderr.getvalue()

    def test_installer_invalid_format_has_no_network_or_write(self):
        store = FakeDpapiStore(OLD_SCOPED_TOKEN)
        status, requests, output = self.install(store, [], token="legacy-token")
        self.assertEqual(status, 2)
        self.assertEqual(requests, [])
        self.assertEqual(store.set_calls, 0)
        self.assertEqual(stored_pair(store), (OLD_SCOPED_TOKEN, DESKTOP_IDENTIFIER))
        self.assertNotIn("legacy-token", output)

    def test_installer_preflight_failure_preserves_previous_without_write(self):
        for backend_status in (401, 403):
            with self.subTest(status=backend_status):
                store = FakeDpapiStore(OLD_SCOPED_TOKEN)
                status, requests, output = self.install(store, [backend_status])
                self.assertEqual(status, 2)
                self.assertEqual(len(requests), 1)
                self.assertEqual(store.set_calls, 0)
                self.assertEqual(stored_pair(store), (OLD_SCOPED_TOKEN, DESKTOP_IDENTIFIER))
                self.assertNotIn(SCOPED_TOKEN, output)

    def test_installer_exact_identifier_mismatch_never_writes(self):
        store = FakeDpapiStore(OLD_SCOPED_TOKEN, identifier="desktop.previous")
        status, requests, output = self.install(store, [403])
        self.assertEqual(status, 2)
        self.assertEqual(len(requests), 1)
        request, _timeout = requests[0]
        self.assertEqual(request.headers["X-taksklad-canary-identifier"], DESKTOP_IDENTIFIER)
        self.assertEqual(stored_pair(store), (OLD_SCOPED_TOKEN, "desktop.previous"))
        self.assertEqual(store.set_calls, 0)
        self.assertNotIn(SCOPED_TOKEN, output)

    def test_bundle_write_failure_preserves_exact_previous_bundle(self):
        store = FakeDpapiStore(OLD_SCOPED_TOKEN, identifier="desktop.previous")
        store.fail_bundle_write_before = True
        status, requests, output = self.install(store, [204])
        self.assertEqual(status, 2)
        self.assertEqual(len(requests), 1)
        self.assertEqual(stored_pair(store), (OLD_SCOPED_TOKEN, "desktop.previous"))
        self.assertNotIn("_OK", output)
        self.assertNotIn(SCOPED_TOKEN, output)

    def test_crash_after_atomic_bundle_replace_rolls_back_one_exact_record(self):
        store = FakeDpapiStore(OLD_SCOPED_TOKEN, identifier="desktop.previous")
        previous = store.values[BACKEND_AUTH_BUNDLE_SECRET]
        store.fail_bundle_write_after = True
        status, requests, output = self.install(store, [204])
        self.assertEqual(status, 2)
        self.assertEqual(len(requests), 1)
        self.assertEqual(store.values[BACKEND_AUTH_BUNDLE_SECRET], previous)
        self.assertEqual(stored_pair(store), (OLD_SCOPED_TOKEN, "desktop.previous"))
        self.assertNotIn(SCOPED_TOKEN, output)

    def test_legacy_cleanup_failure_keeps_verified_bundle_authoritative(self):
        store = FakeDpapiStore(OLD_SCOPED_TOKEN, legacy=True)
        store.fail_legacy_cleanup = True
        status, requests, output = self.install(store, [204, 204])
        self.assertEqual(status, 4)
        self.assertEqual(len(requests), 2)
        self.assertEqual(stored_pair(store), (SCOPED_TOKEN, DESKTOP_IDENTIFIER))
        self.assertIn("legacy_cleanup_manual_recovery_required", output)
        self.assertNotIn(SCOPED_TOKEN, output)

        absent = FakeDpapiStore()
        absent.fail_bundle_write_before = True
        status, requests, output = self.install(absent, [204])
        self.assertEqual(status, 2)
        self.assertEqual(len(requests), 1)
        self.assertIsNone(absent.get_text(BACKEND_AUTH_BUNDLE_SECRET))
        self.assertEqual(absent.delete_calls, 0)
        self.assertNotIn("_OK", output)
        self.assertNotIn(SCOPED_TOKEN, output)

    def test_invalid_expected_identifier_blocks_before_stdin_network_or_store(self):
        store = FakeDpapiStore(OLD_SCOPED_TOKEN)
        input_stream = mock.Mock()
        calls = []
        output = io.StringIO()
        status = install_scoped_backend_token_from_stdin(
            expected_identifier="invalid identifier",
            input_stream=input_stream,
            output_stream=output,
            error_stream=output,
            store=store,
            opener=lambda request, timeout: calls.append(request),
            lock_acquirer=lambda: SimpleNamespace(acquired=True, lock=object()),
            lock_releaser=lambda lock: True,
        )
        self.assertEqual(status, 2)
        input_stream.read.assert_not_called()
        input_stream.readline.assert_not_called()
        self.assertEqual(calls, [])
        self.assertEqual(store.set_calls, 0)

    def test_fresh_install_uses_pinned_origin_and_verifies_stored_token(self):
        store = FakeDpapiStore()
        store.values["telegram_bot_token"] = "synthetic-unrelated-value"
        status, requests, output = self.install(store, [204, 204])
        self.assertEqual(status, 0)
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(r.full_url.startswith(PRODUCTION_BACKEND_ORIGIN) for r, _ in requests))
        self.assertEqual(stored_pair(store), (SCOPED_TOKEN, DESKTOP_IDENTIFIER))
        self.assertNotIn(BACKEND_API_TOKEN_SECRET, store.values)
        self.assertNotIn(BACKEND_PRINCIPAL_IDENTIFIER_SECRET, store.values)
        self.assertEqual(store.values["telegram_bot_token"], "synthetic-unrelated-value")
        self.assertIn("DESKTOP_AUTH_INSTALL_OK", output)
        self.assertNotIn(SCOPED_TOKEN, output)

    def test_roundtrip_mismatch_is_fatal_and_does_not_overwrite_external_change(self):
        store = FakeDpapiStore(OLD_SCOPED_TOKEN)
        original_get = store.get_text
        reads = 0

        def mismatching_read(name):
            nonlocal reads
            reads += 1
            if reads == 2 and name == BACKEND_AUTH_BUNDLE_SECRET:
                external = encode_backend_auth_bundle(
                    "tks." + "e" * 32 + "." + "f" * 43,
                    "desktop.external",
                )
                store.values[name] = external
                return external
            return original_get(name)

        store.get_text = mismatching_read
        status, _requests, output = self.install(store, [204])
        self.assertEqual(status, 4)
        self.assertEqual(
            decode_backend_auth_bundle(store.values[BACKEND_AUTH_BUNDLE_SECRET]),
            ("tks." + "e" * 32 + "." + "f" * 43, "desktop.external"),
        )
        self.assertIn("manual_recovery_required", output)
        self.assertNotIn(SCOPED_TOKEN, output)

    def test_post_write_canary_failure_rolls_back_previous_or_deletes_new(self):
        for previous in (OLD_SCOPED_TOKEN, None):
            with self.subTest(previous=previous is not None):
                previous_identifier = "desktop.previous" if previous is not None else None
                store = FakeDpapiStore(previous, identifier=previous_identifier)
                status, requests, output = self.install(store, [204, 403])
                self.assertEqual(status, 2)
                self.assertEqual(len(requests), 2)
                self.assertEqual(stored_pair(store), (previous, previous_identifier))
                if previous is None:
                    self.assertGreaterEqual(store.delete_calls, 1)
                self.assertNotIn(SCOPED_TOKEN, output)

    def test_rollback_failure_is_fatal_and_never_success(self):
        store = FakeDpapiStore(OLD_SCOPED_TOKEN)
        store.fail_rollback = True
        status, _requests, output = self.install(store, [204, 403])
        self.assertEqual(status, 4)
        self.assertIn("DESKTOP_AUTH_INSTALL_FATAL reason=rollback_failed", output)
        self.assertNotIn("_OK", output)
        self.assertNotIn(SCOPED_TOKEN, output)
        self.assertNotIn(OLD_SCOPED_TOKEN, output)

    def test_concurrent_installer_is_blocked_before_network_or_write(self):
        store = FakeDpapiStore(OLD_SCOPED_TOKEN)
        calls = []
        output = io.StringIO()
        status = install_scoped_backend_token_from_stdin(
            expected_identifier=DESKTOP_IDENTIFIER,
            input_stream=io.StringIO(SCOPED_TOKEN + "\n"),
            output_stream=output,
            error_stream=output,
            store=store,
            opener=lambda request, timeout: calls.append(request),
            lock_acquirer=lambda: SimpleNamespace(acquired=False, lock=None),
        )
        self.assertEqual(status, 2)
        self.assertEqual(calls, [])
        self.assertEqual(store.set_calls, 0)
        self.assertEqual(stored_pair(store), (OLD_SCOPED_TOKEN, DESKTOP_IDENTIFIER))
        self.assertIn("workstation_in_use", output.getvalue())

    def test_server_cli_is_stdin_only_acceptance_and_redacted(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        requests = []
        status = credentialed_returns_canary.main(
            ["--acceptance-token-stdin", "--identifier", ACCEPTANCE_IDENTIFIER],
            input_stream=io.StringIO(SCOPED_TOKEN + "\n"),
            output_stream=stdout,
            error_stream=stderr,
            opener=self.approved_opener([204], requests),
        )
        combined = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(status, 0)
        self.assertEqual(len(requests), 1)
        self.assertIn("kind=acceptance", combined)
        self.assertNotIn(SCOPED_TOKEN, combined)

        with mock.patch.dict("os.environ", {"TAKSKLAD_AUTH_CANARY_TOKEN": SCOPED_TOKEN}):
            blocked = credentialed_returns_canary.main(
                ["--acceptance-token-stdin", "--identifier", ACCEPTANCE_IDENTIFIER],
                input_stream=io.StringIO(""),
                output_stream=stdout,
                error_stream=stderr,
                opener=lambda request, timeout: self.fail("network must not run"),
            )
        self.assertEqual(blocked, 1)
        self.assertNotIn(SCOPED_TOKEN, stdout.getvalue() + stderr.getvalue())

    def test_server_cli_rejects_inconsistent_missing_endpoint_flags_before_stdin_or_network(self):
        output = io.StringIO()
        calls = []
        status = credentialed_returns_canary.main(
            [
                "--acceptance-token-stdin",
                "--identifier",
                ACCEPTANCE_IDENTIFIER,
                "--require-missing-endpoint",
            ],
            input_stream=io.StringIO(SCOPED_TOKEN + "\n"),
            output_stream=output,
            error_stream=output,
            opener=lambda request, timeout: calls.append(request),
        )
        self.assertEqual(status, 2)
        self.assertEqual(calls, [])
        self.assertIn("invalid_bootstrap_mode", output.getvalue())
        self.assertNotIn(SCOPED_TOKEN, output.getvalue())


if __name__ == "__main__":
    unittest.main()
