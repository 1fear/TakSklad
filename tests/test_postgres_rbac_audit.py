import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app import main as backend_main
from backend.app.access_policy import ROLE_PERMISSION_MATRIX, ROUTE_POLICIES
from backend.app.auth_identities import issue_service_token
from backend.app.db import get_db
from backend.app.models import (
    AuditLog,
    AuthSession,
    Incident,
    PendingEvent,
    ServicePrincipal,
    ServicePrincipalToken,
    User,
)
from backend.app.settings import load_settings
from backend.app.web_auth import hash_password
from tests.postgres_support import create_database, drop_database, run_alembic


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresRbacAuditTests(unittest.TestCase):
    database = "taksklad_rbac_audit"

    @classmethod
    def setUpClass(cls):
        cls.url = create_database(cls.database)
        run_alembic(cls.url, "upgrade", "head")
        cls.engine = create_engine(cls.url, pool_pre_ping=True)
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)
        cls.settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "DATABASE_URL": cls.url,
            "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
            "TAKSKLAD_LEGACY_AUTH_MODE": "disabled",
            "TAKSKLAD_WEB_SESSION_SECRET": "synthetic-rbac-session-secret-with-32-bytes",
            "TAKSKLAD_WEB_COOKIE_SECURE": "false",
        })

        def override_db():
            db = cls.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        backend_main.app.dependency_overrides[get_db] = override_db
        cls.settings_patch = mock.patch("backend.app.main.settings", cls.settings)
        cls.settings_patch.start()

    @classmethod
    def tearDownClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        backend_main.app.dependency_overrides.pop(get_db, None)
        cls.settings_patch.stop()
        cls.engine.dispose()
        drop_database(cls.database)

    def setUp(self):
        self.client = TestClient(backend_main.app, base_url="http://testserver")

    def add_user(self, suffix: str, role: str):
        with self.SessionLocal() as db:
            user = User(
                username=f"99800014{suffix:0>4}",
                password_hash=hash_password("synthetic-password", salt=f"rbac-{suffix}", iterations=1000),
                role=role,
                is_active=True,
            )
            db.add(user)
            db.commit()
            return str(user.id), user.username

    def login(self, username: str):
        response = self.client.post(
            "/api/v1/auth/login",
            json={"login": username, "password": "synthetic-password"},
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def issue_principal(self, suffix: str, kind: str, scopes: list[str]):
        with self.SessionLocal() as db:
            principal = ServicePrincipal(
                identifier=f"{kind}-phase14-{suffix}",
                kind=kind,
                scopes=scopes,
                is_active=True,
            )
            db.add(principal)
            db.flush()
            issued = issue_service_token(
                db,
                principal,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                secret_factory=lambda _count: f"synthetic-phase14-{suffix}-secret-material",
            )
            principal_id = str(principal.id)
            db.commit()
        return principal_id, issued.token

    def test_complete_catalog_role_matrix_and_representative_http_statuses(self):
        protected = [policy for policy in ROUTE_POLICIES.values() if policy.web_permission]
        expected = {
            role: [200 if policy.web_permission in permissions else 403 for policy in protected]
            for role, permissions in ROLE_PERMISSION_MATRIX.items()
        }

        self.assertEqual(len(protected), 51)
        self.assertEqual({role: len(statuses) for role, statuses in expected.items()}, {
            "admin": 51,
            "operator": 51,
            "logistics_slots": 51,
        })
        self.assertTrue(all(status == 200 for status in expected["admin"]))
        self.assertIn(403, expected["operator"])
        self.assertIn(403, expected["logistics_slots"])

        unauthenticated = self.client.get("/api/v1/orders/active")
        self.assertEqual(unauthenticated.status_code, 401)

        _admin_id, admin_login = self.add_user("01", "admin")
        self.login(admin_login)
        self.assertEqual(self.client.get("/api/v1/admin/events").status_code, 200)
        self.client.cookies.clear()

        _operator_id, operator_login = self.add_user("02", "operator")
        self.login(operator_login)
        self.assertEqual(self.client.get("/api/v1/orders/active").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/admin/events").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/admin/client-points").status_code, 403)
        self.client.cookies.clear()

        _logistics_id, logistics_login = self.add_user("03", "logistics_slots")
        self.login(logistics_login)
        self.assertEqual(self.client.get("/api/v1/admin/client-points").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/orders/active").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/admin/events").status_code, 403)

    def test_cookie_csrf_origin_matrix_and_actor_spoof_binding(self):
        user_id, username = self.add_user("11", "admin")
        session = self.login(username)
        csrf = session["csrf_token"]
        payload = {
            "source": "synthetic_phase14",
            "severity": "warning",
            "status": "open",
            "title": "Synthetic CSRF matrix incident",
            "entity_type": "external",
            "external_ref": "synthetic-phase14-csrf",
        }
        with self.SessionLocal() as db:
            before = db.query(Incident).count()

        missing = self.client.post("/api/v1/admin/incidents", json=payload, headers={"Origin": "http://testserver"})
        bad = self.client.post(
            "/api/v1/admin/incidents",
            json=payload,
            headers={"Origin": "http://testserver", "X-TakSklad-CSRF": "invalid-proof"},
        )
        cross = self.client.post(
            "/api/v1/admin/incidents",
            json=payload,
            headers={"Origin": "https://cross-origin.example.test", "X-TakSklad-CSRF": csrf},
        )
        invalid_bearer = self.client.post(
            "/api/v1/admin/incidents",
            json=payload,
            headers={
                "Origin": "http://testserver",
                "X-TakSklad-CSRF": csrf,
                "Authorization": "Bearer invalid-service-credential",
            },
        )

        self.assertEqual([missing.status_code, bad.status_code, cross.status_code], [403, 403, 403])
        self.assertEqual(invalid_bearer.status_code, 401)
        rejected_text = missing.text + bad.text + cross.text + invalid_bearer.text
        self.assertNotIn(csrf, rejected_text)
        self.assertNotIn("synthetic-rbac-session-secret", rejected_text)
        with self.SessionLocal() as db:
            self.assertEqual(db.query(Incident).count(), before)

        created = self.client.post(
            "/api/v1/admin/incidents",
            json=payload,
            headers={"Referer": "http://testserver/admin", "X-TakSklad-CSRF": csrf},
        )
        self.assertEqual(created.status_code, 201, created.text)
        updated = self.client.post(
            f"/api/v1/admin/incidents/{created.json()['id']}/status",
            json={
                "status": "manual_review",
                "actor": "spoofed-superuser",
                "source": "synthetic-browser",
                "reason": "Synthetic actor binding proof",
            },
            headers={"Origin": "http://testserver", "X-TakSklad-CSRF": csrf},
        )
        self.assertEqual(updated.status_code, 200, updated.text)

        with self.SessionLocal() as db:
            audit = db.execute(
                select(AuditLog).where(AuditLog.action == "incident_status_changed")
                .where(AuditLog.entity_id == created.json()["id"])
            ).scalar_one()
            self.assertEqual(str(audit.actor_user_id), user_id)
            self.assertIsNone(audit.actor_service_principal_id)
            self.assertEqual(audit.actor_subject, f"user:{user_id}")
            self.assertEqual(audit.payload["claimed_actor"], "spoofed-superuser")
            self.assertEqual(audit.payload["authenticated_subject"], f"user:{user_id}")

    def test_scoped_bearer_bypasses_browser_csrf_and_fills_service_actor(self):
        principal_id, token = self.issue_principal("service", "desktop", ["imports:create", "sync:run"])

        response = self.client.post(
            "/api/v1/imports",
            json={"source": "excel", "filename": "synthetic.xlsx", "rows": []},
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 201, response.text)
        import_id = response.json()["id"]
        sync_response = self.client.post(
            "/api/v1/sync/sources?skladbot=0",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(sync_response.status_code, 200, sync_response.text)
        with self.SessionLocal() as db:
            audit = db.execute(
                select(AuditLog).where(AuditLog.action == "orders_imported")
                .where(AuditLog.entity_id == import_id)
            ).scalar_one()
            self.assertIsNone(audit.actor_user_id)
            self.assertEqual(str(audit.actor_service_principal_id), principal_id)
            self.assertEqual(audit.actor_subject, "service:desktop-phase14-service")
            sync_audit = db.execute(
                select(AuditLog).where(AuditLog.action == "sync_sources_requested")
                .where(AuditLog.actor_service_principal_id == uuid.UUID(principal_id))
            ).scalar_one()
            self.assertEqual(sync_audit.actor_subject, "service:desktop-phase14-service")

    def test_get_requests_leave_auth_and_domain_state_unchanged(self):
        _user_id, username = self.add_user("21", "admin")
        session = self.login(username)
        principal_id, token = self.issue_principal("get", "desktop", ["orders:read"])
        with self.SessionLocal() as db:
            auth_session = db.execute(
                select(AuthSession).where(AuthSession.subject == username).order_by(AuthSession.created_at.desc())
            ).scalars().first()
            auth_session.last_used_at = datetime.now(timezone.utc) - timedelta(hours=2)
            principal = db.get(ServicePrincipal, uuid.UUID(principal_id))
            principal.last_used_at = datetime.now(timezone.utc) - timedelta(hours=2)
            db.commit()
            auth_session_id = auth_session.id
            principal_uuid = principal.id
            before = self._state_fingerprint(db, auth_session_id, principal_uuid)

        admin_get = self.client.get("/api/v1/admin/events")
        bearer_get = self.client.get(
            "/api/v1/orders/active",
            headers={"Authorization": f"Bearer {token}"},
        )
        with mock.patch("backend.app.reconciliation_service.load_google_sheet_records", return_value=[]):
            preview = self.client.get("/api/v1/reports/reconciliation/day?report_date=2026-06-10")

        self.assertEqual(admin_get.status_code, 200)
        self.assertEqual(bearer_get.status_code, 200)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["mode"], "preview")
        with self.SessionLocal() as db:
            after = self._state_fingerprint(db, auth_session_id, principal_uuid)
        self.assertEqual(after, before)

    def test_logout_requires_csrf_and_revokes_server_session(self):
        _user_id, username = self.add_user("31", "admin")
        session = self.login(username)
        csrf = session["csrf_token"]

        denied = self.client.post("/api/v1/auth/logout", headers={"Origin": "http://testserver"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.client.get("/api/v1/auth/check").status_code, 204)
        allowed = self.client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "http://testserver", "X-TakSklad-CSRF": csrf},
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/auth/check").status_code, 401)

    @staticmethod
    def _state_fingerprint(db, session_id, principal_id):
        auth_session = db.get(AuthSession, session_id)
        principal = db.get(ServicePrincipal, principal_id)
        service_token = db.execute(
            select(ServicePrincipalToken).where(ServicePrincipalToken.principal_id == principal_id)
        ).scalars().first()
        return {
            "session_last_used": auth_session.last_used_at,
            "principal_last_used": principal.last_used_at,
            "token_last_used": service_token.last_used_at,
            "audits": db.query(AuditLog).count(),
            "incidents": db.query(Incident).count(),
            "events": db.query(PendingEvent).count(),
        }


if __name__ == "__main__":
    unittest.main()
