import unittest
import uuid
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException, Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.auth_identities import (
    ACCEPTANCE_CANARY_IDENTIFIER,
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
from backend.app.models import AuthSession, Base, Order, ServicePrincipal, ServicePrincipalToken, User
from backend.app.schemas import AuthLoginRequest
from backend.app.settings import load_settings
from backend.app.web_auth import (
    SESSION_COOKIE_NAME,
    WebAuthError,
    authenticate_web_user,
    hash_password,
    normalize_login,
)
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
        self.assertIn("returns:read", SERVICE_PRINCIPAL_SCOPE_MATRIX["acceptance"])
        self.assertNotIn("returns:write", SERVICE_PRINCIPAL_SCOPE_MATRIX["acceptance"])

    def test_legacy_cutoff_and_returns_scope_matrix_are_exact_and_fail_closed(self):
        cutoff = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        legacy_token = "synthetic-legacy-token-value"
        legacy_settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_API_TOKEN": legacy_token,
            "TAKSKLAD_LEGACY_AUTH_MODE": "enforce",
            "TAKSKLAD_LEGACY_AUTH_EXPIRES_AT": cutoff.isoformat(),
            "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
        })
        with mock.patch.object(backend_main, "settings", legacy_settings):
            self.assertTrue(backend_main.legacy_auth_window_active(cutoff - timedelta(microseconds=1)))
            self.assertFalse(backend_main.legacy_auth_window_active(cutoff))
            self.assertFalse(backend_main.legacy_auth_window_active(cutoff + timedelta(microseconds=1)))

        auth_now = datetime.now(timezone.utc)
        read_principal = self.add_principal(
            kind="acceptance",
            scopes=["returns:read"],
            suffix="returns-read",
        )
        read_principal.expires_at = auth_now + timedelta(days=30)
        read_token = issue_service_token(
            self.db,
            read_principal,
            expires_at=auth_now + timedelta(days=1),
            now=auth_now,
            secret_factory=lambda _count: FIXED_SECRET + "-returns-read",
        )
        wrong_principal = self.add_principal(scopes=["orders:read"], suffix="wrong-scope")
        wrong_principal.expires_at = auth_now + timedelta(days=30)
        wrong_token = issue_service_token(
            self.db,
            wrong_principal,
            expires_at=auth_now + timedelta(days=1),
            now=auth_now,
            secret_factory=lambda _count: FIXED_SECRET + "-wrong-scope",
        )
        self.db.commit()
        identity_settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
            "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
        })

        def request_for(path, method="GET", template=None):
            scope = {}
            if template:
                scope["route"] = SimpleNamespace(path=template)
            return SimpleNamespace(
                url=SimpleNamespace(path=path),
                method=method,
                cookies={},
                scope=scope,
                state=SimpleNamespace(),
            )

        with mock.patch.object(backend_main, "settings", identity_settings):
            for authorization in (None, "Bearer invalid-synthetic-token"):
                with self.subTest(authorization=bool(authorization)):
                    with self.assertRaises(HTTPException) as denied:
                        backend_main.read_auth_context(
                            request_for("/api/v1/returns"),
                            authorization,
                            db=self.db,
                        )
                    self.assertEqual(denied.exception.status_code, 401)

            for path in ("/api/v1/returns", "/api/v1/returns/lookup"):
                allowed = backend_main.require_service_token(
                    request_for(path),
                    f"Bearer {read_token.token}",
                    db=self.db,
                )
                self.assertEqual(allowed.permissions, ("returns:read",))

            with self.assertRaises(HTTPException) as wrong_scope:
                backend_main.require_service_token(
                    request_for("/api/v1/returns"),
                    f"Bearer {wrong_token.token}",
                    db=self.db,
                )
            self.assertEqual(wrong_scope.exception.status_code, 403)

            with self.assertRaises(HTTPException) as write_denied:
                backend_main.require_service_token(
                    request_for(
                        "/api/v1/returns/synthetic-order",
                        method="POST",
                        template="/api/v1/returns/{order_id}",
                    ),
                    f"Bearer {read_token.token}",
                    db=self.db,
                )
            self.assertEqual(write_denied.exception.status_code, 403)

    def test_auth_canary_http_endpoints_enforce_exact_identity_matrix_without_business_data(self):
        client = TestClient(backend_main.app)

        def context(source, role, permissions, *, login="synthetic"):
            return backend_main.AuthContext(
                login=login,
                role=role,
                permissions=tuple(sorted(permissions)),
                source=source,
            )

        acceptance_exact = context(
            "service-principal", "acceptance", {"returns:read"}, login=ACCEPTANCE_CANARY_IDENTIFIER
        )
        desktop_exact = context(
            "service-principal",
            "desktop",
            SERVICE_PRINCIPAL_SCOPE_MATRIX["desktop"],
        )

        def request(path, auth_context, identifier=None):
            def dependency():
                if isinstance(auth_context, Exception):
                    raise auth_context
                return auth_context

            backend_main.app.dependency_overrides[backend_main.require_service_token] = dependency
            backend_main.app.dependency_overrides[backend_main.get_db] = lambda: self.fail(
                "business database dependency must not run"
            )
            headers = {}
            if not isinstance(auth_context, Exception):
                headers["X-TakSklad-Canary-Identifier"] = identifier or auth_context.login
            return client.get(path, headers=headers)

        try:
            for label in ("missing", "invalid"):
                with self.subTest(label=label):
                    response = request(
                        "/api/v1/returns/auth-canary/acceptance",
                        HTTPException(status_code=401, detail="Invalid service token"),
                    )
                    self.assertEqual(response.status_code, 401)

            for path, allowed in (
                ("/api/v1/returns/auth-canary/acceptance", acceptance_exact),
                ("/api/v1/returns/auth-canary/desktop", desktop_exact),
            ):
                response = request(path, allowed)
                self.assertEqual(response.status_code, 204)
                self.assertEqual(response.content, b"")
                self.assertEqual(response.headers.get("cache-control"), "no-store")

            denied_cases = [
                ("acceptance-cross-kind", "/api/v1/returns/auth-canary/desktop", acceptance_exact),
                ("desktop-cross-kind", "/api/v1/returns/auth-canary/acceptance", desktop_exact),
                (
                    "acceptance-wrong-identifier",
                    "/api/v1/returns/auth-canary/acceptance",
                    context("service-principal", "acceptance", {"returns:read"}),
                ),
                (
                    "acceptance-extra-scopes",
                    "/api/v1/returns/auth-canary/acceptance",
                    context("service-principal", "acceptance", SERVICE_PRINCIPAL_SCOPE_MATRIX["acceptance"]),
                ),
                (
                    "legacy-during-grace",
                    "/api/v1/returns/auth-canary/desktop",
                    context("legacy-service-token", "desktop", SERVICE_PRINCIPAL_SCOPE_MATRIX["desktop"]),
                ),
                (
                    "web-session",
                    "/api/v1/returns/auth-canary/desktop",
                    context("web-session", "desktop", SERVICE_PRINCIPAL_SCOPE_MATRIX["desktop"]),
                ),
                (
                    "wrong-kind",
                    "/api/v1/returns/auth-canary/desktop",
                    context("service-principal", "worker", SERVICE_PRINCIPAL_SCOPE_MATRIX["desktop"]),
                ),
            ]
            for label, path, denied_context in denied_cases:
                with self.subTest(label=label):
                    expected_identifier = (
                        ACCEPTANCE_CANARY_IDENTIFIER
                        if path.endswith("/acceptance")
                        else getattr(denied_context, "login", "desktop.expected")
                    )
                    self.assertEqual(request(path, denied_context, expected_identifier).status_code, 403)

            for removed_scope in SERVICE_PRINCIPAL_SCOPE_MATRIX["desktop"]:
                with self.subTest(desktop_missing=removed_scope):
                    reduced = set(SERVICE_PRINCIPAL_SCOPE_MATRIX["desktop"]) - {removed_scope}
                    response = request(
                        "/api/v1/returns/auth-canary/desktop",
                        context("service-principal", "desktop", reduced),
                    )
                    self.assertEqual(response.status_code, 403)

            self.assertEqual(self.db.execute(select(Order)).scalars().all(), [])
        finally:
            backend_main.app.dependency_overrides.clear()

    def test_auth_canary_http_uses_real_scoped_token_and_session_authentication(self):
        engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        db = Session(engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)

        def issued(kind, scopes, suffix, *, identifier=None):
            principal = ServicePrincipal(
                id=uuid.uuid4(),
                identifier=identifier or f"http-{kind}-{suffix}",
                kind=kind,
                scopes=list(scopes),
                is_active=True,
                expires_at=now + timedelta(days=1),
                created_at=now,
                updated_at=now,
            )
            db.add(principal)
            db.flush()
            return issue_service_token(
                db,
                principal,
                expires_at=now + timedelta(hours=1),
                now=now,
                secret_factory=lambda _count: FIXED_SECRET + suffix,
            ).token

        acceptance = issued(
            "acceptance", {"returns:read"}, "acceptance", identifier=ACCEPTANCE_CANARY_IDENTIFIER
        )
        acceptance_wrong_identifier = issued(
            "acceptance", {"returns:read"}, "acceptance-wrong-identifier"
        )
        acceptance_full = issued(
            "acceptance",
            SERVICE_PRINCIPAL_SCOPE_MATRIX["acceptance"],
            "acceptance-full",
        )
        desktop_identifier = "desktop.http"
        desktop = issued(
            "desktop", SERVICE_PRINCIPAL_SCOPE_MATRIX["desktop"], "desktop",
            identifier=desktop_identifier,
        )
        desktop_no_write = issued(
            "desktop",
            set(SERVICE_PRINCIPAL_SCOPE_MATRIX["desktop"]) - {"returns:write"},
            "desktop-no-write",
        )
        user = User(
            id=uuid.uuid4(), username="998000000009", password_hash="synthetic", role="operator",
            is_active=True, auth_version=1, created_at=now, updated_at=now,
        )
        db.add(user)
        db.flush()
        web_token = create_user_session(
            db, user, expires_at=now + timedelta(hours=1), now=now,
            secret_factory=lambda _count: FIXED_SECRET + "web",
        ).token
        db.commit()

        def override_db():
            yield db

        backend_main.app.dependency_overrides[backend_main.get_db] = override_db
        client = TestClient(backend_main.app)
        settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
            "TAKSKLAD_API_TOKEN": "synthetic-legacy-http-token",
            "TAKSKLAD_LEGACY_AUTH_MODE": "enforce",
            "TAKSKLAD_LEGACY_AUTH_EXPIRES_AT": (now + timedelta(hours=1)).isoformat(),
            "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
        })
        try:
            with mock.patch.object(backend_main, "settings", settings):
                cases = (
                    ("/api/v1/returns/auth-canary/acceptance", acceptance, ACCEPTANCE_CANARY_IDENTIFIER, 204),
                    ("/api/v1/returns/auth-canary/desktop", desktop, desktop_identifier, 204),
                    ("/api/v1/returns/auth-canary/desktop", desktop, "desktop.beta", 403),
                    ("/api/v1/returns/auth-canary/desktop", acceptance, ACCEPTANCE_CANARY_IDENTIFIER, 403),
                    ("/api/v1/returns/auth-canary/acceptance", desktop, ACCEPTANCE_CANARY_IDENTIFIER, 403),
                    ("/api/v1/returns/auth-canary/acceptance", acceptance_full, ACCEPTANCE_CANARY_IDENTIFIER, 403),
                    ("/api/v1/returns/auth-canary/acceptance", acceptance_wrong_identifier, ACCEPTANCE_CANARY_IDENTIFIER, 403),
                    ("/api/v1/returns/auth-canary/desktop", desktop_no_write, "http-desktop-desktop-no-write", 403),
                )
                for path, token, identifier, expected in cases:
                    response = client.get(path, headers={
                        "Authorization": f"Bearer {token}",
                        "X-TakSklad-Canary-Identifier": identifier,
                    })
                    self.assertEqual(response.status_code, expected)
                    if expected == 204:
                        self.assertEqual(response.content, b"")
                        self.assertEqual(response.headers.get("cache-control"), "no-store")
                for authorization in (None, "Bearer invalid-synthetic"):
                    headers = {} if authorization is None else {"Authorization": authorization}
                    self.assertEqual(
                        client.get(
                            "/api/v1/returns/auth-canary/desktop",
                            headers={**headers, "X-TakSklad-Canary-Identifier": desktop_identifier},
                        ).status_code,
                        401,
                    )
                self.assertEqual(
                    client.get(
                        "/api/v1/returns/auth-canary/desktop",
                        headers={
                            "Authorization": "Bearer synthetic-legacy-http-token",
                            "X-TakSklad-Canary-Identifier": desktop_identifier,
                        },
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    client.get(
                        "/api/v1/returns/auth-canary/desktop",
                        headers={"X-TakSklad-Canary-Identifier": desktop_identifier},
                        cookies={SESSION_COOKIE_NAME: web_token},
                    ).status_code,
                    403,
                )
                self.assertEqual(db.execute(select(Order)).scalars().all(), [])
        finally:
            backend_main.app.dependency_overrides.clear()
            db.close()
            engine.dispose()

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
            "TAKSKLAD_WEB_COOKIE_SECURE": "false",
        })
        request = SimpleNamespace(
            client=SimpleNamespace(host="203.0.113.17"),
            cookies={},
            headers={"host": "testserver", "origin": "http://testserver"},
            url=SimpleNamespace(netloc="testserver", scheme="http"),
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
            logout_request = SimpleNamespace(
                cookies={SESSION_COOKIE_NAME: fresh_token},
                headers={
                    "host": "testserver",
                    "origin": "http://testserver",
                    "X-TakSklad-CSRF": backend_main.csrf_token_for_session(app_settings, fresh_token),
                },
                url=SimpleNamespace(netloc="testserver", scheme="http"),
            )
            backend_main.web_logout(logout_request, Response(), db=self.db)
            with self.assertRaises(IdentityAuthError):
                backend_main.read_web_session(logout_request, db=self.db)

    def test_phone_login_with_or_without_plus_resolves_the_same_unique_user(self):
        user = self.add_user("9")
        user.username = "+998000000009"
        user.password_hash = hash_password("synthetic-password", salt="phone-format-salt", iterations=1000)
        self.db.flush()
        app_settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
            "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
        })

        with_plus = authenticate_web_user(
            app_settings,
            "+998000000009",
            "synthetic-password",
            db=self.db,
        )
        without_plus = authenticate_web_user(
            app_settings,
            "998000000009",
            "synthetic-password",
            db=self.db,
        )

        self.assertEqual(normalize_login(with_plus.login), "998000000009")
        self.assertEqual(with_plus.user_id, user.id)
        self.assertEqual(without_plus.user_id, user.id)

    def test_disabled_legacy_web_auth_uses_overlapping_db_identity(self):
        user = self.add_user("3")
        shared_password_hash = hash_password(
            "synthetic-password",
            salt="disabled-overlap-salt",
            iterations=1000,
        )
        user.password_hash = shared_password_hash
        self.db.flush()
        app_settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
            "TAKSKLAD_LEGACY_AUTH_MODE": "disabled",
            "TAKSKLAD_WEB_LOGIN": user.username,
            "TAKSKLAD_WEB_PASSWORD_HASH": shared_password_hash,
            "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
        })

        identity = authenticate_web_user(
            app_settings,
            user.username,
            "synthetic-password",
            db=self.db,
        )

        self.assertEqual(identity.login, user.username)
        self.assertEqual(identity.role, "operator")
        self.assertEqual(identity.user_id, user.id)
        self.assertEqual(identity.auth_version, user.auth_version)

    def test_enforced_legacy_web_auth_uses_overlapping_db_identity_first(self):
        user = self.add_user("4")
        shared_password_hash = hash_password(
            "synthetic-password",
            salt="enforce-overlap-salt",
            iterations=1000,
        )
        user.password_hash = shared_password_hash
        self.db.flush()
        app_settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
            "TAKSKLAD_LEGACY_AUTH_MODE": "enforce",
            "TAKSKLAD_WEB_LOGIN": user.username,
            "TAKSKLAD_WEB_PASSWORD_HASH": shared_password_hash,
            "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
        })

        identity = authenticate_web_user(
            app_settings,
            user.username,
            "synthetic-password",
            db=self.db,
        )

        self.assertEqual(identity.login, user.username)
        self.assertEqual(identity.role, "operator")
        self.assertEqual(identity.user_id, user.id)
        self.assertEqual(identity.auth_version, user.auth_version)

    def test_enforced_legacy_web_auth_falls_back_without_db_match(self):
        legacy_login = "998000000007"
        legacy_password_hash = hash_password(
            "synthetic-password",
            salt="enforce-fallback-salt",
            iterations=1000,
        )
        app_settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
            "TAKSKLAD_LEGACY_AUTH_MODE": "enforce",
            "TAKSKLAD_WEB_LOGIN": legacy_login,
            "TAKSKLAD_WEB_PASSWORD_HASH": legacy_password_hash,
            "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
        })

        identity = authenticate_web_user(
            app_settings,
            legacy_login,
            "synthetic-password",
            db=self.db,
        )

        self.assertEqual(identity.login, legacy_login)
        self.assertEqual(identity.role, "admin")
        self.assertIsNone(identity.user_id)

    def test_expired_enforced_legacy_window_issues_overlapping_db_session(self):
        user = self.add_user("2")
        shared_password_hash = hash_password(
            "synthetic-password",
            salt="expired-enforce-overlap-salt",
            iterations=1000,
        )
        user.password_hash = shared_password_hash
        self.db.flush()
        app_settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
            "TAKSKLAD_LEGACY_AUTH_MODE": "enforce",
            "TAKSKLAD_LEGACY_AUTH_EXPIRES_AT": "2000-01-01T00:00:00+00:00",
            "TAKSKLAD_WEB_LOGIN": user.username,
            "TAKSKLAD_WEB_PASSWORD_HASH": shared_password_hash,
            "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
            "TAKSKLAD_WEB_COOKIE_SECURE": "false",
        })
        request = SimpleNamespace(
            client=SimpleNamespace(host="203.0.113.19"),
            cookies={},
            headers={"host": "testserver", "origin": "http://testserver"},
            url=SimpleNamespace(netloc="testserver", scheme="http"),
            state=SimpleNamespace(),
        )
        response = Response()

        with mock.patch.object(backend_main, "settings", app_settings):
            self.assertFalse(backend_main.legacy_auth_window_active())
            login = backend_main.web_login(
                AuthLoginRequest(login=user.username, password="synthetic-password"),
                request,
                response,
                db=self.db,
            )
            cookie = SimpleCookie()
            cookie.load(response.headers["set-cookie"])
            token = cookie[SESSION_COOKIE_NAME].value
            session = backend_main.read_web_session(
                SimpleNamespace(cookies={SESSION_COOKIE_NAME: token}),
                db=self.db,
            )

        self.assertTrue(login.authenticated)
        self.assertEqual(login.login, user.username)
        self.assertEqual(login.role, "operator")
        self.assertTrue(token.startswith("tks."))
        self.assertEqual(session["uid"], str(user.id))
        self.assertEqual(session["role"], "operator")

    def test_duplicate_plus_variants_are_ambiguous_and_fail_closed(self):
        first = self.add_user("5")
        first.username = "+998000000005"
        first.password_hash = hash_password("first-password", salt="first-phone-salt", iterations=1000)
        second = self.add_user("6")
        second.username = "998000000005"
        second.password_hash = hash_password("second-password", salt="second-phone-salt", iterations=1000)
        self.db.flush()
        app_settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
            "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
        })

        for candidate_password in ("first-password", "second-password"):
            with self.subTest(candidate_password=candidate_password):
                with self.assertRaises(WebAuthError):
                    authenticate_web_user(
                        app_settings,
                        "998000000005",
                        candidate_password,
                        db=self.db,
                    )

    def test_verified_credentials_do_not_poison_limiter_when_session_issuance_fails(self):
        user = self.add_user("8")
        user.password_hash = hash_password("synthetic-password", salt="phase13-salt", iterations=1000)
        self.db.flush()
        app_settings = load_settings({
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
            "TAKSKLAD_WEB_SESSION_SECRET": "independent-synthetic-session-secret",
            "TAKSKLAD_WEB_COOKIE_SECURE": "false",
        })
        request = SimpleNamespace(
            client=SimpleNamespace(host="203.0.113.18"),
            cookies={},
            headers={"host": "testserver", "origin": "http://testserver"},
            url=SimpleNamespace(netloc="testserver", scheme="http"),
            state=SimpleNamespace(),
        )

        class RecordingLimiter:
            failure_count = 0

            def ensure_not_locked(self, _key):
                return None

            def register_failure(self, *_args, **_kwargs):
                self.failure_count += 1

            def clear(self, _key):
                return None

        limiter = RecordingLimiter()
        with (
            mock.patch.object(backend_main, "settings", app_settings),
            mock.patch.object(backend_main, "login_limiter", limiter),
            mock.patch.object(
                backend_main,
                "create_user_session",
                side_effect=IdentityAuthError("synthetic session store failure"),
            ),
        ):
            with self.assertRaises(HTTPException) as captured:
                backend_main.web_login(
                    AuthLoginRequest(login=user.username, password="synthetic-password"),
                    request,
                    Response(),
                    db=self.db,
                )

        self.assertEqual(captured.exception.status_code, 503)
        self.assertEqual(limiter.failure_count, 0)

    def test_request_scope_matrix_and_legacy_shadow_are_enforced(self):
        auth_now = datetime.now(timezone.utc)
        principal = self.add_principal(scopes=["orders:read"])
        principal.expires_at = auth_now + timedelta(days=30)
        issued = issue_service_token(
            self.db,
            principal,
            expires_at=auth_now + timedelta(days=1),
            now=auth_now,
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
                scope={},
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
            worker.expires_at = auth_now + timedelta(days=30)
            worker_token = issue_service_token(
                self.db,
                worker,
                expires_at=auth_now + timedelta(days=1),
                now=auth_now,
                secret_factory=lambda _count: FIXED_SECRET + "-worker",
            )
            reconciliation_preview = backend_main.require_service_token(
                request_for("/api/v1/reports/reconciliation/day"),
                f"Bearer {worker_token.token}",
                db=self.db,
            )
            self.assertEqual(reconciliation_preview.source, "service-principal")
            with self.assertRaises(HTTPException) as reconciliation_denied:
                backend_main.require_service_token(
                    request_for("/api/v1/reports/reconciliation/day", method="POST"),
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
