#!/usr/bin/env python3
"""Fail-closed compatibility gate for the current TakSklad desktop 2.0.52 API.

The gate is intentionally static and data-free.  It verifies the HTTP methods,
route templates, authentication policy and service-principal scopes used by the
published desktop client.  Additional backend routes are allowed.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
FROZEN_DESKTOP_VERSION = "2.0.52"


@dataclass(frozen=True, order=True)
class FrozenRoute:
    method: str
    path: str
    authentication: str
    scope: str | None
    client_source: str


FROZEN_ROUTES = (
    FrozenRoute("GET", "/health", "public", None, "desktop_diagnostics.py"),
    FrozenRoute("GET", "/api/v1/kiz/availability", "protected", "kiz:read", "backend_client.py"),
    FrozenRoute("GET", "/api/v1/orders/active", "protected", "orders:read", "backend_client.py"),
    FrozenRoute("GET", "/api/v1/reports/day", "protected", "orders:read", "backend_client.py"),
    FrozenRoute("GET", "/api/v1/returns", "protected", "returns:read", "backend_client.py"),
    FrozenRoute(
        "GET",
        "/api/v1/returns/auth-canary/desktop",
        "protected",
        "returns:read",
        "returns_auth_canary.py",
    ),
    FrozenRoute("GET", "/api/v1/returns/lookup", "protected", "returns:read", "backend_client.py"),
    FrozenRoute("POST", "/api/v1/imports", "protected", "imports:create", "backend_client.py"),
    FrozenRoute("POST", "/api/v1/imports/preview", "protected", "imports:preview", "backend_client.py"),
    FrozenRoute("POST", "/api/v1/orders/{order_id}/complete", "protected", "orders:complete", "backend_client.py"),
    FrozenRoute("POST", "/api/v1/returns/{order_id}", "protected", "returns:write", "backend_client.py"),
    FrozenRoute("POST", "/api/v1/scans", "protected", "scans:create", "backend_client.py"),
    FrozenRoute("POST", "/api/v1/scans/undo", "protected", "scans:undo", "backend_client.py"),
    FrozenRoute("POST", "/api/v1/sync/sources", "protected", "sync:run", "backend_client.py"),
)


def _read_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if not isinstance(node, ast.JoinedStr):
        return None
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            if isinstance(value.value, ast.Name):
                parts.append("{" + value.value.id + "}")
            else:
                parts.append("{value}")
        else:
            return None
    return "".join(parts)


def _route_template(value: str) -> str:
    route = str(value or "").split("?", 1)[0]
    if route.endswith("{query}"):
        route = route[:-7]
    return route


def discover_backend_client_calls(path: Path) -> set[tuple[str, str]]:
    calls: set[tuple[str, str]] = set()
    for node in ast.walk(_read_tree(path)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "backend_request" and len(node.args) >= 2:
            method = _literal_string(node.args[0])
            route = _literal_string(node.args[1])
        elif node.func.id == "backend_request_all_pages" and node.args:
            method = "GET"
            route = _literal_string(node.args[0])
        else:
            continue
        if method and route:
            calls.add((method.upper(), _route_template(route)))
    return calls


def discover_declared_routes(path: Path) -> set[tuple[str, str]]:
    prefixes = {"app": "", "api": "/api/v1", "auth_api": "/api/v1/auth"}
    routes: set[tuple[str, str]] = set()
    for node in ast.walk(_read_tree(path)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            owner = decorator.func.value
            if not isinstance(owner, ast.Name) or owner.id not in prefixes or not decorator.args:
                continue
            method = decorator.func.attr.upper()
            route = _literal_string(decorator.args[0])
            if method in {"GET", "POST", "PUT", "PATCH", "DELETE"} and route is not None:
                routes.add((method, prefixes[owner.id] + route))
    return routes


def discover_route_policies(path: Path) -> dict[tuple[str, str], tuple[str, str | None]]:
    policies: dict[tuple[str, str], tuple[str, str | None]] = {}
    tree = _read_tree(path)
    assignment = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "ROUTE_POLICIES"
        ),
        None,
    )
    if assignment is None or not isinstance(assignment.value, ast.Dict):
        return policies
    for key_node, value_node in zip(assignment.value.keys, assignment.value.values):
        if not isinstance(key_node, ast.Tuple) or len(key_node.elts) != 2:
            continue
        method = _literal_string(key_node.elts[0])
        route = _literal_string(key_node.elts[1])
        if not method or not route or not isinstance(value_node, ast.Call):
            continue
        factory = value_node.func.id if isinstance(value_node.func, ast.Name) else ""
        if factory == "_public":
            policies[(method.upper(), route)] = ("public", None)
        elif factory == "_protected" and len(value_node.args) >= 2:
            scope = _literal_string(value_node.args[1])
            policies[(method.upper(), route)] = ("protected", scope)
        elif factory == "_session":
            policies[(method.upper(), route)] = ("session", None)
    return policies


def discover_desktop_scopes(path: Path) -> set[str]:
    for node in _read_tree(path).body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "SERVICE_PRINCIPAL_SCOPE_MATRIX" for target in node.targets):
            continue
        if not isinstance(node.value, ast.Dict):
            break
        for key, value in zip(node.value.keys, node.value.values):
            if _literal_string(key) != "desktop":
                continue
            collection = value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "frozenset"
                and len(value.args) == 1
            ):
                collection = value.args[0]
            if not isinstance(collection, (ast.Set, ast.List, ast.Tuple)):
                return set()
            return {item for element in collection.elts if (item := _literal_string(element)) is not None}
    return set()


def discover_app_version(path: Path) -> str:
    for node in _read_tree(path).body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "APP_VERSION" for target in node.targets):
            return _literal_string(node.value) or ""
    return ""


def bearer_auth_contract_present(path: Path) -> bool:
    tree = _read_tree(path)
    function = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "make_backend_headers"),
        None,
    )
    if function is None:
        return False
    loads_secret = any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "load_backend_auth_bundle"
        for node in ast.walk(function)
    )
    has_bearer = any(
        isinstance(node, ast.JoinedStr)
        and any(isinstance(part, ast.Constant) and str(part.value).startswith("Bearer ") for part in node.values)
        for node in ast.walk(function)
    )
    return loads_secret and has_bearer


def validate_contract(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    expected_client_calls = {
        (route.method, route.path) for route in FROZEN_ROUTES if route.client_source == "backend_client.py"
    }
    actual_client_calls = discover_backend_client_calls(root / "src/taksklad/backend_client.py")
    if actual_client_calls != expected_client_calls:
        missing = sorted(expected_client_calls - actual_client_calls)
        unexpected = sorted(actual_client_calls - expected_client_calls)
        if missing:
            errors.append(f"desktop calls missing or renamed: {missing}")
        if unexpected:
            errors.append(f"desktop {FROZEN_DESKTOP_VERSION} gained unversioned API calls: {unexpected}")

    declared_routes = discover_declared_routes(root / "backend/app/main.py")
    policies = discover_route_policies(root / "backend/app/access_policy.py")
    desktop_scopes = discover_desktop_scopes(root / "backend/app/auth_identities.py")
    for route in FROZEN_ROUTES:
        identity = (route.method, route.path)
        if identity not in declared_routes:
            errors.append(f"required backend route missing: {route.method} {route.path}")
            continue
        if route.authentication == "public":
            if identity in policies and policies[identity] != ("public", None):
                errors.append(f"public route authentication changed: {route.method} {route.path}")
            continue
        actual_policy = policies.get(identity)
        expected_policy = (route.authentication, route.scope)
        if actual_policy != expected_policy:
            errors.append(
                f"route policy mismatch for {route.method} {route.path}: "
                f"expected={expected_policy!r} actual={actual_policy!r}"
            )
        if route.scope and route.scope not in desktop_scopes:
            errors.append(f"desktop principal is missing required scope {route.scope} for {route.method} {route.path}")

    version = discover_app_version(root / "src/taksklad/config.py")
    if version != FROZEN_DESKTOP_VERSION:
        errors.append(f"frozen desktop version changed: expected={FROZEN_DESKTOP_VERSION} actual={version or 'missing'}")
    if not bearer_auth_contract_present(root / "src/taksklad/backend_client.py"):
        errors.append("desktop Bearer credential contract is missing or changed")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    errors = validate_contract(args.root.resolve())
    if errors:
        for error in errors:
            print(f"desktop_api_contract_error: {error}", file=sys.stderr)
        return 1
    print(
        f"desktop_api_contract_ok version={FROZEN_DESKTOP_VERSION} "
        f"contract=1 routes={len(FROZEN_ROUTES)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
