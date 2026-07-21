#!/usr/bin/env python3
"""Check backend dependency, size, and Telegram orchestration boundaries."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


DEFAULT_EXCEPTION_PATH = Path("tools/code_organization_exceptions.json")
TELEGRAM_WORKER_MAX_LINES = 3000
TELEGRAM_PROCESSOR_MAX_LINES = 2000
ALLOWED_EXCEPTION_RULES = {
    "max_lines",
    "telegram_orchestrator_persistence",
}
PERSISTENCE_CALLS = {
    "add",
    "add_all",
    "bulk_insert_mappings",
    "bulk_save_objects",
    "bulk_update_mappings",
    "commit",
    "delete",
    "execute",
    "flush",
    "merge",
    "rollback",
    "select",
}
TELEGRAM_ORCHESTRATOR_METHODS = {
    "__init__",
    "__getattr__",
    "_initialize_processors",
    "configured",
    "handle_callback_query",
    "handle_update",
    "notify_update_error",
    "poll_once",
}


@dataclass(frozen=True)
class ExceptionEntry:
    rule: str
    path: str
    owner: str
    reason: str


@dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    message: str


@dataclass
class CheckResult:
    graph: dict[str, set[str]] = field(default_factory=dict)
    order_skladbot_sccs: list[list[str]] = field(default_factory=list)
    telegram_worker_sccs: list[list[str]] = field(default_factory=list)
    telegram_processor_back_edges: list[tuple[str, str]] = field(default_factory=list)
    line_counts: dict[str, tuple[int, int]] = field(default_factory=dict)
    violations: list[Violation] = field(default_factory=list)
    applied_exceptions: list[ExceptionEntry] = field(default_factory=list)
    unused_exceptions: list[ExceptionEntry] = field(default_factory=list)
    exception_errors: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        return [violation.message for violation in self.violations] + self.exception_errors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when a check fails.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--exceptions", type=Path, default=DEFAULT_EXCEPTION_PATH)
    return parser.parse_args(argv)


def module_name(path: Path) -> str:
    return path.stem


def relative_import_targets(node: ast.ImportFrom, known_modules: set[str]) -> set[str]:
    targets: set[str] = set()
    if node.level == 1:
        if node.module:
            target = node.module.split(".", 1)[0]
            if target in known_modules:
                targets.add(target)
        else:
            targets.update(alias.name for alias in node.names if alias.name in known_modules)
        return targets
    if node.level == 0 and node.module:
        prefix = "backend.app."
        if node.module.startswith(prefix):
            target = node.module[len(prefix):].split(".", 1)[0]
            if target in known_modules:
                targets.add(target)
    return targets


def build_dependency_graph(app_dir: Path) -> dict[str, set[str]]:
    module_paths = {module_name(path): path for path in app_dir.glob("*.py") if path.name != "__init__.py"}
    known_modules = set(module_paths)
    graph = {name: set() for name in known_modules}
    for name, path in module_paths.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                graph[name].update(relative_import_targets(node, known_modules))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    prefix = "backend.app."
                    if alias.name.startswith(prefix):
                        target = alias.name[len(prefix):].split(".", 1)[0]
                        if target in known_modules:
                            graph[name].add(target)
    return graph


def strongly_connected_components(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return Tarjan SCCs in deterministic order."""
    index = 0
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indexes[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for target in sorted(graph.get(node, set())):
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])

        if lowlinks[node] != indexes[node]:
            return
        component: list[str] = []
        while stack:
            target = stack.pop()
            on_stack.remove(target)
            component.append(target)
            if target == node:
                break
        components.append(sorted(component))

    for node in sorted(graph):
        if node not in indexes:
            visit(node)
    return sorted(components)


def is_order_module(name: str) -> bool:
    return name.startswith("order") or name.startswith("orders")


def is_skladbot_module(name: str) -> bool:
    return name.startswith("skladbot")


def forbidden_order_skladbot_sccs(graph: dict[str, set[str]]) -> list[list[str]]:
    return [
        component
        for component in strongly_connected_components(graph)
        if len(component) > 1
        and any(is_order_module(name) for name in component)
        and any(is_skladbot_module(name) for name in component)
    ]


def is_telegram_processor(name: str) -> bool:
    return name.startswith("telegram_") and name.endswith("_processor")


def forbidden_telegram_processor_back_edges(graph: dict[str, set[str]]) -> list[tuple[str, str]]:
    return sorted(
        (name, "telegram_worker")
        for name, targets in graph.items()
        if is_telegram_processor(name) and "telegram_worker" in targets
    )


def forbidden_telegram_worker_sccs(graph: dict[str, set[str]]) -> list[list[str]]:
    return [
        component
        for component in strongly_connected_components(graph)
        if len(component) > 1
        and "telegram_worker" in component
        and any(is_telegram_processor(name) for name in component)
    ]


def normalize_repo_path(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def count_lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def collect_line_counts(root: Path, app_dir: Path) -> dict[str, tuple[int, int]]:
    checks: list[tuple[Path, int]] = [(app_dir / "telegram_worker.py", TELEGRAM_WORKER_MAX_LINES)]
    checks.extend((path, TELEGRAM_PROCESSOR_MAX_LINES) for path in sorted(app_dir.glob("telegram_*_processor.py")))
    return {
        normalize_repo_path(root, path): (count_lines(path), limit)
        for path, limit in checks
        if path.exists()
    }


def size_violations(line_counts: dict[str, tuple[int, int]]) -> list[Violation]:
    violations: list[Violation] = []
    for relative, (lines, limit) in line_counts.items():
        if lines > limit:
            violations.append(Violation(
                rule="max_lines",
                path=relative,
                message=f"{relative}: {lines} lines exceeds limit {limit}",
            ))
    return violations


def imported_name(node: ast.ImportFrom) -> str:
    if node.level == 1:
        return node.module or ""
    return node.module or ""


def telegram_persistence_findings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    findings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_module = imported_name(node)
            imported_symbols = {alias.name for alias in node.names}
            if imported_module == "sqlalchemy" or imported_module.startswith("sqlalchemy."):
                findings.add(f"line {node.lineno}: imports ORM module {imported_module}")
            if node.level == 1 and imported_module in {"db", "models"}:
                findings.add(f"line {node.lineno}: imports persistence module .{imported_module}")
            if node.level == 1 and imported_module.endswith("runtime_dependencies"):
                findings.add(f"line {node.lineno}: imports persistence service locator .{imported_module}")
            if node.level == 1 and not imported_module:
                for imported_symbol in imported_symbols:
                    if imported_symbol.endswith("runtime_dependencies"):
                        findings.add(
                            f"line {node.lineno}: imports persistence service locator .{imported_symbol}"
                        )
            if "SessionLocal" in imported_symbols:
                findings.add(f"line {node.lineno}: imports SessionLocal")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlalchemy" or alias.name.startswith("sqlalchemy."):
                    findings.add(f"line {node.lineno}: imports ORM module {alias.name}")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "SessionLocal" for target in targets):
                findings.add(f"line {node.lineno}: assigns indirect SessionLocal service locator")
        elif isinstance(node, ast.Attribute) and node.attr == "SessionLocal":
            findings.add(f"line {node.lineno}: accesses indirect .SessionLocal service locator")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in PERSISTENCE_CALLS:
                findings.add(f"line {node.lineno}: calls persistence function {node.func.id}()")
            elif isinstance(node.func, ast.Attribute) and node.func.attr in PERSISTENCE_CALLS:
                findings.add(f"line {node.lineno}: calls persistence method .{node.func.attr}()")
    return sorted(findings, key=lambda value: (int(value.split()[1].rstrip(":")), value))


def persistence_violations(root: Path, app_dir: Path) -> list[Violation]:
    path = app_dir / "telegram_worker.py"
    if not path.exists():
        return []
    findings = telegram_persistence_findings(path)
    if not findings:
        return []
    relative = normalize_repo_path(root, path)
    preview = "; ".join(findings[:8])
    suffix = f"; +{len(findings) - 8} more" if len(findings) > 8 else ""
    return [Violation(
        rule="telegram_orchestrator_persistence",
        path=relative,
        message=f"{relative}: Telegram orchestrator owns persistence: {preview}{suffix}",
    )]


def orchestrator_method_violations(root: Path, app_dir: Path) -> list[Violation]:
    path = app_dir / "telegram_worker.py"
    if not path.exists():
        return []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    worker = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TelegramWorker"),
        None,
    )
    if worker is None:
        return [Violation(
            rule="telegram_orchestrator_method_ownership",
            path=normalize_repo_path(root, path),
            message="backend/app/telegram_worker.py: TelegramWorker class is missing",
        )]
    defined = {
        node.name
        for node in worker.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    domain_methods = sorted(defined - TELEGRAM_ORCHESTRATOR_METHODS)
    if not domain_methods:
        return []
    relative = normalize_repo_path(root, path)
    return [Violation(
        rule="telegram_orchestrator_method_ownership",
        path=relative,
        message=f"{relative}: domain methods remain in orchestrator: {', '.join(domain_methods)}",
    )]


def ast_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = ast_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def telegram_port_boundary_violations(root: Path, app_dir: Path) -> list[Violation]:
    violations: list[Violation] = []
    worker_path = app_dir / "telegram_worker.py"
    if worker_path.exists():
        tree = ast.parse(worker_path.read_text(encoding="utf-8"), filename=str(worker_path))
        worker = next(
            (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TelegramWorker"),
            None,
        )
        if worker is not None:
            forbidden_bases = sorted(ast_name(base) for base in worker.bases if ast_name(base))
            if forbidden_bases:
                relative = normalize_repo_path(root, worker_path)
                violations.append(Violation(
                    rule="telegram_explicit_ports",
                    path=relative,
                    message=(
                        f"{relative}: TelegramWorker must use composition, not inherit processors/ports: "
                        f"{', '.join(forbidden_bases)}"
                    ),
                ))
            raw_calls = sorted({
                node.lineno
                for node in ast.walk(worker)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "telegram_request"
            })
            if raw_calls:
                relative = normalize_repo_path(root, worker_path)
                violations.append(Violation(
                    rule="telegram_external_payload_ownership",
                    path=relative,
                    message=(
                        f"{relative}: TelegramWorker constructs generic Telegram requests at lines "
                        f"{','.join(str(line) for line in raw_calls)}"
                    ),
                ))
        transport_imports = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                transport_imports.extend(
                    alias.name for alias in node.names if alias.name in {"httpx", "urllib", "urllib.parse"}
                )
            elif isinstance(node, ast.ImportFrom) and node.module in {"httpx", "urllib", "urllib.parse"}:
                transport_imports.append(node.module)
        if transport_imports:
            relative = normalize_repo_path(root, worker_path)
            violations.append(Violation(
                rule="telegram_external_payload_ownership",
                path=relative,
                message=(
                    f"{relative}: TelegramWorker imports transport modules: "
                    f"{', '.join(sorted(set(transport_imports)))}"
                ),
            ))

    processor_classes = {
        "telegram_admin_processor.py": "TelegramAdminProcessor",
        "telegram_import_processor.py": "TelegramImportProcessor",
        "telegram_report_processor.py": "TelegramReportProcessor",
        "telegram_scheduled_report_processor.py": "TelegramScheduledReportProcessor",
    }
    for filename, class_name in processor_classes.items():
        path = app_dir / filename
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        processor = next(
            (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name),
            None,
        )
        relative = normalize_repo_path(root, path)
        bases = {ast_name(base) for base in processor.bases} if processor is not None else set()
        if processor is None or "TelegramProcessorDelegate" not in bases:
            violations.append(Violation(
                rule="telegram_explicit_ports",
                path=relative,
                message=f"{relative}: {class_name} must declare TelegramProcessorDelegate",
            ))
        if filename == "telegram_import_processor.py":
            forbidden_imports = []
            for node in tree.body:
                if isinstance(node, ast.Import):
                    forbidden_imports.extend(
                        alias.name for alias in node.names if alias.name in {"httpx", "urllib", "urllib.parse"}
                    )
                elif isinstance(node, ast.ImportFrom) and node.module in {"httpx", "urllib", "urllib.parse"}:
                    forbidden_imports.append(node.module)
            if forbidden_imports or "api.telegram.org" in source:
                details = sorted(set(forbidden_imports))
                if "api.telegram.org" in source:
                    details.append("api.telegram.org")
                violations.append(Violation(
                    rule="telegram_external_payload_ownership",
                    path=relative,
                    message=f"{relative}: processor owns Telegram HTTP details: {', '.join(details)}",
                ))
    return violations


def load_exceptions(path: Path) -> tuple[list[ExceptionEntry], list[str]]:
    if not path.exists():
        return [], [f"exception file not found: {path}"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"invalid exception file {path}: {exc}"]
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("exceptions"), list):
        return [], [f"{path}: expected object with version=1 and exceptions array"]

    entries: list[ExceptionEntry] = []
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(payload["exceptions"]):
        label = f"{path}: exceptions[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{label} must be an object")
            continue
        values = {key: raw.get(key) for key in ("rule", "path", "owner", "reason")}
        missing = [key for key, value in values.items() if not isinstance(value, str) or not value.strip()]
        if missing:
            errors.append(f"{label} missing non-empty fields: {', '.join(missing)}")
            continue
        rule = values["rule"].strip()
        relative = values["path"].strip()
        if rule not in ALLOWED_EXCEPTION_RULES:
            errors.append(f"{label} has unsupported rule: {rule}")
            continue
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            errors.append(f"{label} path must be repository-relative: {relative}")
            continue
        key = (rule, relative)
        if key in seen:
            errors.append(f"{label} duplicates exception {rule}:{relative}")
            continue
        seen.add(key)
        entries.append(ExceptionEntry(rule, relative, values["owner"].strip(), values["reason"].strip()))
    return entries, errors


def apply_exceptions(
    violations: Iterable[Violation],
    exceptions: list[ExceptionEntry],
) -> tuple[list[Violation], list[ExceptionEntry], list[ExceptionEntry]]:
    by_key = {(entry.rule, entry.path): entry for entry in exceptions}
    remaining: list[Violation] = []
    applied: list[ExceptionEntry] = []
    for violation in violations:
        entry = by_key.get((violation.rule, violation.path))
        if entry is None:
            remaining.append(violation)
        else:
            applied.append(entry)
    applied_keys = {(entry.rule, entry.path) for entry in applied}
    unused = [entry for entry in exceptions if (entry.rule, entry.path) not in applied_keys]
    return remaining, applied, unused


def run_checks(root: Path, exception_path: Path) -> CheckResult:
    root = root.resolve()
    app_dir = root / "backend" / "app"
    result = CheckResult()
    if not app_dir.is_dir():
        result.exception_errors.append(f"backend app directory not found: {app_dir}")
        return result

    result.graph = build_dependency_graph(app_dir)
    result.order_skladbot_sccs = forbidden_order_skladbot_sccs(result.graph)
    result.telegram_worker_sccs = forbidden_telegram_worker_sccs(result.graph)
    result.telegram_processor_back_edges = forbidden_telegram_processor_back_edges(result.graph)
    result.line_counts = collect_line_counts(root, app_dir)
    raw_violations: list[Violation] = []
    for component in result.order_skladbot_sccs:
        raw_violations.append(Violation(
            rule="order_skladbot_cycle",
            path="backend/app",
            message=f"forbidden order/SkladBot dependency cycle: {' -> '.join(component)}",
        ))
    for component in result.telegram_worker_sccs:
        raw_violations.append(Violation(
            rule="telegram_worker_cycle",
            path="backend/app",
            message=f"forbidden Telegram worker/processor dependency cycle: {' -> '.join(component)}",
        ))
    for source, target in result.telegram_processor_back_edges:
        raw_violations.append(Violation(
            rule="telegram_processor_back_edge",
            path=f"backend/app/{source}.py",
            message=f"forbidden Telegram processor back-edge: {source} -> {target}",
        ))
    raw_violations.extend(size_violations(result.line_counts))
    raw_violations.extend(persistence_violations(root, app_dir))
    raw_violations.extend(orchestrator_method_violations(root, app_dir))
    raw_violations.extend(telegram_port_boundary_violations(root, app_dir))

    resolved_exception_path = exception_path if exception_path.is_absolute() else root / exception_path
    exceptions, exception_errors = load_exceptions(resolved_exception_path)
    result.exception_errors.extend(exception_errors)
    result.violations, result.applied_exceptions, result.unused_exceptions = apply_exceptions(raw_violations, exceptions)
    result.exception_errors.extend(
        f"unused organization exception: {entry.rule}:{entry.path}"
        for entry in result.unused_exceptions
    )
    return result


def emit(message: str) -> None:
    sys.stdout.write(f"{message}\n")


def print_result(result: CheckResult) -> None:
    edge_count = sum(len(targets) for targets in result.graph.values())
    emit(f"CODE_ORGANIZATION_GRAPH nodes={len(result.graph)} edges={edge_count}")
    emit(f"CODE_ORGANIZATION_ORDER_SKLADBOT_SCCS count={len(result.order_skladbot_sccs)}")
    for component in result.order_skladbot_sccs:
        emit(f"CODE_ORGANIZATION_SCC modules={','.join(component)}")
    emit(f"CODE_ORGANIZATION_TELEGRAM_WORKER_SCCS count={len(result.telegram_worker_sccs)}")
    for component in result.telegram_worker_sccs:
        emit(f"CODE_ORGANIZATION_TELEGRAM_SCC modules={','.join(component)}")
    emit(f"CODE_ORGANIZATION_TELEGRAM_BACK_EDGES count={len(result.telegram_processor_back_edges)}")
    for source, target in result.telegram_processor_back_edges:
        emit(f"CODE_ORGANIZATION_TELEGRAM_BACK_EDGE source={source} target={target}")
    applied_keys = {(entry.rule, entry.path) for entry in result.applied_exceptions}
    failed_keys = {(violation.rule, violation.path) for violation in result.violations}
    for path, (lines, limit) in sorted(result.line_counts.items()):
        key = ("max_lines", path)
        status = "exception" if key in applied_keys else "failed" if key in failed_keys else "ok"
        emit(f"CODE_ORGANIZATION_SIZE path={path} lines={lines} limit={limit} status={status}")
    for entry in result.applied_exceptions:
        emit(
            "CODE_ORGANIZATION_EXCEPTION "
            f"status=applied rule={entry.rule} path={entry.path} owner={json.dumps(entry.owner, ensure_ascii=False)} "
            f"reason={json.dumps(entry.reason, ensure_ascii=False)}"
        )
    for entry in result.unused_exceptions:
        emit(f"CODE_ORGANIZATION_EXCEPTION status=unused rule={entry.rule} path={entry.path}")
    for error in result.errors:
        emit(f"CODE_ORGANIZATION_ERROR {error}")
    status = "ok" if not result.errors else "failed"
    emit(
        f"CODE_ORGANIZATION_RESULT status={status} errors={len(result.errors)} "
        f"exceptions={len(result.applied_exceptions)}"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_checks(args.root, args.exceptions)
    print_result(result)
    return 1 if args.strict and result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
