import unittest
import uuid
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException, Response
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app.auth_identities import (
    SERVICE_PRINCIPAL_SCOPE_MATRIX,
    IdentityAuthError,
    IdentityScopeError,
    authenticate_service_token,
    create_user_session,
    digest_token,
    issue_service_token,
    revoke_service_token,
    revoke_user_session,
    rotate_service_token,
    scopes_for_principal_kind,
    validate_principal_scopes,
    validate_user_session,
)
from backend.app.models import AuthSession, Base, ServicePrincipal, ServicePrincipalToken, User
from backend.app.schemas import AuthLoginRequest
from backend.app.settings import load_settings
from backend.app.web_auth import SESSION_COOKIE_NAME, hash_password
from backend.app import main as backend_main


NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
FIXED_SECRET = "synthetic-fixed-secret-material-00000000000000000000"


class BackendAuthIdentityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add_user(self, suffix="1"):
        user = User(
            id=uuid.uuid4(),
            username=f"99800000000{suffix}",
            password_hash=f"pbkdf2-synthetic-{suffix}",
            role="operator",
            is_active=True,
            auth_version=1,
            created_at=NOW,
            updated_at=NOW,
        )
        self.db.add(user)
        self.db.flush()
        return user

    def add_principal(self, kind="desktop", scopes=None, suffix="1"):
        principal = ServicePrincipal(
            id=uuid.uuid4(),
            identifier=f"synthetic-{kind}-{suffix}",
            kind=kind,
            scopes=list(scopes if scopes is not None else scopes_for_principal_kind(kind)),
            is_active=True,
            expires_at=NOW + timedelta(days=30),
            created_at=NOW,
            updated_at=NOW,
        )
        self.db.add(principal)
        self.db.flush()
        return principal

    @staticmethod
    def secret_factory(_byte_count):
        return FIXED_SECRET

    def test_least_privilege_scope_matrix_has_no_implicit_admin(self):
        self.assertEqual(set(SERVICE_PRINCIPAL_SCOPE_MATRIX), {"desktop", "worker", "acceptance"})
        for kind, scopes in SERVICE_PRINCIPAL_SCOPE_MATRIX.items():
            with self.subTest(kind=kind):
                self.assertNotIn("admin:write", scopes)
                self.assertEqual(validate_principal_scopes(kind, scopes), tuple(sorted(scopes)))

        with self.assertRaises(ValueError):
            validate_principal_scopes("desktop", ["orders:read", "admin:write"])
        with self.assertRaises(ValueError):
            scopes_for_principal_kind("unknown")

    def test_service_token_is_digest_only_scoped_and_touched(self):
        principal = self.add_principal(scopes=["orders:read"])
        issued = issue_service_token(
            self.db,
            principal,
            expires_at=NOW + timedelta(hours=2),
            now=NOW,
            secret_factory=self.secret_factory,
        )

        self.assertRegex(issued.token, rf"^tks\.{issued.identifier.hex}\.[^.]+$")
        stored = self.db.get(ServicePrincipalToken, issued.identifier)
        self.assertEqual(stored.token_digest, digest_token(issued.token))
        self.assertNotEqual(stored.token_digest, issued.token)
        self.assertNotIn(FIXED_SECRET, repr(vars(stored)))
        self.assertNotIn(issued.token, repr(vars(stored)))

        verified = authenticate_service_token(
            self.db,
            issued.token,
            required_scope="orders:read",
            now=NOW + timedelta(seconds=61),
        )
        self.assertEqual(verified.principal_id, principal.id)
        self.assertEqual(verified.principal_identifier, principal.identifier)
        self.assertEqual(verified.scopes, frozenset({"orders:read"}))
        self.assertEqual(stored.last_used_at.replace(tzinfo=timezone.utc), NOW + timedelta(seconds=61))
        self.assertEqual(principal.last_used_at.replace(tzinfo=timezone.utc), NOW + timedelta(seconds=61))

        with self.assertRaises(IdentityScopeError):
            authenticate_service_token(self.db, issued.token, required_scope="scans:create", now=NOW)
        with self.assertRaises(IdentityAuthError):
            authenticate_service_token(self.db, issued.token[:-1] + "x", now=NOW)

        principal.scopes = ["admin:write"]
        self.db.flush()
        with self.assertRaises(IdentityAuthError):
            authenticate_service_token(self.db, issued.token, now=NOW)

    def test_service_token_disable_and_explicit_revocation_are_next_request(self):
        principal = self.add_principal(scopes=["orders:read"])
        issued = issue_service_token(
            self.db,
            principal,
            expires_at=NOW + timedelta(hours=1),
            now=NOW,
            secret_factory=self.secret_factory,
        )
        authenticate_service_token(self.db, issued.token, now=NOW)

        principal.is_active = False
        self.db.flush()
        with self.assertRaises(IdentityAuthError):
            authenticate_service_token(self.db, issued.token, now=NOW)

        principal.is_active = True
        revoke_service_token(self.db, issued.identifier, now=NOW + timedelta(seconds=1))
        with self.assertRaises(IdentityAuthError):
            authenticate_service_token(self.db, issued.token, now=NOW + timedelta(seconds=1))

    def test_rotation_caps_overlap_and_rejects_excess(self):
        principal = self.add_principal(scopes=["orders:read"])
        old = issue_service_token(
            self.db,
            principal,
            expires_at=NOW + timedelta(hours=4),
            now=NOW,
            secret_factory=lambda _count: FIXED_SECRET + "-old",
        )

        with self.assertRaises(ValueError):
            rotate_service_token(
                self.db,
                principal,
                expires_at=NOW + timedelta(hours=4),
                overlap_seconds=301,
                max_overlap_seconds=300,
                now=NOW,
                secret_factory=lambda _count: FIXED_SECRET + "-rejected",
            )

        new = rotate_service_token(
            self.db,
            principal,
            expires_at=NOW + timedelta(hours=4),
            overlap_seconds=30,
            max_overlap_seconds=300,
            now=NOW,
            secret_factory=lambda _count: FIXED_SECRET + "-new",
        )
        old_row = self.db.get(ServicePrincipalToken, old.identifier)
        self.assertEqual(old_row.expires_at.replace(tzinfo=timezone.utc), NOW + timedelta(seconds=30))
        self.assertEqual(old_row.replaced_by_token_id, new.identifier)
        authenticate_service_token(self.db, old.token, now=NOW + timedelta(seconds=29))
        with self.assertRaises(IdentityAuthError):
            authenticate_service_token(self.db, old.token, now=NOW + timedelta(seconds=30))
        authenticate_service_token(self.db, new.token, now=NOW + timedelta(seconds=30))

    def test_user_state_changes_invalidate_next_session_request(self):
        mutations = (
            lambda user: setattr(user, "is_active", False),
            lambda user: setattr(user, "password_hash", "pbkdf2-synthetic-changed"),
            lambda user: setattr(user, "role", "admin"),
            lambda user: setattr(user, "auth_version", user.auth_version + 1),
        )
        for index, mutate in enumerate(mutations, start=1):
            with self.subTest(mutation=index):
                user = self.add_user(str(index))
                issued = create_user_session(
                    self.db,
                    user,
                    expires_at=NOW + timedelta(hours=1),
                    now=NOW,
                    secret_factory=lambda _count, index=index: FIXED_SECRET + str(index),
                )
                before = validate_user_session(self.db, issued.token, now=NOW)
                self.assertEqual(before.user_id, user.id)
                mutate(user)
                self.db.flush()
                with self.assertRaises(IdentityAuthError):
                    validate_user_session(self.db, issued.token, now=NOW + timedelta(seconds=1))

    def test_logout_revokes_server_side_and_session_plaintext_is_absent(self):
        user = self.add_user()
        issued = create_user_session(
            self.db,
            user,
            expires_at=NOW + timedelta(hours=1),
            now=NOW,
            secret_factory=self.secret_factory,
        )
        stored = self.db.get(AuthSession, issued.identifier)
        self.assertEqual(stored.session_digest, digest_token(issued.token))
        self.assertNotIn(FIXED_SECRET, repr(vars(stored)))
        self.assertNotIn(issued.token, repr(vars(stored)))
        self.assertEqual(self.db.scalars(select(AuthSession)).all(), [stored])

        revoke_user_session(self.db, issued.token, now=NOW + timedelta(seconds=5))
        self.assertIsNotNone(stored.revoked_at)
        with self.assertRaises(IdentityAuthError):
            validate_user_session(self.db, issued.token, now=NOW + timedelta(seconds=5))

    def test_api_login_state_change_and_logout_are_db_backed(self):
        user = self.add_user("7")
        user.password_hash = hash_password("synthetic-password", salt="phase13-salt", iterations=1000)
        self.db.flush()
        app_settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
            "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
        })
        request = SimpleNamespace(
            client=SimpleNamespace(host="203.0.113.17"),
            cookies={},
            state=SimpleNamespace(),
        )
        response = Response()

        with mock.patch.object(backend_main, "settings", app_settings):
            login = backend_main.web_login(
                AuthLoginRequest(login=user.username, password="synthetic-password"),
                request,
                response,
                db=self.db,
            )
            self.assertTrue(login.authenticated)
            cookie = SimpleCookie()
            cookie.load(response.headers["set-cookie"])
            token = cookie[SESSION_COOKIE_NAME].value
            replay = SimpleNamespace(cookies={SESSION_COOKIE_NAME: token})
            self.assertEqual(backend_main.read_web_session(replay, db=self.db)["sub"], user.username)

            user.role = "admin"
            self.db.flush()
            with self.assertRaises(IdentityAuthError):
                backend_main.read_web_session(replay, db=self.db)

            user.role = "operator"
            self.db.flush()
            relogin_response = Response()
            backend_main.web_login(
                AuthLoginRequest(login=user.username, password="synthetic-password"),
                request,
                relogin_response,
                db=self.db,
            )
            cookie.load(relogin_response.headers["set-cookie"])
            fresh_token = cookie[SESSION_COOKIE_NAME].value
            logout_request = SimpleNamespace(cookies={SESSION_COOKIE_NAME: fresh_token})
            backend_main.web_logout(logout_request, Response(), db=self.db)
            with self.assertRaises(IdentityAuthError):
                backend_main.read_web_session(logout_request, db=self.db)

    def test_request_scope_matrix_and_legacy_shadow_are_enforced(self):
        principal = self.add_principal(scopes=["orders:read"])
        issued = issue_service_token(
            self.db,
            principal,
            expires_at=NOW + timedelta(days=1),
            now=NOW,
            secret_factory=self.secret_factory,
        )
        identity_settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
            "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
        })

        def request_for(path, method="GET"):
            return SimpleNamespace(
                url=SimpleNamespace(path=path),
                method=method,
                cookies={},
                state=SimpleNamespace(),
            )

        with mock.patch.object(backend_main, "settings", identity_settings):
            allowed = backend_main.require_service_token(
                request_for("/api/v1/orders/active"),
                f"Bearer {issued.token}",
                db=self.db,
            )
            self.assertEqual(allowed.source, "service-principal")
            self.assertNotIn("admin:write", allowed.permissions)
            with self.assertRaises(HTTPException) as denied:
                backend_main.require_service_token(
                    request_for("/api/v1/admin/table"),
                    f"Bearer {issued.token}",
                    db=self.db,
                )
            self.assertEqual(denied.exception.status_code, 403)

            worker = self.add_principal(kind="worker", scopes=["reports:read"], suffix="reconciliation")
            worker_token = issue_service_token(
                self.db,
                worker,
                expires_at=NOW + timedelta(days=1),
                now=NOW,
                secret_factory=lambda _count: FIXED_SECRET + "-worker",
            )
            with self.assertRaises(HTTPException) as reconciliation_denied:
                backend_main.require_service_token(
                    request_for("/api/v1/reports/reconciliation/day"),
                    f"Bearer {worker_token.token}",
                    db=self.db,
                )
            self.assertEqual(reconciliation_denied.exception.status_code, 403)

        legacy_token = "synthetic-legacy-token-value"
        shadow_settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_API_TOKEN": legacy_token,
            "TAKSKLAD_LEGACY_AUTH_MODE": "shadow",
            "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
        })
        with mock.patch.object(backend_main, "settings", shadow_settings), self.assertLogs(level="WARNING") as logs:
            with self.assertRaises(HTTPException) as shadow_denied:
                backend_main.read_auth_context(
                    request_for("/api/v1/orders/active"),
                    f"Bearer {legacy_token}",
                    db=self.db,
                )
        self.assertEqual(shadow_denied.exception.status_code, 401)
        self.assertNotIn(legacy_token, "\n".join(logs.output))

        expired_settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_API_TOKEN": legacy_token,
            "TAKSKLAD_LEGACY_AUTH_MODE": "enforce",
            "TAKSKLAD_LEGACY_AUTH_EXPIRES_AT": "2000-01-01T00:00:00+00:00",
            "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
        })
        with mock.patch.object(backend_main, "settings", expired_settings):
            with self.assertRaises(HTTPException) as expired_denied:
                backend_main.read_auth_context(
                    request_for("/api/v1/orders/active"),
                    f"Bearer {legacy_token}",
                    db=self.db,
                )
        self.assertEqual(expired_denied.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
