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
from backend.app.main import client_identity, configure_cors, login_attempt_key, register_login_failure
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
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "https://app.example.com")
        self.assertIn("GET", response.headers["access-control-allow-methods"])
        self.assertIn("Authorization", response.headers["access-control-allow-headers"])

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
        with self.assertRaises(LoginLimiterCapacityExceeded):
            limiter.register_failure("three", max_attempts=5, window_seconds=5, lock_seconds=30)
        self.assertEqual(limiter.size(), 2)

        now[0] += 11
        self.assertEqual(limiter.size(), 0)
        limiter.register_failure("locked", max_attempts=1, window_seconds=5, lock_seconds=30)
        with self.assertRaises(LoginRateLimited):
            limiter.ensure_not_locked("locked")

    def test_login_limiter_key_has_fixed_size_for_oversized_login(self):
        request = SimpleNamespace(
            client=SimpleNamespace(host="203.0.113.10"),
            headers={},
        )

        key = login_attempt_key(request, "9" * 1_000_000)

        self.assertEqual(len(key), len("203.0.113.10:") + 64)

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


if __name__ == "__main__":
    unittest.main()
