"""Single fail-closed authorization catalog for every versioned API route."""

from __future__ import annotations

from dataclasses import dataclass


ROLE_ADMIN = "admin"
ROLE_LOGISTICS_SLOTS = "logistics_slots"
ROLE_OPERATOR = "operator"
ROLE_DENIED = "denied"

PERMISSION_WAREHOUSE_READ = "warehouse:read"
PERMISSION_WAREHOUSE_WRITE = "warehouse:write"
PERMISSION_ADMIN_READ = "admin:read"
PERMISSION_ADMIN_WRITE = "admin:write"
PERMISSION_IMPORT_READ = "imports:read"
PERMISSION_IMPORT_WRITE = "imports:write"
PERMISSION_REPORT_READ = "reports:read"
PERMISSION_CLIENT_POINTS_READ = "client_points:read"
PERMISSION_CLIENT_POINTS_WRITE = "client_points:write"
PERMISSION_LOGISTICS_READ = "logistics:read"
PERMISSION_DIAGNOSTICS_READ = "diagnostics:read"

ALL_PERMISSIONS = frozenset({
    PERMISSION_WAREHOUSE_READ,
    PERMISSION_WAREHOUSE_WRITE,
    PERMISSION_ADMIN_READ,
    PERMISSION_ADMIN_WRITE,
    PERMISSION_IMPORT_READ,
    PERMISSION_IMPORT_WRITE,
    PERMISSION_REPORT_READ,
    PERMISSION_CLIENT_POINTS_READ,
    PERMISSION_CLIENT_POINTS_WRITE,
    PERMISSION_LOGISTICS_READ,
    PERMISSION_DIAGNOSTICS_READ,
})

ROLE_PERMISSION_MATRIX = {
    ROLE_ADMIN: ALL_PERMISSIONS,
    ROLE_OPERATOR: frozenset({
        PERMISSION_WAREHOUSE_READ,
        PERMISSION_WAREHOUSE_WRITE,
        PERMISSION_IMPORT_READ,
        PERMISSION_IMPORT_WRITE,
        PERMISSION_REPORT_READ,
        PERMISSION_LOGISTICS_READ,
    }),
    ROLE_LOGISTICS_SLOTS: frozenset({
        PERMISSION_CLIENT_POINTS_READ,
        PERMISSION_CLIENT_POINTS_WRITE,
        PERMISSION_LOGISTICS_READ,
    }),
}

AUTH_PUBLIC = "public"
AUTH_SESSION = "session"
AUTH_PROTECTED = "protected"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class RoutePolicy:
    authentication: str
    web_permission: str | None = None
    service_scope: str | None = None
    service_scope_alternatives: frozenset[str] = frozenset()
    mutates: bool = False
    sensitive: bool = False


def _public() -> RoutePolicy:
    return RoutePolicy(authentication=AUTH_PUBLIC)


def _session(*, mutates: bool = False) -> RoutePolicy:
    return RoutePolicy(authentication=AUTH_SESSION, mutates=mutates)


def _protected(
    permission: str,
    scope: str,
    *,
    service_scope_alternatives: frozenset[str] = frozenset(),
    mutates: bool = False,
    sensitive: bool = False,
) -> RoutePolicy:
    return RoutePolicy(
        authentication=AUTH_PROTECTED,
        web_permission=permission,
        service_scope=scope,
        service_scope_alternatives=service_scope_alternatives,
        mutates=mutates,
        sensitive=sensitive,
    )


ROUTE_POLICIES: dict[tuple[str, str], RoutePolicy] = {
    ("POST", "/api/v1/auth/login"): _public(),
    ("POST", "/api/v1/auth/logout"): _session(mutates=True),
    ("GET", "/api/v1/auth/session"): _session(),
    ("GET", "/api/v1/auth/check"): _session(),
    ("GET", "/api/v1/orders/active"): _protected(PERMISSION_WAREHOUSE_READ, "orders:read"),
    ("GET", "/api/v1/admin/table"): _protected(PERMISSION_ADMIN_READ, "admin:read", sensitive=True),
    ("GET", "/api/v1/admin/dashboard/day-summary"): _protected(PERMISSION_ADMIN_READ, "admin:read"),
    ("GET", "/api/v1/admin/metrics"): _protected(PERMISSION_DIAGNOSTICS_READ, "diagnostics:read", sensitive=True),
    ("GET", "/api/v1/admin/client-points"): _protected(PERMISSION_CLIENT_POINTS_READ, "client_points:read"),
    ("GET", "/api/v1/admin/client-points/order-summary"): _protected(PERMISSION_CLIENT_POINTS_READ, "client_points:read"),
    ("GET", "/api/v1/admin/logistics-calendar"): _protected(PERMISSION_CLIENT_POINTS_READ, "client_points:read"),
    ("POST", "/api/v1/admin/logistics-calendar/day"): _protected(PERMISSION_ADMIN_WRITE, "admin:write", mutates=True),
    ("POST", "/api/v1/admin/client-points/timeslot"): _protected(PERMISSION_CLIENT_POINTS_WRITE, "client_points:write", mutates=True),
    ("GET", "/api/v1/admin/orders/export.xlsx"): _protected(PERMISSION_ADMIN_READ, "admin:read", sensitive=True),
    ("GET", "/api/v1/admin/events"): _protected(PERMISSION_ADMIN_READ, "admin:read", sensitive=True),
    ("GET", "/api/v1/admin/operations"): _protected(PERMISSION_ADMIN_READ, "admin:read", sensitive=True),
    ("GET", "/api/v1/admin/smartup-auto-imports/history"): _protected(PERMISSION_ADMIN_READ, "admin:read", sensitive=True),
    ("GET", "/api/v1/admin/events/{event_id}"): _protected(PERMISSION_ADMIN_READ, "admin:read", sensitive=True),
    ("POST", "/api/v1/admin/events/{event_id}/retry"): _protected(PERMISSION_ADMIN_WRITE, "admin:write", mutates=True),
    ("POST", "/api/v1/admin/incidents"): _protected(PERMISSION_ADMIN_WRITE, "admin:write", mutates=True),
    ("GET", "/api/v1/admin/incidents"): _protected(PERMISSION_ADMIN_READ, "admin:read", sensitive=True),
    ("GET", "/api/v1/admin/incidents/{incident_id}"): _protected(PERMISSION_ADMIN_READ, "admin:read", sensitive=True),
    ("POST", "/api/v1/admin/incidents/{incident_id}/status"): _protected(PERMISSION_ADMIN_WRITE, "admin:write", mutates=True),
    ("GET", "/api/v1/readiness"): _protected(PERMISSION_DIAGNOSTICS_READ, "diagnostics:read", sensitive=True),
    ("POST", "/api/v1/admin/orders/bulk/complete-without-kiz"): _protected(PERMISSION_ADMIN_WRITE, "admin:write", mutates=True),
    ("POST", "/api/v1/admin/orders/{order_id}/archive-without-kiz"): _protected(PERMISSION_ADMIN_WRITE, "admin:write", mutates=True),
    ("POST", "/api/v1/admin/orders/{order_id}/cancel"): _protected(PERMISSION_ADMIN_WRITE, "admin:write", mutates=True),
    ("POST", "/api/v1/admin/orders/{order_id}/delete-active"): _protected(PERMISSION_ADMIN_WRITE, "orders:delete_active", mutates=True),
    ("POST", "/api/v1/admin/orders/{order_id}/reset-rescan"): _protected(PERMISSION_ADMIN_WRITE, "admin:write", mutates=True),
    ("POST", "/api/v1/admin/orders/{order_id}/restore"): _protected(PERMISSION_ADMIN_WRITE, "admin:write", mutates=True),
    ("POST", "/api/v1/admin/orders/{order_id}/resync-skladbot"): _protected(PERMISSION_ADMIN_WRITE, "admin:write", mutates=True),
    ("GET", "/api/v1/admin/skladbot/dry-runs"): _protected(PERMISSION_ADMIN_READ, "admin:read", sensitive=True),
    ("POST", "/api/v1/admin/skladbot/dry-runs/{dry_run_id}/rebuild"): _protected(PERMISSION_ADMIN_WRITE, "admin:write", mutates=True),
    ("POST", "/api/v1/sync/sources"): _protected(PERMISSION_ADMIN_WRITE, "sync:run", mutates=True),
    ("POST", "/api/v1/imports/excel/preview"): _protected(PERMISSION_ADMIN_WRITE, "imports:write", mutates=True),
    ("POST", "/api/v1/imports/excel"): _protected(PERMISSION_ADMIN_WRITE, "imports:write", mutates=True),
    ("GET", "/api/v1/returns"): _protected(PERMISSION_WAREHOUSE_READ, "returns:read"),
    ("GET", "/api/v1/returns/auth-canary/acceptance"): _protected(PERMISSION_WAREHOUSE_READ, "returns:read"),
    ("GET", "/api/v1/returns/auth-canary/desktop"): _protected(PERMISSION_WAREHOUSE_READ, "returns:read"),
    ("POST", "/api/v1/scans"): _protected(PERMISSION_WAREHOUSE_WRITE, "scans:create", mutates=True),
    ("GET", "/api/v1/kiz/availability"): _protected(PERMISSION_WAREHOUSE_READ, "kiz:read"),
    ("POST", "/api/v1/scans/undo"): _protected(PERMISSION_WAREHOUSE_WRITE, "scans:undo", mutates=True),
    ("POST", "/api/v1/orders/{order_id}/complete"): _protected(PERMISSION_WAREHOUSE_WRITE, "orders:complete", mutates=True),
    ("GET", "/api/v1/returns/lookup"): _protected(PERMISSION_WAREHOUSE_READ, "returns:read"),
    ("POST", "/api/v1/returns/{order_id}"): _protected(PERMISSION_WAREHOUSE_WRITE, "returns:write", mutates=True),
    ("POST", "/api/v1/imports"): _protected(PERMISSION_IMPORT_WRITE, "imports:create", mutates=True),
    ("POST", "/api/v1/imports/preview"): _protected(PERMISSION_IMPORT_WRITE, "imports:preview", mutates=True),
    ("GET", "/api/v1/imports"): _protected(PERMISSION_IMPORT_READ, "imports:read"),
    ("GET", "/api/v1/reports/day"): _protected(
        PERMISSION_REPORT_READ,
        "orders:read",
        service_scope_alternatives=frozenset({"reports:read"}),
    ),
    ("GET", "/api/v1/reports/reconciliation/day"): _protected(PERMISSION_REPORT_READ, "reports:read"),
    ("POST", "/api/v1/reports/reconciliation/day"): _protected(PERMISSION_ADMIN_WRITE, "reconciliation:run", mutates=True, sensitive=True),
    ("GET", "/api/v1/reports/kiz/source-files"): _protected(PERMISSION_REPORT_READ, "reports:read"),
    ("GET", "/api/v1/reports/kiz/dates"): _protected(PERMISSION_REPORT_READ, "reports:read"),
    ("GET", "/api/v1/reports/kiz/date"): _protected(PERMISSION_REPORT_READ, "reports:read"),
    ("GET", "/api/v1/reports/kiz/range"): _protected(PERMISSION_REPORT_READ, "reports:read"),
    ("GET", "/api/v1/reports/kiz/source-file"): _protected(PERMISSION_REPORT_READ, "reports:read"),
    ("GET", "/api/v1/logistics/dates"): _protected(PERMISSION_LOGISTICS_READ, "logistics:read"),
    ("GET", "/api/v1/logistics/report"): _protected(PERMISSION_LOGISTICS_READ, "logistics:read"),
    ("GET", "/api/v1/diagnostics/logs"): _protected(PERMISSION_DIAGNOSTICS_READ, "diagnostics:read", sensitive=True),
}


def route_policy(method: str, path_template: str) -> RoutePolicy | None:
    return ROUTE_POLICIES.get((str(method or "").upper(), str(path_template or "")))


def permissions_for_role(role: str) -> tuple[str, ...]:
    return tuple(sorted(ROLE_PERMISSION_MATRIX.get(str(role or ""), frozenset())))
