import unittest
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import main as backend_main
from backend.app import station_access
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

    def setUp(self):
        station_access._parse_cached.cache_clear()
        self.addCleanup(station_access._parse_cached.cache_clear)

    def test_unsafe_or_broken_values_disable_whole_list(self):
        networks = parse_warehouse_cidrs([f"{WAREHOUSE_IP}/32"])
        self.assertTrue(is_warehouse_address(WAREHOUSE_IP, networks))
        self.assertFalse(is_warehouse_address(OUTSIDE_IP, networks))
        self.assertFalse(is_warehouse_address("unknown", networks))
        for values, offender in (
            (["0.0.0.0/0"], "0.0.0.0/0"),
            (["::/0"], "::/0"),
            (["172.18.0.0/16"], "172.18.0.0/16"),
            (["192.168.1.0/24"], "192.168.1.0/24"),
            (["127.0.0.1"], "127.0.0.1"),
            (["not-a-network"], "not-a-network"),
            ([f"{WAREHOUSE_IP}/24"], f"{WAREHOUSE_IP}/24"),
            ([f"{WAREHOUSE_IP}/32", "10.0.0.0/8"], "10.0.0.0/8"),
        ):
            with self.subTest(values=values):
                with self.assertLogs("backend.app.station_access", level="ERROR") as captured:
                    self.assertEqual(parse_warehouse_cidrs(values), ())
                self.assertEqual(len(captured.records), 1)
                self.assertIn(offender, captured.output[0])
                self.assertIn("станция выключена", captured.output[0])

    def test_valid_values_parse_and_empty_list_is_empty(self):
        with self.assertNoLogs("backend.app.station_access", level="ERROR"):
            bare = parse_warehouse_cidrs([SECOND_WAREHOUSE_IP])
            self.assertEqual(parse_warehouse_cidrs([]), ())
            self.assertEqual(parse_warehouse_cidrs(()), ())
            self.assertEqual(parse_warehouse_cidrs(None), ())
        self.assertEqual(len(bare), 1)
        self.assertEqual(str(bare[0]), f"{SECOND_WAREHOUSE_IP}/32")
        self.assertTrue(is_warehouse_address(SECOND_WAREHOUSE_IP, bare))

    def test_same_broken_value_is_logged_once(self):
        with self.assertLogs("backend.app.station_access", level="ERROR") as captured:
            for _ in range(3):
                self.assertEqual(parse_warehouse_cidrs(["not-a-network"]), ())
        self.assertEqual(len(captured.records), 1)
        with self.assertLogs("backend.app.station_access", level="ERROR") as captured:
            self.assertEqual(parse_warehouse_cidrs(["another-broken"]), ())
        self.assertEqual(len(captured.records), 1)


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
        self.denied_log_patch = mock.patch.object(backend_main, "station_denied_log_times", OrderedDict())
        self.denied_log_patch.start()
        station_access._parse_cached.cache_clear()

    def tearDown(self):
        self.denied_log_patch.stop()
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

    def test_two_hop_forwarded_chain_from_trusted_proxy_logs_in(self):
        # Так приходит запрос из прода: nginx дописывает к адресу клиента адрес traefik
        proxy = self.client_from(TRUSTED_PROXY_IP)
        chain = {"X-Forwarded-For": f"{WAREHOUSE_IP}, 172.18.0.5"}

        self.assertEqual(self.station_login(proxy, chain).status_code, 200)
        self.assertEqual(proxy.get("/api/v1/auth/check", headers=chain).status_code, 204)
        outside_chain = {"X-Forwarded-For": f"{OUTSIDE_IP}, 172.18.0.5"}
        self.assertEqual(self.station_login(proxy, outside_chain).status_code, 403)

    def test_login_refusal_writes_one_warning_per_minute_per_address(self):
        client = self.client_from(OUTSIDE_IP)
        clock = mock.Mock(monotonic=mock.Mock(return_value=1000.0))
        with mock.patch.object(backend_main, "time", clock):
            with self.assertLogs("backend.app.main", level="WARNING") as captured:
                self.assertEqual(self.station_login(client).status_code, 403)
            self.assertEqual(len(captured.records), 1)
            self.assertIn(OUTSIDE_IP, captured.output[0])

            clock.monotonic.return_value = 1030.0
            with self.assertNoLogs("backend.app.main", level="WARNING"):
                self.assertEqual(self.station_login(client).status_code, 403)

            with self.assertLogs("backend.app.main", level="WARNING") as other:
                self.assertEqual(self.station_login(self.client_from("198.51.100.55")).status_code, 403)
            self.assertIn("198.51.100.55", other.output[0])

            clock.monotonic.return_value = 1061.0
            with self.assertLogs("backend.app.main", level="WARNING") as again:
                self.assertEqual(self.station_login(client).status_code, 403)
            self.assertIn(OUTSIDE_IP, again.output[0])

    def test_session_refusal_outside_warehouse_logs_address_without_token(self):
        token = self.station_login(self.client_from(WAREHOUSE_IP)).cookies[SESSION_COOKIE_NAME]
        outside = self.client_from(OUTSIDE_IP)
        outside.cookies.set(SESSION_COOKIE_NAME, token)

        with self.assertLogs("backend.app.main", level="WARNING") as captured:
            for _ in range(3):
                self.assertEqual(outside.get("/api/v1/auth/check").status_code, 401)
        self.assertEqual(len(captured.records), 1)
        self.assertIn(OUTSIDE_IP, captured.output[0])
        self.assertNotIn(token, captured.output[0])

    def test_denied_log_state_is_bounded_and_drops_oldest(self):
        with mock.patch.object(backend_main, "STATION_DENIED_LOG_MAX_ENTRIES", 3):
            with self.assertLogs("backend.app.main", level="WARNING"):
                for index in range(5):
                    backend_main.log_station_refusal(f"198.51.100.{index}", "вход")

        self.assertEqual(list(backend_main.station_denied_log_times), ["198.51.100.2", "198.51.100.3", "198.51.100.4"])

    def test_startup_logs_station_state_once(self):
        def started(**overrides):
            with mock.patch.object(backend_main, "settings", station_settings(**overrides)):
                with mock.patch.object(backend_main, "validate_backend_settings"), \
                        mock.patch.object(backend_main, "start_device_pairing_sweeper"):
                    with self.assertLogs("backend.app.main", level="INFO") as captured:
                        backend_main.validate_startup_configuration()
            return captured.output

        enabled = started()
        self.assertEqual(len(enabled), 1)
        self.assertIn("станция включена: 2 сетей", enabled[0])
        self.assertNotIn(WAREHOUSE_IP, enabled[0])
        for overrides in (
            {"TAKSKLAD_WAREHOUSE_CIDRS": ""},
            {"TAKSKLAD_IDENTITY_AUTH_ENABLED": "false"},
        ):
            with self.subTest(overrides=overrides):
                disabled = started(**overrides)
                self.assertEqual(len(disabled), 1)
                self.assertIn("станция выключена", disabled[0])

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
