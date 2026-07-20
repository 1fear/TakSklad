import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from taksklad import backend_client, desktop_pairing, main
from taksklad.secret_store import (
    BACKEND_AUTH_BUNDLE_SECRET,
    SecretStoreError,
    decode_backend_auth_bundle,
    encode_backend_auth_bundle,
)


SETUP_CODE = "S" * 43
TOKEN = "tks." + "a" * 32 + "." + "b" * 43
ROTATED_TOKEN = "tks." + "c" * 32 + "." + "d" * 43
IDENTIFIER = "desktop.paired"
PAIRING_ID = "123e4567-e89b-12d3-a456-426614174000"


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.closed = False

    def getcode(self):
        return self.status

    def read(self, _limit=-1):
        return self._payload

    def close(self):
        self.closed = True


class FakeDpapiStore:
    def __init__(self, bundle=None):
        self.values = {}
        if bundle is not None:
            self.values[BACKEND_AUTH_BUNDLE_SECRET] = bundle
        self.fail_write = False

    def status(self):
        return {
            "provider": "windows_dpapi",
            "available": True,
            "persistent": True,
            "scope": "current_user",
            "state": "ok",
        }

    def get_text(self, name):
        return self.values.get(name)

    def set_text(self, name, value):
        if self.fail_write and name == BACKEND_AUTH_BUNDLE_SECRET:
            self.fail_write = False
            raise RuntimeError("synthetic write failure")
        self.values[name] = value
        return True

    def delete(self, name):
        return self.values.pop(name, None) is not None


def redeem_payload():
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    return {
        "pairing_id": PAIRING_ID,
        "credential": TOKEN,
        "principal_identifier": IDENTIFIER,
        "ack_deadline": deadline.isoformat(),
    }


def ack_payload():
    expiry = datetime.now(timezone.utc) + timedelta(days=365)
    return {
        "pairing_id": PAIRING_ID,
        "status": "acked",
        "credential_expires_at": expiry.isoformat(),
    }


def bootstrap_payload(credential=TOKEN):
    payload = redeem_payload()
    payload["credential"] = credential
    return payload


class DesktopPairingTests(unittest.TestCase):
    def test_valid_bundle_skips_modal_construction(self):
        store = FakeDpapiStore(encode_backend_auth_bundle(TOKEN, IDENTIFIER))
        with mock.patch.object(desktop_pairing.tk, "Tk", side_effect=AssertionError("UI must stay closed")):
            self.assertTrue(desktop_pairing.run_desktop_pairing_dialog(store=store))

    def test_invalid_setup_code_is_rejected_before_lock_store_and_network(self):
        acquire = mock.Mock()
        opener = mock.Mock()
        with self.assertRaises(desktop_pairing.DesktopPairingError) as captured:
            desktop_pairing.pair_desktop_from_setup_code(
                "short",
                store=FakeDpapiStore(),
                opener=opener,
                lock_acquirer=acquire,
            )
        self.assertEqual(captured.exception.reason, "setup_code_invalid")
        acquire.assert_not_called()
        opener.assert_not_called()

    def test_existing_valid_credential_is_unchanged_without_pairing_network(self):
        original = encode_backend_auth_bundle(TOKEN, IDENTIFIER)
        store = FakeDpapiStore(original)
        calls = []

        result = desktop_pairing.pair_desktop_from_setup_code(
            SETUP_CODE,
            store=store,
            opener=lambda request, timeout: calls.append(request),
            credential_lock_held=True,
        )

        self.assertTrue(result)
        self.assertEqual(calls, [])
        self.assertEqual(store.values[BACKEND_AUTH_BUNDLE_SECRET], original)

    def test_silent_bootstrap_keeps_server_verified_bundle_without_bootstrap(self):
        original = encode_backend_auth_bundle(TOKEN, IDENTIFIER)
        store = FakeDpapiStore(original)
        bootstrap_opener = mock.Mock(side_effect=AssertionError("bootstrap must stay closed"))
        canary_requests = []

        self.assertTrue(desktop_pairing.ensure_public_desktop_identity(
            store=store,
            opener=bootstrap_opener,
            canary_opener=lambda request, timeout: (
                canary_requests.append(request) or FakeResponse(204)
            ),
            credential_lock_held=True,
        ))

        bootstrap_opener.assert_not_called()
        self.assertEqual(len(canary_requests), 1)
        self.assertEqual(canary_requests[0].headers["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(store.values[BACKEND_AUTH_BUNDLE_SECRET], original)

    def test_silent_bootstrap_rotates_only_server_rejected_bundle(self):
        original = encode_backend_auth_bundle(TOKEN, IDENTIFIER)
        store = FakeDpapiStore(original)
        bootstrap_requests = []
        canary_tokens = []

        def opener(request, timeout):
            bootstrap_requests.append(request)
            if request.full_url.endswith(desktop_pairing.BOOTSTRAP_PATH):
                return FakeResponse(200, bootstrap_payload(ROTATED_TOKEN))
            return FakeResponse(200, ack_payload())

        def canary_opener(request, timeout):
            token = request.headers["Authorization"]
            canary_tokens.append(token)
            if token == f"Bearer {TOKEN}":
                return FakeResponse(401)
            return FakeResponse(204)

        self.assertTrue(desktop_pairing.ensure_public_desktop_identity(
            store=store,
            opener=opener,
            canary_opener=canary_opener,
            credential_lock_held=True,
        ))

        self.assertEqual(
            decode_backend_auth_bundle(store.values[BACKEND_AUTH_BUNDLE_SECRET]),
            (ROTATED_TOKEN, IDENTIFIER),
        )
        self.assertEqual(len(bootstrap_requests), 2)
        self.assertEqual(canary_tokens.count(f"Bearer {TOKEN}"), 1)
        self.assertEqual(canary_tokens.count(f"Bearer {ROTATED_TOKEN}"), 2)

    def test_silent_bootstrap_preserves_bundle_when_canary_transport_is_unavailable(self):
        original = encode_backend_auth_bundle(TOKEN, IDENTIFIER)
        store = FakeDpapiStore(original)
        bootstrap_opener = mock.Mock(side_effect=AssertionError("bootstrap must stay closed"))

        self.assertTrue(desktop_pairing.ensure_public_desktop_identity(
            store=store,
            opener=bootstrap_opener,
            canary_opener=mock.Mock(side_effect=OSError("offline")),
            credential_lock_held=True,
        ))

        bootstrap_opener.assert_not_called()
        self.assertEqual(store.values[BACKEND_AUTH_BUNDLE_SECRET], original)

    def test_silent_bootstrap_restores_rejected_bundle_when_rotation_fails(self):
        original = encode_backend_auth_bundle(TOKEN, IDENTIFIER)
        store = FakeDpapiStore(original)

        def opener(request, timeout):
            if request.full_url.endswith(desktop_pairing.BOOTSTRAP_PATH):
                return FakeResponse(200, bootstrap_payload(ROTATED_TOKEN))
            return FakeResponse(503, {})

        def canary_opener(request, timeout):
            if request.headers["Authorization"] == f"Bearer {TOKEN}":
                return FakeResponse(401)
            return FakeResponse(204)

        with self.assertRaises(desktop_pairing.DesktopPairingError):
            desktop_pairing.ensure_public_desktop_identity(
                store=store,
                opener=opener,
                canary_opener=canary_opener,
                sleep_func=lambda _seconds: None,
                credential_lock_held=True,
            )

        self.assertEqual(store.values[BACKEND_AUTH_BUNDLE_SECRET], original)

    def test_silent_bootstrap_persists_only_verified_public_identity(self):
        store = FakeDpapiStore()
        bootstrap_requests = []
        canary_requests = []

        def opener(request, timeout):
            bootstrap_requests.append(request)
            if request.full_url.endswith(desktop_pairing.BOOTSTRAP_PATH):
                return FakeResponse(200, bootstrap_payload())
            return FakeResponse(200, ack_payload())

        self.assertTrue(desktop_pairing.ensure_public_desktop_identity(
            store=store,
            opener=opener,
            canary_opener=lambda request, timeout: (
                canary_requests.append(request) or FakeResponse(204)
            ),
            credential_lock_held=True,
        ))

        self.assertEqual(
            decode_backend_auth_bundle(store.values[BACKEND_AUTH_BUNDLE_SECRET]),
            (TOKEN, IDENTIFIER),
        )
        self.assertEqual(len(bootstrap_requests), 2)
        request, ack_request = bootstrap_requests
        self.assertEqual(
            request.full_url,
            desktop_pairing.PRODUCTION_BACKEND_ORIGIN + desktop_pairing.BOOTSTRAP_PATH,
        )
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(json.loads(request.data), {"desktop_version": desktop_pairing.APP_VERSION})
        self.assertEqual(ack_request.headers["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(len(canary_requests), 2)

    def test_silent_bootstrap_failures_are_sanitized_and_fail_closed(self):
        leaked_credential = "tks." + "c" * 32 + "." + "d" * 43
        transport = mock.Mock(side_effect=OSError(leaked_credential))
        cases = {
            "http": lambda request, timeout: FakeResponse(
                400,
                {"detail": leaked_credential},
            ),
            "invalid": lambda request, timeout: FakeResponse(
                200,
                {"credential": leaked_credential},
            ),
            "oversize": lambda request, timeout: SimpleNamespace(
                status=200,
                getcode=lambda: 200,
                read=lambda _limit=-1: b"x" * (desktop_pairing.MAX_RESPONSE_BYTES + 1),
                close=lambda: None,
            ),
            "redirect": lambda request, timeout: FakeResponse(
                302,
                {"location": f"https://example.test/{leaked_credential}"},
            ),
            "transport": SimpleNamespace(open=transport),
        }

        for name, opener in cases.items():
            with self.subTest(name=name):
                store = FakeDpapiStore()
                with self.assertRaises(desktop_pairing.DesktopPairingError) as captured:
                    desktop_pairing.ensure_public_desktop_identity(
                        store=store,
                        opener=opener,
                        canary_opener=mock.Mock(),
                        sleep_func=lambda _seconds: None,
                        credential_lock_held=True,
                    )
                self.assertNotIn(leaked_credential, str(captured.exception))
                self.assertNotIn(BACKEND_AUTH_BUNDLE_SECRET, store.values)

        self.assertEqual(transport.call_count, desktop_pairing.BOOTSTRAP_MAX_ATTEMPTS)

    def test_silent_bootstrap_ack_failure_restores_exact_previous_bundle(self):
        previous = "{synthetic-corrupt-bundle"
        store = FakeDpapiStore(previous)

        def opener(request, timeout):
            if request.full_url.endswith(desktop_pairing.BOOTSTRAP_PATH):
                return FakeResponse(200, bootstrap_payload())
            return FakeResponse(503, {})

        with self.assertRaises(desktop_pairing.DesktopPairingError):
            desktop_pairing.ensure_public_desktop_identity(
                store=store,
                opener=opener,
                canary_opener=lambda request, timeout: FakeResponse(204),
                sleep_func=lambda _seconds: None,
                credential_lock_held=True,
            )

        self.assertEqual(store.values[BACKEND_AUTH_BUNDLE_SECRET], previous)

    def test_silent_bootstrap_retries_only_transient_http_failures(self):
        store = FakeDpapiStore()
        responses = [
            FakeResponse(503, {}),
            FakeResponse(429, {}),
            FakeResponse(200, bootstrap_payload()),
            FakeResponse(200, ack_payload()),
        ]
        sleeps = []

        self.assertTrue(desktop_pairing.ensure_public_desktop_identity(
            store=store,
            opener=lambda request, timeout: responses.pop(0),
            canary_opener=lambda request, timeout: FakeResponse(204),
            sleep_func=sleeps.append,
            credential_lock_held=True,
        ))

        self.assertEqual(sleeps, [0.25, 0.5])

    def test_redeem_failure_never_writes_a_bundle(self):
        store = FakeDpapiStore()
        calls = []

        def opener(request, timeout):
            calls.append(request)
            return FakeResponse(403, {"detail": "synthetic secret-bearing detail"})

        with self.assertRaises(desktop_pairing.DesktopPairingError) as captured:
            desktop_pairing.pair_desktop_from_setup_code(
                SETUP_CODE,
                store=store,
                opener=opener,
                credential_lock_held=True,
            )

        self.assertEqual(captured.exception.reason, "pairing_http_403")
        self.assertNotIn(BACKEND_AUTH_BUNDLE_SECRET, store.values)
        self.assertEqual(len(calls), 1)
        self.assertNotIn(SETUP_CODE, str(captured.exception))

    def test_preflight_canary_failure_preserves_invalid_previous_bundle_exactly(self):
        previous = "{synthetic-corrupt-bundle"
        store = FakeDpapiStore(previous)
        pairing_calls = []

        def pairing_opener(request, timeout):
            pairing_calls.append(request)
            return FakeResponse(200, redeem_payload())

        with self.assertRaises(desktop_pairing.DesktopPairingError):
            desktop_pairing.pair_desktop_from_setup_code(
                SETUP_CODE,
                store=store,
                opener=pairing_opener,
                canary_opener=lambda request, timeout: FakeResponse(403),
                credential_lock_held=True,
            )

        self.assertEqual(store.values[BACKEND_AUTH_BUNDLE_SECRET], previous)
        self.assertEqual(len(pairing_calls), 1)

    def test_dpapi_write_failure_preserves_previous_bundle(self):
        previous = "{synthetic-corrupt-bundle"
        store = FakeDpapiStore(previous)
        store.fail_write = True

        with self.assertRaises(desktop_pairing.DesktopPairingError):
            desktop_pairing.pair_desktop_from_setup_code(
                SETUP_CODE,
                store=store,
                opener=lambda request, timeout: FakeResponse(200, redeem_payload()),
                canary_opener=lambda request, timeout: FakeResponse(204),
                credential_lock_held=True,
            )

        self.assertEqual(store.values[BACKEND_AUTH_BUNDLE_SECRET], previous)

    def test_ack_failure_rolls_back_exact_previous_bundle(self):
        previous = "{synthetic-corrupt-bundle"
        store = FakeDpapiStore(previous)
        pairing_paths = []

        def pairing_opener(request, timeout):
            pairing_paths.append(request.full_url)
            if request.full_url.endswith(desktop_pairing.REDEEM_PATH):
                return FakeResponse(200, redeem_payload())
            return FakeResponse(503, {})

        with self.assertRaises(desktop_pairing.DesktopPairingError):
            desktop_pairing.pair_desktop_from_setup_code(
                SETUP_CODE,
                store=store,
                opener=pairing_opener,
                canary_opener=lambda request, timeout: FakeResponse(204),
                sleep_func=lambda _seconds: None,
                credential_lock_held=True,
            )

        self.assertEqual(store.values[BACKEND_AUTH_BUNDLE_SECRET], previous)
        self.assertEqual(len(pairing_paths), 4)

    def test_success_persists_verified_bundle_and_acknowledges_once(self):
        store = FakeDpapiStore()
        pairing_requests = []
        canary_requests = []

        def pairing_opener(request, timeout):
            pairing_requests.append(request)
            if request.full_url.endswith(desktop_pairing.REDEEM_PATH):
                return FakeResponse(200, redeem_payload())
            return FakeResponse(200, ack_payload())

        result = desktop_pairing.pair_desktop_from_setup_code(
            SETUP_CODE,
            store=store,
            opener=pairing_opener,
            canary_opener=lambda request, timeout: (
                canary_requests.append(request) or FakeResponse(204)
            ),
            credential_lock_held=True,
        )

        self.assertTrue(result)
        self.assertEqual(decode_backend_auth_bundle(store.values[BACKEND_AUTH_BUNDLE_SECRET]), (TOKEN, IDENTIFIER))
        self.assertEqual(len(canary_requests), 2)
        self.assertEqual(len(pairing_requests), 2)
        redeem_request, ack_request = pairing_requests
        self.assertEqual(redeem_request.full_url, desktop_pairing.PRODUCTION_BACKEND_ORIGIN + desktop_pairing.REDEEM_PATH)
        self.assertNotIn("Authorization", redeem_request.headers)
        self.assertEqual(ack_request.headers["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(json.loads(redeem_request.data), {
            "setup_code": SETUP_CODE,
            "desktop_version": desktop_pairing.APP_VERSION,
        })

    def test_pairing_lock_blocks_before_redeem_and_releases_after_success(self):
        blocked_calls = []
        with self.assertRaises(desktop_pairing.DesktopPairingError) as captured:
            desktop_pairing.pair_desktop_from_setup_code(
                SETUP_CODE,
                store=FakeDpapiStore(),
                opener=lambda request, timeout: blocked_calls.append(request),
                lock_acquirer=lambda: SimpleNamespace(acquired=False, lock=None),
            )
        self.assertEqual(captured.exception.reason, "workstation_in_use")
        self.assertEqual(blocked_calls, [])

        held = {"value": False}
        released = []
        store = FakeDpapiStore()

        def acquire():
            held["value"] = True
            return SimpleNamespace(acquired=True, lock="pairing-lock")

        def opener(request, timeout):
            self.assertTrue(held["value"])
            if request.full_url.endswith(desktop_pairing.REDEEM_PATH):
                return FakeResponse(200, redeem_payload())
            return FakeResponse(200, ack_payload())

        def canary_opener(request, timeout):
            self.assertTrue(held["value"])
            return FakeResponse(204)

        def release(lock):
            self.assertEqual(lock, "pairing-lock")
            held["value"] = False
            released.append(lock)

        self.assertTrue(desktop_pairing.pair_desktop_from_setup_code(
            SETUP_CODE,
            store=store,
            opener=opener,
            canary_opener=canary_opener,
            lock_acquirer=acquire,
            lock_releaser=release,
        ))
        self.assertFalse(held["value"])
        self.assertEqual(released, ["pairing-lock"])

    def test_backend_configured_requires_complete_locally_valid_bundle(self):
        with mock.patch.object(backend_client, "TAKSKLAD_BACKEND_BASE_URL", desktop_pairing.PRODUCTION_BACKEND_ORIGIN):
            with mock.patch.object(backend_client, "load_backend_auth_bundle", return_value=(TOKEN, IDENTIFIER)):
                self.assertTrue(backend_client.backend_configured())
            with mock.patch.object(
                backend_client,
                "load_backend_auth_bundle",
                side_effect=SecretStoreError("missing"),
            ):
                self.assertFalse(backend_client.backend_configured())

    def test_startup_silently_bootstraps_before_constructing_scanning_app(self):
        instance_lock = object()
        credential_lock = object()
        app = SimpleNamespace(single_instance_lock=None, mainloop=mock.Mock())
        with (
            mock.patch.object(main, "acquire_single_instance_lock", return_value=SimpleNamespace(acquired=True, lock=instance_lock)),
            mock.patch.object(main, "release_single_instance_lock"),
            mock.patch.object(main, "acquire_credential_mutation_lock", return_value=SimpleNamespace(acquired=True, lock=credential_lock)),
            mock.patch.object(main, "release_credential_mutation_lock"),
            mock.patch.object(main, "maybe_rename_windows_executable", return_value=False),
            mock.patch.object(main, "ensure_windows_desktop_shortcut"),
            mock.patch.object(main, "migrate_desktop_secrets", return_value={"restart_required": False}),
            mock.patch.object(main, "migrate_legacy_json_files_to_app_data"),
            mock.patch.object(main, "migrate_legacy_pending_saves_to_backend_events", return_value={"remaining": 0}),
            mock.patch.object(main, "log_startup_self_check"),
            mock.patch.object(main, "backend_configured", return_value=True),
            mock.patch.object(main, "ensure_public_desktop_identity", return_value=True) as bootstrap,
            mock.patch.object(desktop_pairing, "run_desktop_pairing_dialog") as pairing_dialog,
            mock.patch.object(main, "ScanningApp", return_value=app),
        ):
            self.assertEqual(main.run_app(), 0)

        bootstrap.assert_called_once_with(credential_lock_held=True)
        pairing_dialog.assert_not_called()
        app.mainloop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
