import unittest
import uuid
from datetime import datetime, timezone
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import main as backend_main
from backend.app.login_limiter import BoundedTTLLoginLimiter
from backend.app.models import Base, User
from backend.app.settings import load_settings
from backend.app.station_access import (
    STATION_USERNAME,
    ensure_station_user,
    is_warehouse_address,
    parse_warehouse_cidrs,
)
from backend.app.web_auth import (
    SESSION_COOKIE_NAME,
    WebAuthError,
    authenticate_web_user,
    hash_password,
    normalize_login,
    role_permissions,
)

WAREHOUSE_IP = "203.0.113.10"
SECOND_WAREHOUSE_IP = "198.51.100.7"
OUTSIDE_IP = "198.51.100.99"
TRUSTED_PROXY_IP = "172.18.0.9"
ORIGIN = {"Origin": "http://testserver"}


def station_settings(**overrides):
    environ = {
        "TAKSKLAD_ENV": "test",
        "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
        "TAKSKLAD_LEGACY_AUTH_MODE": "disabled",
        "TAKSKLAD_WEB_SESSION_SECRET": "synthetic-station-session-secret-0123456789",
        "TAKSKLAD_WEB_COOKIE_SECURE": "false",
        "TAKSKLAD_TRUSTED_PROXY_CIDRS": "172.18.0.0/16",
        "TAKSKLAD_WAREHOUSE_CIDRS": f"{WAREHOUSE_IP}/32,{SECOND_WAREHOUSE_IP}/32",
    }
    environ.update(overrides)
    return load_settings(environ)


class StationNetworkParsingTests(unittest.TestCase):
    def test_settings_read_warehouse_cidrs(self):
        settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_WAREHOUSE_CIDRS": f"{WAREHOUSE_IP}/32, {SECOND_WAREHOUSE_IP}",
        })
        self.assertEqual(settings.warehouse_cidrs, (f"{WAREHOUSE_IP}/32", SECOND_WAREHOUSE_IP))
        self.assertEqual(load_settings({"TAKSKLAD_ENV": "test"}).warehouse_cidrs, ())

    def test_unsafe_or_broken_values_disable_whole_list(self):
        networks = parse_warehouse_cidrs([f"{WAREHOUSE_IP}/32"])
        self.assertTrue(is_warehouse_address(WAREHOUSE_IP, networks))
        self.assertFalse(is_warehouse_address(OUTSIDE_IP, networks))
        self.assertFalse(is_warehouse_address("unknown", networks))
        for values in (
            ["0.0.0.0/0"],
            ["::/0"],
            ["172.18.0.0/16"],
            ["192.168.1.0/24"],
            ["127.0.0.1"],
            ["not-a-network"],
            [f"{WAREHOUSE_IP}/24"],
            [f"{WAREHOUSE_IP}/32", "10.0.0.0/8"],
        ):
            with self.subTest(values=values):
                self.assertEqual(parse_warehouse_cidrs(values), ())


class StationAccessTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        backend_main.app.dependency_overrides[backend_main.get_db] = override_get_db
        self.settings_patch = mock.patch.object(backend_main, "settings", station_settings())
        self.settings_patch.start()
        self.limiter_patch = mock.patch.object(
            backend_main,
            "station_session_limiter",
            BoundedTTLLoginLimiter(max_entries=100, entry_ttl_seconds=120),
        )
        self.limiter_patch.start()

    def tearDown(self):
        self.limiter_patch.stop()
        self.settings_patch.stop()
        backend_main.app.dependency_overrides.clear()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def client_from(self, address):
        return TestClient(backend_main.app, client=(address, 50000))

    def station_login(self, client, headers=None):
        return client.post("/api/v1/auth/station", headers={**ORIGIN, **(headers or {})})

    def test_station_login_from_warehouse_issues_session(self):
        client = self.client_from(WAREHOUSE_IP)

        response = self.station_login(client)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["authenticated"])
        self.assertEqual(body["login"], STATION_USERNAME)
        self.assertEqual(body["role"], "station")
        self.assertEqual(body["permissions"], ["reports:read", "warehouse:read", "warehouse:write"])
        self.assertTrue(body["csrf_token"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn(SESSION_COOKIE_NAME, response.cookies)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertEqual(client.get("/api/v1/auth/check").status_code, 204)
        session = client.get("/api/v1/auth/session").json()
        self.assertTrue(session["authenticated"])
        self.assertEqual(session["role"], "station")
        self.assertEqual(client.get("/api/v1/orders/active").status_code, 200)

    def test_second_warehouse_address_is_accepted(self):
        self.assertEqual(self.station_login(self.client_from(SECOND_WAREHOUSE_IP)).status_code, 200)

    def test_outside_address_is_denied_with_code_and_no_shared_limiter_entry(self):
        client = self.client_from(OUTSIDE_IP)
        with mock.patch.object(backend_main, "register_login_failure") as shared_failure:
            response = self.station_login(client)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], {"code": "station_network_denied"})
        self.assertNotIn(SESSION_COOKIE_NAME, response.cookies)
        shared_failure.assert_not_called()
        with self.SessionLocal() as db:
            self.assertEqual(db.execute(select(func.count()).select_from(User)).scalar_one(), 0)

    def test_login_without_origin_is_denied(self):
        response = self.client_from(WAREHOUSE_IP).post("/api/v1/auth/station")

        self.assertEqual(response.status_code, 403)
        self.assertNotIn(SESSION_COOKIE_NAME, response.cookies)

    def test_empty_or_unsafe_cidr_list_disables_station(self):
        for value in ("", "0.0.0.0/0", "172.18.0.0/16", "not-a-network", f"{WAREHOUSE_IP}/32,::/0"):
            with self.subTest(value=value):
                with mock.patch.object(backend_main, "settings", station_settings(TAKSKLAD_WAREHOUSE_CIDRS=value)):
                    response = self.station_login(self.client_from(WAREHOUSE_IP))
                self.assertEqual(response.status_code, 403)

    def test_station_session_is_rejected_outside_warehouse(self):
        warehouse = self.client_from(WAREHOUSE_IP)
        token = self.station_login(warehouse).cookies[SESSION_COOKIE_NAME]
        outside = self.client_from(OUTSIDE_IP)
        outside.cookies.set(SESSION_COOKIE_NAME, token)

        self.assertEqual(outside.get("/api/v1/auth/check").status_code, 401)
        self.assertFalse(outside.get("/api/v1/auth/session").json()["authenticated"])
        self.assertEqual(outside.get("/api/v1/orders/active").status_code, 401)
        self.assertEqual(warehouse.get("/api/v1/auth/check").status_code, 204)

    def test_forwarded_header_counts_only_behind_trusted_proxy(self):
        spoofed = self.station_login(self.client_from(OUTSIDE_IP), {"X-Forwarded-For": WAREHOUSE_IP})
        self.assertEqual(spoofed.status_code, 403)

        proxied = self.station_login(self.client_from(TRUSTED_PROXY_IP), {"X-Forwarded-For": WAREHOUSE_IP})
        self.assertEqual(proxied.status_code, 200)

        prepended = self.station_login(
            self.client_from(TRUSTED_PROXY_IP),
            {"X-Forwarded-For": f"{WAREHOUSE_IP}, {OUTSIDE_IP}"},
        )
        self.assertEqual(prepended.status_code, 403)

    def test_auth_check_behind_proxy_needs_forwarded_address(self):
        proxy = self.client_from(TRUSTED_PROXY_IP)
        forwarded = {"X-Forwarded-For": WAREHOUSE_IP}
        self.assertEqual(self.station_login(proxy, forwarded).status_code, 200)

        self.assertEqual(proxy.get("/api/v1/auth/check", headers=forwarded).status_code, 204)
        self.assertEqual(proxy.get("/api/v1/orders/active", headers=forwarded).status_code, 200)
        # nginx без X-Forwarded-For: backend видит только адрес прокси и отказывает
        self.assertEqual(proxy.get("/api/v1/auth/check").status_code, 401)

    def test_station_user_cannot_log_in_with_password(self):
        self.assertEqual(normalize_login(STATION_USERNAME), "")
        with self.SessionLocal() as db:
            ensure_station_user(db)
            db.commit()
            with self.assertRaises(WebAuthError):
                authenticate_web_user(backend_main.settings, STATION_USERNAME, "", db=db)

    def test_station_user_is_created_once(self):
        with self.SessionLocal() as db:
            first = ensure_station_user(db)
            second = ensure_station_user(db)
            db.commit()
            self.assertEqual(first.id, second.id)
            self.assertIsNone(first.password_hash)
            self.assertEqual(first.role, "station")
            self.assertEqual(
                db.execute(select(func.count()).select_from(User).where(User.username == STATION_USERNAME)).scalar_one(),
                1,
            )

    def test_deactivated_station_user_loses_sessions(self):
        client = self.client_from(WAREHOUSE_IP)
        self.station_login(client)
        with self.SessionLocal() as db:
            user = db.execute(select(User).where(User.username == STATION_USERNAME)).scalar_one()
            user.is_active = False
            db.commit()

        self.assertEqual(client.get("/api/v1/auth/check").status_code, 401)
        self.assertEqual(self.station_login(client).status_code, 503)

    def test_other_roles_are_not_bound_to_warehouse_network(self):
        now = datetime.now(timezone.utc)
        with self.SessionLocal() as db:
            db.add(User(
                id=uuid.uuid4(),
                username="998000000001",
                password_hash=hash_password("synthetic-password", salt="synthetic-salt", iterations=1000),
                role="operator",
                is_active=True,
                auth_version=1,
                created_at=now,
                updated_at=now,
            ))
            db.commit()
        outside = self.client_from(OUTSIDE_IP)
        login = outside.post(
            "/api/v1/auth/login",
            json={"login": "998000000001", "password": "synthetic-password"},
            headers=ORIGIN,
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(outside.get("/api/v1/auth/check").status_code, 204)

    def test_station_login_refused_when_station_user_has_foreign_role(self):
        now = datetime.now(timezone.utc)
        with self.SessionLocal() as db:
            db.add(User(
                id=uuid.uuid4(),
                username=STATION_USERNAME,
                password_hash=None,
                role="admin",
                is_active=True,
                auth_version=1,
                created_at=now,
                updated_at=now,
            ))
            db.commit()
        client = self.client_from(WAREHOUSE_IP)

        response = self.station_login(client)

        self.assertEqual(response.status_code, 503)
        self.assertNotIn(SESSION_COOKIE_NAME, response.cookies)
        self.assertEqual(client.get("/api/v1/auth/check").status_code, 401)

    def test_successful_station_logins_are_limited_per_address(self):
        client = self.client_from(WAREHOUSE_IP)
        statuses = [self.station_login(client).status_code for _ in range(11)]

        self.assertEqual(statuses[:10], [200] * 10)
        self.assertEqual(statuses[10], 429)
        self.assertEqual(self.station_login(self.client_from(SECOND_WAREHOUSE_IP)).status_code, 200)

    def test_station_role_permissions(self):
        self.assertEqual(role_permissions("station"), ("reports:read", "warehouse:read", "warehouse:write"))
        self.assertTrue(is_warehouse_address(WAREHOUSE_IP, parse_warehouse_cidrs([f"{WAREHOUSE_IP}/32"])))


if __name__ == "__main__":
    unittest.main()
