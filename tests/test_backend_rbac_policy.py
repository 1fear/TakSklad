import ast
import inspect
import textwrap
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from backend.app import main as backend_main
from backend.app.access_policy import (
    ALL_PERMISSIONS,
    AUTH_PROTECTED,
    PERMISSION_ADMIN_READ,
    PERMISSION_ADMIN_WRITE,
    ROLE_ADMIN,
    ROLE_DENIED,
    ROLE_LOGISTICS_SLOTS,
    ROLE_OPERATOR,
    ROLE_PERMISSION_MATRIX,
    ROUTE_POLICIES,
    SAFE_METHODS,
)
from backend.app.csrf import browser_origin_matches, csrf_token_for_session, csrf_token_matches
from backend.app.main import app
from backend.app.settings import load_settings
from backend.app.web_auth import normalize_role, role_permissions


class BackendRbacPolicyTests(unittest.TestCase):
    def test_every_versioned_route_has_exactly_one_policy(self):
        actual = {
            (method, route.path)
            for route in app.routes
            if getattr(route, "path", "").startswith("/api/v1")
            for method in (getattr(route, "methods", set()) or set())
            if method != "OPTIONS"
        }

        self.assertEqual(actual, set(ROUTE_POLICIES))
        self.assertEqual(len(actual), 63)
        self.assertIn(("POST", "/api/v1/auth/desktop-bootstrap"), actual)
        self.assertIn(("GET", "/api/v1/returns/auth-canary/acceptance"), actual)
        self.assertIn(("GET", "/api/v1/returns/auth-canary/desktop"), actual)

    def test_every_protected_route_has_complete_web_and_service_policy(self):
        protected = [policy for policy in ROUTE_POLICIES.values() if policy.authentication == AUTH_PROTECTED]

        self.assertEqual(len(protected), 57)
        self.assertTrue(all(policy.web_permission in ALL_PERMISSIONS for policy in protected))
        self.assertTrue(all(bool(policy.service_scope) for policy in protected))

    def test_get_and_head_policies_are_declared_non_mutating(self):
        unsafe_safe_routes = [
            (method, path)
            for (method, path), policy in ROUTE_POLICIES.items()
            if method in SAFE_METHODS and policy.mutates
        ]

        self.assertEqual(unsafe_safe_routes, [])

    def test_sensitive_admin_surfaces_are_not_granted_to_restricted_roles(self):
        restricted_permissions = ROLE_PERMISSION_MATRIX[ROLE_OPERATOR] | ROLE_PERMISSION_MATRIX[ROLE_LOGISTICS_SLOTS]
        sensitive_policies = [policy for policy in ROUTE_POLICIES.values() if policy.sensitive]

        self.assertTrue(sensitive_policies)
        for policy in sensitive_policies:
            self.assertNotIn(policy.web_permission, restricted_permissions)
            self.assertIn(policy.web_permission, {PERMISSION_ADMIN_READ, PERMISSION_ADMIN_WRITE, "diagnostics:read"})

    def test_role_matrix_is_complete_and_unknown_roles_fail_closed(self):
        self.assertEqual(set(ROLE_PERMISSION_MATRIX), {ROLE_ADMIN, ROLE_OPERATOR, ROLE_LOGISTICS_SLOTS})
        self.assertEqual(set(role_permissions(ROLE_ADMIN)), set(ALL_PERMISSIONS))
        self.assertIn(PERMISSION_ADMIN_WRITE, role_permissions(ROLE_ADMIN))
        self.assertNotIn(PERMISSION_ADMIN_READ, role_permissions(ROLE_OPERATOR))
        self.assertNotIn(PERMISSION_ADMIN_READ, role_permissions(ROLE_LOGISTICS_SLOTS))
        self.assertEqual(normalize_role("unexpected-superuser"), ROLE_DENIED)
        self.assertEqual(role_permissions("unexpected-superuser"), ())

    def test_csrf_token_is_session_bound_and_origin_is_exact(self):
        settings = load_settings({
            "TAKSKLAD_ENV": "local",
            "TAKSKLAD_WEB_SESSION_SECRET": "synthetic-session-secret-with-32-bytes-minimum",
            "TAKSKLAD_WEB_COOKIE_SECURE": "false",
        })
        first = csrf_token_for_session(settings, "synthetic-session-one")
        second = csrf_token_for_session(settings, "synthetic-session-two")
        request = SimpleNamespace(
            headers={"host": "app.example.test", "origin": "http://app.example.test"},
            url=SimpleNamespace(netloc="app.example.test", scheme="http"),
        )

        self.assertNotEqual(first, second)
        self.assertTrue(csrf_token_matches(settings, "synthetic-session-one", first))
        self.assertFalse(csrf_token_matches(settings, "synthetic-session-one", second))
        self.assertTrue(browser_origin_matches(request, settings))
        request.headers["origin"] = "https://cross-origin.example.test"
        self.assertFalse(browser_origin_matches(request, settings))
        self.assertNotIn(settings.web_session_secret, first)

    def test_enforcement_executes_complete_role_and_service_matrix(self):
        protected = [
            (method, path, policy)
            for (method, path), policy in ROUTE_POLICIES.items()
            if policy.authentication == AUTH_PROTECTED
        ]
        db = SimpleNamespace(info={}, rollback=lambda: None)
        decisions = 0

        def request_for(method, path):
            return SimpleNamespace(
                method=method,
                url=SimpleNamespace(path=path),
                scope={"route": SimpleNamespace(path=path)},
                state=SimpleNamespace(),
                cookies={},
                headers={},
            )

        with mock.patch.object(backend_main, "require_browser_request_security"):
            for role, permissions in ROLE_PERMISSION_MATRIX.items():
                context = backend_main.AuthContext(
                    login=f"synthetic-{role}",
                    role=role,
                    permissions=tuple(permissions),
                    source="web-session",
                    user_id="00000000-0000-0000-0000-000000001400",
                )
                with mock.patch.object(backend_main, "read_auth_context", return_value=context):
                    for method, path, policy in protected:
                        if policy.web_permission in permissions:
                            result = backend_main.require_service_token(request_for(method, path), db=db)
                            self.assertEqual(result.role, role)
                        else:
                            with self.assertRaises(HTTPException) as denied:
                                backend_main.require_service_token(request_for(method, path), db=db)
                            self.assertEqual(denied.exception.status_code, 403)
                        decisions += 1

            for method, path, policy in protected:
                allowed_service = backend_main.AuthContext(
                    login="synthetic-service",
                    role="worker",
                    permissions=(policy.service_scope,),
                    source="service-principal",
                    principal_id="00000000-0000-0000-0000-000000001401",
                )
                denied_service = backend_main.AuthContext(
                    login="synthetic-service",
                    role="worker",
                    permissions=(),
                    source="service-principal",
                    principal_id="00000000-0000-0000-0000-000000001401",
                )
                with mock.patch.object(backend_main, "read_auth_context", return_value=allowed_service):
                    self.assertEqual(
                        backend_main.require_service_token(request_for(method, path), db=db).source,
                        "service-principal",
                    )
                with mock.patch.object(backend_main, "read_auth_context", return_value=denied_service):
                    with self.assertRaises(HTTPException) as denied:
                        backend_main.require_service_token(request_for(method, path), db=db)
                    self.assertEqual(denied.exception.status_code, 403)
                decisions += 2

                with mock.patch.object(
                    backend_main,
                    "read_auth_context",
                    side_effect=HTTPException(status_code=401, detail="Not authenticated"),
                ):
                    with self.assertRaises(HTTPException) as anonymous:
                        backend_main.require_service_token(request_for(method, path), db=db)
                    self.assertEqual(anonymous.exception.status_code, 401)
                decisions += 1

        self.assertEqual(decisions, 342)

    def test_day_report_accepts_desktop_and_legacy_report_reader_scopes(self):
        request = SimpleNamespace(
            method="GET",
            url=SimpleNamespace(path="/api/v1/reports/day"),
            scope={"route": SimpleNamespace(path="/api/v1/reports/day")},
            state=SimpleNamespace(),
            cookies={},
            headers={},
        )
        db = SimpleNamespace(info={}, rollback=lambda: None)
        for scope in ("orders:read", "reports:read"):
            context = backend_main.AuthContext(
                login=f"synthetic-{scope}",
                role="worker",
                permissions=(scope,),
                source="service-principal",
                principal_id="00000000-0000-0000-0000-000000001402",
            )
            with self.subTest(scope=scope), mock.patch.object(
                backend_main, "read_auth_context", return_value=context
            ):
                self.assertEqual(
                    backend_main.require_service_token(request, db=db).source,
                    "service-principal",
                )

    def test_all_get_handlers_static_mutation_scan_is_zero(self):
        banned_calls = {
            "commit",
            "flush",
            "add",
            "delete",
            "run_daily_reconciliation",
            "process_pending_google_sheets_exports",
            "sync_google_sheet_to_backend",
            "update_orders_from_skladbot",
            "create_import_in_db",
            "create_scan_in_db",
            "undo_scan_in_db",
            "complete_order_in_db",
            "mark_order_returned_in_db",
            "retry_event_queue_event_in_db",
            "update_incident_status_in_db",
            "update_client_point_timeslot_in_db",
            "set_logistics_calendar_day_in_db",
            "rebuild_skladbot_dry_run",
        }
        scanned = 0
        findings = []
        for route in app.routes:
            if not route.path.startswith("/api/v1") or "GET" not in (route.methods or set()):
                continue
            scanned += 1
            tree = ast.parse(textwrap.dedent(inspect.getsource(route.endpoint)))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "attr", "") or getattr(node.func, "id", "")
                if name in banned_calls:
                    findings.append(f"GET {route.path}: {name}")

        self.assertGreaterEqual(scanned, 30)
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
