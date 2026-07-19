import os
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import main as backend_main
from backend.app.auth_identities import (
    SERVICE_PRINCIPAL_SCOPE_MATRIX,
    create_user_session,
    issue_service_token,
)
from backend.app.csrf import csrf_token_for_session
from backend.app.device_pairing_service import (
    DevicePairingError,
    acknowledge_desktop_pairing,
    bootstrap_desktop,
    build_device_pairing_readiness,
    cleanup_expired_pairings,
    create_desktop_pairing,
    redeem_desktop_pairing,
    run_device_pairing_sweeper_loop,
)
from backend.app.models import (
    AuditLog,
    Base,
    DesktopPairing,
    DesktopPairingMaintenance,
    DesktopPairingRateLimit,
    ServicePrincipal,
    ServicePrincipalToken,
    User,
)
from backend.app.settings import load_settings
from backend.app.web_auth import SESSION_COOKIE_NAME
from tests.postgres_support import create_database, drop_database, run_alembic


PEPPER = "synthetic-device-pairing-pepper-with-32-bytes"
NOW = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


class DevicePairingServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.sessions()
        self.admin = User(
            id=uuid.uuid4(),
            username="synthetic-pairing-admin",
            password_hash="synthetic",
            role="admin",
            is_active=True,
            auth_version=1,
            created_at=NOW,
            updated_at=NOW,
        )
        self.db.add(self.admin)
        self.db.commit()

    def tearDown(self):
        backend_main.app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()

    def create(self, **overrides):
        values = {
            "pepper": PEPPER,
            "created_by_user_id": self.admin.id,
            "device_label": "Warehouse PC",
            "rate_key": "192.0.2.20",
            "now": NOW,
        }
        values.update(overrides)
        return create_desktop_pairing(self.db, **values)

    def test_code_and_credential_are_one_time_and_never_persisted(self):
        created = self.create()
        pairing = self.db.get(DesktopPairing, created.pairing_id)
        self.assertEqual(len(pairing.setup_code_digest), 64)
        self.assertNotEqual(pairing.setup_code_digest, created.setup_code)
        self.assertNotIn(created.setup_code, repr(pairing.__dict__))

        redeemed = redeem_desktop_pairing(
            self.db,
            pepper=PEPPER,
            setup_code=created.setup_code,
            desktop_version="2.0.50",
            rate_key="192.0.2.20",
            now=NOW + timedelta(seconds=1),
        )
        principal = self.db.get(ServicePrincipal, pairing.principal_id)
        token = self.db.get(ServicePrincipalToken, pairing.token_id)
        self.assertEqual(principal.kind, "desktop")
        self.assertEqual(set(principal.scopes), set(SERVICE_PRINCIPAL_SCOPE_MATRIX["desktop"]))
        self.assertTrue(principal.identifier.startswith("desktop.paired."))
        self.assertNotIn(redeemed.credential, repr(token.__dict__))
        self.assertEqual(len(token.token_digest), 64)
        self.assertLessEqual(
            abs((_aware(token.expires_at) - redeemed.ack_deadline).total_seconds()),
            1,
        )
        audit_text = repr([row.payload for row in self.db.execute(select(AuditLog)).scalars()])
        self.assertNotIn(created.setup_code, audit_text)
        self.assertNotIn(redeemed.credential, audit_text)

        with self.assertRaises(DevicePairingError) as replay:
            redeem_desktop_pairing(
                self.db,
                pepper=PEPPER,
                setup_code=created.setup_code,
                rate_key="192.0.2.20",
                now=NOW + timedelta(seconds=2),
            )
        self.assertEqual(replay.exception.status_code, 401)

    def test_public_bootstrap_issues_unacked_exact_desktop_scopes_and_persists_digest_only(self):
        bootstrapped = bootstrap_desktop(
            self.db,
            pepper=PEPPER,
            desktop_version="2.0.50",
            rate_key="203.0.113.40",
            now=NOW,
        )

        pairing = self.db.get(DesktopPairing, bootstrapped.pairing_id)
        principal = self.db.get(ServicePrincipal, pairing.principal_id)
        token = self.db.get(ServicePrincipalToken, pairing.token_id)
        audit = self.db.execute(
            select(AuditLog).where(AuditLog.action == "desktop_public_bootstrap_issued")
        ).scalar_one()

        self.assertEqual(principal.kind, "desktop")
        self.assertEqual(set(principal.scopes), set(SERVICE_PRINCIPAL_SCOPE_MATRIX["desktop"]))
        self.assertEqual(principal.identifier, bootstrapped.principal_identifier)
        self.assertTrue(principal.identifier.startswith("desktop.bootstrap."))
        self.assertEqual(pairing.status, "redeemed_unacked")
        self.assertIsNone(pairing.created_by_user_id)
        self.assertEqual(len(pairing.setup_code_digest), 64)
        self.assertEqual(len(token.token_digest), 64)
        self.assertNotEqual(token.token_digest, bootstrapped.credential)
        self.assertNotIn(bootstrapped.credential, repr(token.__dict__))
        self.assertEqual(_aware(token.expires_at), bootstrapped.ack_deadline)
        self.assertEqual(bootstrapped.ack_deadline, NOW + timedelta(minutes=5))
        audit_text = repr(audit.__dict__)
        self.assertNotIn(bootstrapped.credential, audit_text)
        self.assertNotIn(token.token_digest, audit_text)
        self.assertEqual(audit.payload["desktop_version_present"], True)
        self.assertNotIn("203.0.113.40", audit_text)
        self.assertRegex(audit.payload["source_digest"], r"^[0-9a-f]{64}$")

        acked = acknowledge_desktop_pairing(
            self.db,
            bootstrapped.pairing_id,
            auth_principal_id=principal.id,
            auth_token_id=token.id,
            now=NOW + timedelta(seconds=1),
        )
        self.assertEqual(acked["status"], "acked")
        self.assertEqual(
            acked["credential_expires_at"],
            NOW + timedelta(seconds=1, days=365),
        )

    def test_public_bootstrap_rate_limit_is_hashed_and_persists_across_sessions(self):
        rate_key = "203.0.113.41"
        with mock.patch("backend.app.device_pairing_service.PUBLIC_BOOTSTRAP_RATE_LIMIT", 2):
            for _index in range(2):
                bootstrap_desktop(
                    self.db,
                    pepper=PEPPER,
                    desktop_version="2.0.50",
                    rate_key=rate_key,
                    now=NOW,
                )

            self.db.close()
            self.db = self.sessions()
            with self.assertRaises(DevicePairingError) as limited:
                bootstrap_desktop(
                    self.db,
                    pepper=PEPPER,
                    desktop_version="2.0.50",
                    rate_key=rate_key,
                    now=NOW + timedelta(seconds=1),
                )

        self.assertEqual(limited.exception.status_code, 429)
        rate_row = self.db.execute(select(DesktopPairingRateLimit)).scalar_one()
        self.assertEqual(len(rate_row.bucket_digest), 64)
        self.assertNotIn(rate_key, repr(rate_row.__dict__))

    def test_public_bootstrap_has_a_global_unacked_capacity_limit(self):
        with mock.patch("backend.app.device_pairing_service.GLOBAL_PENDING_CAP", 2):
            for index in range(2):
                bootstrap_desktop(
                    self.db,
                    pepper=PEPPER,
                    desktop_version="2.0.51",
                    rate_key=f"203.0.113.{50 + index}",
                    now=NOW,
                )
            with self.assertRaises(DevicePairingError) as unavailable:
                bootstrap_desktop(
                    self.db,
                    pepper=PEPPER,
                    desktop_version="2.0.51",
                    rate_key="203.0.113.52",
                    now=NOW,
                )

        self.assertEqual(unavailable.exception.status_code, 503)
        self.assertEqual(
            len(self.db.execute(select(DesktopPairing)).scalars().all()),
            2,
        )

    def test_public_bootstrap_rejects_unbounded_desktop_version(self):
        for version in ("", "x" * 41, "2.0.50/unsafe"):
            with self.subTest(version=version), self.assertRaises(DevicePairingError) as invalid:
                bootstrap_desktop(
                    self.db,
                    pepper=PEPPER,
                    desktop_version=version,
                    rate_key="203.0.113.42",
                    now=NOW,
                )
            self.assertEqual(invalid.exception.status_code, 422)

    def test_ack_is_bound_to_exact_token_and_idempotently_extends_it(self):
        created = self.create()
        redeemed = redeem_desktop_pairing(
            self.db,
            pepper=PEPPER,
            setup_code=created.setup_code,
            rate_key="192.0.2.21",
            now=NOW + timedelta(seconds=1),
        )
        pairing = self.db.get(DesktopPairing, created.pairing_id)
        with self.assertRaises(DevicePairingError) as denied:
            acknowledge_desktop_pairing(
                self.db,
                pairing.id,
                auth_principal_id=pairing.principal_id,
                auth_token_id=uuid.uuid4(),
                now=NOW + timedelta(seconds=2),
            )
        self.assertEqual(denied.exception.status_code, 403)

        acked = acknowledge_desktop_pairing(
            self.db,
            pairing.id,
            auth_principal_id=pairing.principal_id,
            auth_token_id=pairing.token_id,
            now=NOW + timedelta(seconds=2),
        )
        repeated = acknowledge_desktop_pairing(
            self.db,
            pairing.id,
            auth_principal_id=pairing.principal_id,
            auth_token_id=pairing.token_id,
            now=NOW + timedelta(seconds=3),
        )
        self.assertEqual(acked, repeated)
        self.assertEqual(acked["status"], "acked")
        self.assertGreater(acked["credential_expires_at"], redeemed.ack_deadline)

    def test_orphan_cleanup_revokes_token_and_deactivates_only_new_principal(self):
        created = self.create()
        redeem_desktop_pairing(
            self.db,
            pepper=PEPPER,
            setup_code=created.setup_code,
            rate_key="192.0.2.22",
            now=NOW,
        )
        pairing = self.db.get(DesktopPairing, created.pairing_id)
        cleanup_expired_pairings(self.db, now=NOW + timedelta(seconds=301))
        self.assertEqual(pairing.status, "revoked")
        self.assertFalse(self.db.get(ServicePrincipal, pairing.principal_id).is_active)
        self.assertIsNotNone(self.db.get(ServicePrincipalToken, pairing.token_id).revoked_at)

    def test_redeem_rate_lock_persists_across_database_sessions(self):
        for _index in range(10):
            with self.assertRaises(DevicePairingError) as invalid:
                redeem_desktop_pairing(
                    self.db,
                    pepper=PEPPER,
                    setup_code="A" * 43,
                    rate_key="198.51.100.9",
                    now=NOW,
                )
            self.assertEqual(invalid.exception.status_code, 401)
        with self.assertRaises(DevicePairingError) as limited:
            redeem_desktop_pairing(
                self.db,
                pepper=PEPPER,
                setup_code="A" * 43,
                rate_key="198.51.100.9",
                now=NOW,
            )
        self.assertEqual(limited.exception.status_code, 429)
        self.db.close()
        self.db = self.sessions()
        with self.assertRaises(DevicePairingError) as still_limited:
            redeem_desktop_pairing(
                self.db,
                pepper=PEPPER,
                setup_code="A" * 43,
                rate_key="198.51.100.9",
                now=NOW + timedelta(seconds=1),
            )
        self.assertEqual(still_limited.exception.status_code, 429)
        row = self.db.execute(select(DesktopPairingRateLimit)).scalar_one()
        self.assertIsNotNone(row.locked_until)

    def test_readiness_requires_fresh_persisted_sweeper_heartbeat(self):
        missing = build_device_pairing_readiness(self.db, now=NOW, require_sweeper=True)
        self.assertEqual(missing["status"], "unhealthy")
        self.db.add(DesktopPairingMaintenance(
            name="sweeper",
            last_started_at=NOW,
            last_succeeded_at=NOW,
            updated_at=NOW,
        ))
        self.db.commit()
        fresh = build_device_pairing_readiness(self.db, now=NOW + timedelta(seconds=30), require_sweeper=True)
        stale = build_device_pairing_readiness(self.db, now=NOW + timedelta(seconds=61), require_sweeper=True)
        self.assertEqual(fresh["status"], "ok")
        self.assertEqual(stale["status"], "unhealthy")
        self.assertTrue(stale["sweeper_heartbeat_stale"])

    def test_http_contract_enforces_browser_admin_csrf_and_exact_ack_token(self):
        session_now = datetime.now(timezone.utc)
        issued_session = create_user_session(
            self.db,
            self.admin,
            expires_at=session_now + timedelta(hours=1),
            now=session_now,
            secret_factory=lambda _count: "synthetic-admin-session-secret-material-000000000000",
        )
        operator = User(
            id=uuid.uuid4(),
            username="synthetic-pairing-operator",
            password_hash="synthetic",
            role="operator",
            is_active=True,
            auth_version=1,
            created_at=session_now,
            updated_at=session_now,
        )
        self.db.add(operator)
        self.db.flush()
        operator_session = create_user_session(
            self.db,
            operator,
            expires_at=session_now + timedelta(hours=1),
            now=session_now,
            secret_factory=lambda _count: "synthetic-operator-session-secret-material-0000000000",
        )
        existing_desktop = ServicePrincipal(
            id=uuid.uuid4(),
            identifier="desktop.synthetic-existing",
            kind="desktop",
            scopes=sorted(SERVICE_PRINCIPAL_SCOPE_MATRIX["desktop"]),
            is_active=True,
            created_at=session_now,
            updated_at=session_now,
        )
        self.db.add(existing_desktop)
        self.db.flush()
        desktop_bearer = issue_service_token(
            self.db,
            existing_desktop,
            expires_at=session_now + timedelta(hours=1),
            now=session_now,
            secret_factory=lambda _count: "synthetic-existing-desktop-secret-material-000000000",
        ).token
        self.db.commit()
        app_settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
            "TAKSKLAD_LEGACY_AUTH_MODE": "disabled",
            "TAKSKLAD_WEB_SESSION_SECRET": PEPPER,
            "TAKSKLAD_WEB_COOKIE_SECURE": "false",
        })

        def override_db():
            db = self.sessions()
            try:
                yield db
            finally:
                db.close()

        backend_main.app.dependency_overrides[backend_main.get_db] = override_db
        client = TestClient(backend_main.app, base_url="http://testserver")
        csrf = csrf_token_for_session(app_settings, issued_session.token)
        with mock.patch.object(backend_main, "settings", app_settings):
            def pairing_count():
                with self.sessions() as check_db:
                    return len(check_db.execute(select(DesktopPairing)).scalars().all())

            baseline = pairing_count()
            missing_csrf = client.post(
                "/api/v1/admin/desktop-pairings",
                json={"device_label": "Warehouse PC"},
                headers={"Origin": "http://testserver"},
                cookies={SESSION_COOKIE_NAME: issued_session.token},
            )
            self.assertEqual(missing_csrf.status_code, 403)
            self.assertEqual(pairing_count(), baseline)
            cross_origin = client.post(
                "/api/v1/admin/desktop-pairings",
                json={"device_label": "Warehouse PC"},
                headers={"Origin": "https://cross-origin.example", "X-TakSklad-CSRF": csrf},
                cookies={SESSION_COOKIE_NAME: issued_session.token},
            )
            self.assertEqual(cross_origin.status_code, 403)
            self.assertEqual(pairing_count(), baseline)
            operator_denied = client.post(
                "/api/v1/admin/desktop-pairings",
                json={"device_label": "Warehouse PC"},
                headers={
                    "Origin": "http://testserver",
                    "X-TakSklad-CSRF": csrf_token_for_session(app_settings, operator_session.token),
                },
                cookies={SESSION_COOKIE_NAME: operator_session.token},
            )
            self.assertEqual(operator_denied.status_code, 403)
            self.assertEqual(pairing_count(), baseline)
            bearer_denied = client.post(
                "/api/v1/admin/desktop-pairings",
                json={"device_label": "Warehouse PC"},
                headers={"Authorization": f"Bearer {desktop_bearer}"},
            )
            self.assertEqual(bearer_denied.status_code, 403)
            self.assertEqual(pairing_count(), baseline)
            legacy_settings = load_settings({
                "TAKSKLAD_ENV": "test",
                "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
                "TAKSKLAD_LEGACY_AUTH_MODE": "enforce",
                "TAKSKLAD_LEGACY_AUTH_EXPIRES_AT": (session_now + timedelta(hours=1)).isoformat(),
                "TAKSKLAD_API_TOKEN": "synthetic-legacy-pairing-token",
                "TAKSKLAD_WEB_SESSION_SECRET": PEPPER,
                "TAKSKLAD_WEB_COOKIE_SECURE": "false",
            })
            with mock.patch.object(backend_main, "settings", legacy_settings):
                legacy_denied = client.post(
                    "/api/v1/admin/desktop-pairings",
                    json={"device_label": "Warehouse PC"},
                    headers={"Authorization": "Bearer synthetic-legacy-pairing-token"},
                )
            self.assertEqual(legacy_denied.status_code, 403)
            self.assertEqual(pairing_count(), baseline)
            created = client.post(
                "/api/v1/admin/desktop-pairings",
                json={"device_label": "Warehouse PC"},
                headers={"Origin": "http://testserver", "X-TakSklad-CSRF": csrf},
                cookies={SESSION_COOKIE_NAME: issued_session.token},
            )
            self.assertEqual(created.status_code, 200, created.text)
            self.assertEqual(created.headers.get("cache-control"), "no-store")
            setup_code = created.json()["setup_code"]
            redeemed = client.post(
                "/api/v1/auth/desktop-pairing/redeem",
                json={"setup_code": setup_code, "desktop_version": "2.0.50"},
            )
            self.assertEqual(redeemed.status_code, 200, redeemed.text)
            self.assertEqual(redeemed.headers.get("cache-control"), "no-store")
            replay = client.post(
                "/api/v1/auth/desktop-pairing/redeem",
                json={"setup_code": setup_code, "desktop_version": "2.0.50"},
            )
            self.assertEqual(replay.status_code, 401)
            ack = client.post(
                f"/api/v1/auth/desktop-pairing/{redeemed.json()['pairing_id']}/ack",
                json={},
                headers={"Authorization": f"Bearer {redeemed.json()['credential']}"},
            )
            self.assertEqual(ack.status_code, 200, ack.text)
            self.assertEqual(ack.json()["status"], "acked")
            self.assertEqual(ack.headers.get("cache-control"), "no-store")

    def test_public_bootstrap_http_contract_needs_no_auth_or_csrf_and_is_no_store(self):
        app_settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
            "TAKSKLAD_LEGACY_AUTH_MODE": "disabled",
            "TAKSKLAD_WEB_SESSION_SECRET": PEPPER,
            "TAKSKLAD_WEB_COOKIE_SECURE": "false",
        })

        def override_db():
            db = self.sessions()
            try:
                yield db
            finally:
                db.close()

        backend_main.app.dependency_overrides[backend_main.get_db] = override_db
        client = TestClient(backend_main.app, base_url="http://testserver")
        with (
            mock.patch.object(backend_main, "settings", app_settings),
            mock.patch.object(
                backend_main,
                "client_identity",
                return_value="203.0.113.43",
            ) as identity,
        ):
            response = client.post(
                "/api/v1/auth/desktop-bootstrap",
                json={"desktop_version": "2.0.50"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual(response.headers.get("pragma"), "no-cache")
        self.assertEqual(
            set(response.json()),
            {"pairing_id", "credential", "principal_identifier", "ack_deadline"},
        )
        self.assertTrue(response.json()["credential"].startswith("tks."))
        self.assertNotIn("set-cookie", response.headers)
        identity.assert_called_once_with(mock.ANY, app_settings.trusted_proxy_cidrs)

    def test_public_bootstrap_http_contract_rate_limits_by_client_identity(self):
        app_settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
            "TAKSKLAD_LEGACY_AUTH_MODE": "disabled",
            "TAKSKLAD_WEB_SESSION_SECRET": PEPPER,
            "TAKSKLAD_WEB_COOKIE_SECURE": "false",
        })

        def override_db():
            db = self.sessions()
            try:
                yield db
            finally:
                db.close()

        backend_main.app.dependency_overrides[backend_main.get_db] = override_db
        client = TestClient(backend_main.app, base_url="http://testserver")
        with (
            mock.patch.object(backend_main, "settings", app_settings),
            mock.patch("backend.app.device_pairing_service.PUBLIC_BOOTSTRAP_RATE_LIMIT", 2),
            mock.patch.object(
                backend_main,
                "client_identity",
                return_value="203.0.113.44",
            ) as identity,
        ):
            responses = [
                client.post(
                    "/api/v1/auth/desktop-bootstrap",
                    json={"desktop_version": "2.0.50"},
                    headers={"X-Forwarded-For": f"198.51.100.{index}"},
                )
                for index in range(3)
            ]

        self.assertEqual([response.status_code for response in responses[:2]], [200] * 2)
        self.assertEqual(responses[2].status_code, 429)
        self.assertEqual(responses[2].headers.get("cache-control"), "no-store")
        self.assertGreater(int(responses[2].headers["retry-after"]), 0)
        self.assertEqual(identity.call_count, 3)

    def test_sweeper_records_heartbeat_on_empty_database(self):
        stop = threading.Event()
        thread = threading.Thread(
            target=run_device_pairing_sweeper_loop,
            args=(self.sessions,),
            kwargs={"stop_event": stop, "interval_seconds": 1},
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 2
        heartbeat = None
        while time.monotonic() < deadline:
            self.db.expire_all()
            heartbeat = self.db.get(DesktopPairingMaintenance, "sweeper")
            if heartbeat is not None and heartbeat.last_succeeded_at is not None:
                break
            time.sleep(0.02)
        stop.set()
        thread.join(timeout=2)
        self.assertIsNotNone(heartbeat)
        self.assertIsNotNone(heartbeat.last_succeeded_at)


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresDevicePairingConcurrencyTests(unittest.TestCase):
    database_name = "taksklad_desktop_pairing_concurrency"

    @classmethod
    def setUpClass(cls):
        cls.url = create_database(cls.database_name)
        run_alembic(cls.url, "upgrade", "head")
        cls.engine = create_engine(cls.url, pool_pre_ping=True)
        cls.sessions = sessionmaker(bind=cls.engine, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.engine.dispose()
        drop_database(cls.database_name)

    def test_concurrent_redeem_creates_exactly_one_principal_and_token(self):
        now = datetime.now(timezone.utc)
        with self.sessions() as db:
            admin = User(
                id=uuid.uuid4(),
                username="synthetic-concurrent-pairing-admin",
                password_hash="synthetic",
                role="admin",
                is_active=True,
                auth_version=1,
                created_at=now,
                updated_at=now,
            )
            db.add(admin)
            db.commit()
            created = create_desktop_pairing(
                db,
                pepper=PEPPER,
                created_by_user_id=admin.id,
                rate_key="192.0.2.100",
                now=now,
            )

        gate = threading.Barrier(2)

        def redeem(index):
            with self.sessions() as db:
                gate.wait(timeout=5)
                try:
                    result = redeem_desktop_pairing(
                        db,
                        pepper=PEPPER,
                        setup_code=created.setup_code,
                        rate_key=f"192.0.2.{101 + index}",
                        now=now + timedelta(seconds=1),
                    )
                    return ("ok", str(result.pairing_id))
                except DevicePairingError as exc:
                    return ("denied", exc.status_code)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(redeem, (0, 1)))
        self.assertEqual(sum(result[0] == "ok" for result in results), 1)
        self.assertEqual(sum(result[0] == "denied" for result in results), 1)
        with self.sessions() as db:
            pairing = db.get(DesktopPairing, created.pairing_id)
            self.assertEqual(pairing.status, "redeemed_unacked")
            self.assertEqual(len(db.execute(select(ServicePrincipal)).scalars().all()), 1)
            self.assertEqual(len(db.execute(select(ServicePrincipalToken)).scalars().all()), 1)


def _aware(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


if __name__ == "__main__":
    unittest.main()
