import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import main as backend_main
from backend.app.login_limiter import (
    BoundedTTLLoginLimiter,
    LoginLimiterCapacityExceeded,
    LoginRateLimited,
)
from backend.app.main import (
    client_identity,
    configure_cors,
    ensure_login_not_locked,
    login_attempt_key,
    register_login_failure,
    require_browser_request_security,
)
from backend.app.csrf import csrf_token_for_session
from backend.app.web_auth import SESSION_COOKIE_NAME
from backend.app.settings import load_settings


class BackendCorsTests(unittest.TestCase):
    def test_configured_frontend_origin_can_call_api_with_bearer_header(self):
        test_app = FastAPI()

        @test_app.get("/api/v1/orders/active")
        def active_orders():
            return []

        settings = load_settings({
            "TAKSKLAD_CORS_ORIGINS": "https://app.example.com",
        })
        configure_cors(test_app, settings)
        client = TestClient(test_app)

        response = client.options(
            "/api/v1/orders/active",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type,x-taksklad-csrf",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "https://app.example.com")
        self.assertIn("GET", response.headers["access-control-allow-methods"])
        self.assertIn("Authorization", response.headers["access-control-allow-headers"])
        self.assertIn("X-TakSklad-CSRF", response.headers["access-control-allow-headers"])

    def test_cookie_csrf_guard_requires_exact_origin_and_session_proof(self):
        auth_settings = load_settings({
            "TAKSKLAD_ENV": "local",
            "TAKSKLAD_WEB_SESSION_SECRET": "synthetic-csrf-session-secret-with-32-bytes",
            "TAKSKLAD_WEB_COOKIE_SECURE": "false",
        })
        session_token = "synthetic-session-token"
        csrf_token = csrf_token_for_session(auth_settings, session_token)

        def request(origin, candidate):
            return SimpleNamespace(
                cookies={SESSION_COOKIE_NAME: session_token},
                headers={
                    "host": "testserver",
                    "origin": origin,
                    "X-TakSklad-CSRF": candidate,
                },
                url=SimpleNamespace(netloc="testserver", scheme="http"),
            )

        original_settings = backend_main.settings
        try:
            backend_main.settings = auth_settings
            require_browser_request_security(request("http://testserver", csrf_token))
            for origin, candidate in (
                ("http://testserver", ""),
                ("http://testserver", "wrong-proof"),
                ("https://cross-origin.example.test", csrf_token),
                ("null", csrf_token),
            ):
                with self.assertRaises(HTTPException) as denied:
                    require_browser_request_security(request(origin, candidate))
                self.assertEqual(denied.exception.status_code, 403)
                self.assertNotIn(csrf_token, str(denied.exception.detail))
                self.assertNotIn(auth_settings.web_session_secret, str(denied.exception.detail))
        finally:
            backend_main.settings = original_settings

    def test_local_anonymous_context_requires_both_explicit_conditions(self):
        request = SimpleNamespace(cookies={})
        original_settings = backend_main.settings
        try:
            backend_main.settings = load_settings({"TAKSKLAD_ENV": "local"})
            with self.assertRaises(HTTPException) as denied:
                backend_main.read_auth_context(request)
            self.assertEqual(denied.exception.status_code, 401)

            backend_main.settings = load_settings({
                "TAKSKLAD_ENV": "local",
                "TAKSKLAD_INSECURE_LOCAL_ANONYMOUS": "1",
            })
            context = backend_main.read_auth_context(request)
            self.assertEqual(context.source, "local-dev")
        finally:
            backend_main.settings = original_settings

    def test_untrusted_peer_cannot_spoof_forwarded_identity(self):
        request = SimpleNamespace(
            client=SimpleNamespace(host="203.0.113.10"),
            headers={"x-forwarded-for": "198.51.100.77"},
        )

        self.assertEqual(client_identity(request, ("10.0.0.0/8",)), "203.0.113.10")

    def test_trusted_proxy_chain_uses_first_untrusted_hop_from_the_right(self):
        request = SimpleNamespace(
            client=SimpleNamespace(host="10.0.0.5"),
            headers={"x-forwarded-for": "198.51.100.77, 10.0.0.4"},
        )

        self.assertEqual(client_identity(request, ("10.0.0.0/8",)), "198.51.100.77")
        request.headers["x-forwarded-for"] = "malformed, 10.0.0.4"
        self.assertEqual(client_identity(request, ("10.0.0.0/8",)), "10.0.0.5")

    def test_login_limiter_is_bounded_and_expires_deterministically(self):
        now = [100.0]
        limiter = BoundedTTLLoginLimiter(
            max_entries=2,
            entry_ttl_seconds=10,
            clock=lambda: now[0],
        )
        limiter.register_failure("one", max_attempts=5, window_seconds=5, lock_seconds=30)
        limiter.register_failure("two", max_attempts=5, window_seconds=5, lock_seconds=30)
        with self.assertRaises(LoginLimiterCapacityExceeded) as capacity:
            limiter.register_failure("three", max_attempts=5, window_seconds=5, lock_seconds=30)
        self.assertEqual(capacity.exception.retry_after, 10)
        self.assertEqual(limiter.size(), 2)

        now[0] += 11
        self.assertEqual(limiter.size(), 0)
        with self.assertRaises(LoginRateLimited) as threshold:
            limiter.register_failure("locked", max_attempts=1, window_seconds=5, lock_seconds=30)
        self.assertEqual(threshold.exception.retry_after, 30)
        with self.assertRaises(LoginRateLimited):
            limiter.ensure_not_locked("locked")

    def test_login_limiter_key_has_fixed_size_for_oversized_login(self):
        request = SimpleNamespace(
            client=SimpleNamespace(host="203.0.113.10"),
            headers={},
        )

        key = login_attempt_key(request, "9" * 1_000_000)

        self.assertEqual(len(key), len("203.0.113.10:") + 64)

    def test_login_limiter_key_canonicalizes_optional_phone_plus(self):
        request = SimpleNamespace(
            client=SimpleNamespace(host="203.0.113.10"),
            headers={},
        )

        self.assertEqual(
            login_attempt_key(request, "+998 90 111 22 33"),
            login_attempt_key(request, "998901112233"),
        )

    def test_concurrent_login_lock_race_returns_429(self):
        class RaceLimiter:
            def register_failure(self, *args, **kwargs):
                raise LoginRateLimited(30)

        original_limiter = backend_main.login_limiter
        try:
            backend_main.login_limiter = RaceLimiter()
            with self.assertRaises(HTTPException) as captured:
                register_login_failure("fixed-key")
        finally:
            backend_main.login_limiter = original_limiter

        self.assertEqual(captured.exception.status_code, 429)
        self.assertEqual(captured.exception.headers["Retry-After"], "30")

    def test_existing_login_lock_returns_retry_after(self):
        class LockedLimiter:
            def ensure_not_locked(self, *_args, **_kwargs):
                raise LoginRateLimited(30.2)

        original_limiter = backend_main.login_limiter
        try:
            backend_main.login_limiter = LockedLimiter()
            with self.assertRaises(HTTPException) as captured:
                ensure_login_not_locked("fixed-key")
        finally:
            backend_main.login_limiter = original_limiter

        self.assertEqual(captured.exception.status_code, 429)
        self.assertEqual(captured.exception.headers["Retry-After"], "31")

    def test_full_login_limiter_returns_capacity_retry_after(self):
        class FullLimiter:
            def ensure_not_locked(self, *_args, **_kwargs):
                raise LoginLimiterCapacityExceeded(45.1)

        original_limiter = backend_main.login_limiter
        try:
            backend_main.login_limiter = FullLimiter()
            with self.assertRaises(HTTPException) as captured:
                ensure_login_not_locked("fixed-key")
        finally:
            backend_main.login_limiter = original_limiter

        self.assertEqual(captured.exception.status_code, 429)
        self.assertEqual(captured.exception.headers["Retry-After"], "46")


if __name__ == "__main__":
    unittest.main()
