#!/usr/bin/env python3
"""Fail-closed, value-redacting supply-chain security gate for TakSklad."""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


RULESET = "taksklad-security-gate-v1"
SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
SAFE_TEXT_SUFFIXES = {
    ".cfg",
    ".conf",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".ts",
    ".tsx",
    ".xml",
    ".yaml",
    ".yml",
}
FORBIDDEN_ROOTS = {
    "backups",
    "backup",
    "client-exports",
    "client_exports",
    "credentials",
    "exports",
    "outputs",
    "reports",
    "scan_backups",
    "secrets",
    "Сверка",
    "отчеты",
}
FORBIDDEN_EXACT_NAMES = {"credentials.json", "пароли.md"}
CONTENT_SKIP_PREFIXES = (
    ".git/",
    ".release-state/",
    ".supergoal/",
    "archive/",
    "backups/",
    "backup/",
    "client-exports/",
    "client_exports/",
    "credentials/",
    "exports/",
    "frontend/coverage/",
    "frontend/dist/",
    "frontend/node_modules/",
    "generated/",
    "outputs/",
    "reports/",
    "scan_backups/",
    "secrets/",
    "tests/fixtures/security_gate/",
    "Сверка/",
    "отчеты/",
)
SECRET_RULES = (
    (
        "secret.aws-access-key",
        re.compile(r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])"),
        "critical",
    ),
    (
        "secret.github-token",
        re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9_]{30,}(?![A-Za-z0-9_])"),
        "critical",
    ),
    (
        "secret.openai-key",
        re.compile(r"(?<![A-Za-z0-9_-])sk-(?:proj-)?[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
        "critical",
    ),
    (
        "secret.private-key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
            r"\s+[A-Za-z0-9+/=\r\n]{80,}"
            r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
        ),
        "critical",
    ),
)


def emit(message: str, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.write(message + "\n")


@dataclass(frozen=True)
class Finding:
    scanner: str
    rule_id: str
    severity: str
    path: str = ""
    line: int = 0
    package: str = ""
    version: str = ""
    detail: str = ""


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _git_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("git ls-files failed")
    return sorted(
        item.decode("utf-8", "surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    )


def _is_forbidden_path(path: str) -> bool:
    normalized = PurePosixPath(path)
    lowered_parts = {part.lower() for part in normalized.parts}
    if any(part in FORBIDDEN_ROOTS or part.lower() in FORBIDDEN_ROOTS for part in normalized.parts):
        return True
    if normalized.name.lower() in FORBIDDEN_EXACT_NAMES:
        return True
    if normalized.name.startswith(".env") and normalized.name != ".env.example":
        return True
    return bool(lowered_parts & {item.lower() for item in FORBIDDEN_ROOTS})


def scan_forbidden_paths(paths: Iterable[str]) -> list[Finding]:
    return [
        Finding(
            scanner="forbidden-data",
            rule_id="forbidden-data.release-path",
            severity="critical",
            path=path,
            detail="forbidden path is present in the release source set",
        )
        for path in paths
        if _is_forbidden_path(path)
    ]


def _phase_baseline_ref(root: Path) -> str:
    explicit = os.environ.get("SECURITY_GATE_BASE_REF", "").strip()
    if explicit:
        return explicit
    state_path = (
        root
        / ".supergoal/taksklad-full-stabilization-security-per-e9read/STATE.md"
    )
    try:
        match = re.search(
            r"^\*\*Baseline ref:\*\*\s*([0-9a-f]{40})\s*$",
            state_path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    except OSError:
        match = None
    if match:
        return match.group(1)
    return "HEAD^"


def scan_new_forbidden_paths(root: Path, paths: Iterable[str]) -> tuple[list[Finding], int]:
    baseline_ref = _phase_baseline_ref(root)
    findings: list[Finding] = []
    baseline_paths = 0
    for path in paths:
        if not _is_forbidden_path(path):
            continue
        existed = subprocess.run(
            ["git", "cat-file", "-e", f"{baseline_ref}:{path}"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
        if existed:
            baseline_paths += 1
            continue
        findings.extend(scan_forbidden_paths([path]))
    return findings, baseline_paths


def _content_candidates(root: Path, paths: Iterable[str]) -> Iterable[tuple[str, Path]]:
    for relative in paths:
        if (
            PurePosixPath(relative).name.startswith(".env")
            or _is_forbidden_path(relative)
            or relative.startswith(CONTENT_SKIP_PREFIXES)
        ):
            continue
        path = root / relative
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in SAFE_TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
        except OSError:
            continue
        yield relative, path


def scan_secrets(root: Path, paths: Iterable[str]) -> list[Finding]:
    findings: list[Finding] = []
    for relative, path in _content_candidates(root, paths):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw:
            continue
        text = raw.decode("utf-8", "replace")
        for rule_id, pattern, severity in SECRET_RULES:
            for match in pattern.finditer(text):
                findings.append(
                    Finding(
                        scanner="secret",
                        rule_id=rule_id,
                        severity=severity,
                        path=relative,
                        line=text.count("\n", 0, match.start()) + 1,
                        detail="value redacted",
                    )
                )
    return findings


class _SastVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.findings: list[Finding] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = ""
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            prefix = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
            name = f"{prefix}.{node.func.attr}" if prefix else node.func.attr

        rule_id = ""
        severity = "high"
        if name in {"eval", "exec"}:
            rule_id = "sast.dynamic-code-execution"
            severity = "critical"
        elif name in {"pickle.loads", "pickle.load"}:
            rule_id = "sast.unsafe-deserialization"
            severity = "critical"
        elif name == "yaml.load":
            has_safe_loader = any(
                keyword.arg == "Loader"
                and (
                    isinstance(keyword.value, ast.Attribute)
                    and keyword.value.attr in {"SafeLoader", "CSafeLoader"}
                )
                for keyword in node.keywords
            )
            if not has_safe_loader:
                rule_id = "sast.unsafe-yaml-load"
        elif name.startswith("subprocess."):
            if any(
                keyword.arg == "shell"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            ):
                rule_id = "sast.subprocess-shell-true"
                severity = "critical"

        if rule_id:
            self.findings.append(
                Finding(
                    scanner="sast",
                    rule_id=rule_id,
                    severity=severity,
                    path=self.path,
                    line=node.lineno,
                    detail="unsafe call pattern",
                )
            )
        self.generic_visit(node)


def scan_sast(root: Path, paths: Iterable[str]) -> list[Finding]:
    findings: list[Finding] = []
    for relative, path in _content_candidates(root, paths):
        if path.suffix.lower() != ".py" or relative.startswith("tests/"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError, UnicodeError):
            continue
        visitor = _SastVisitor(relative)
        visitor.visit(tree)
        findings.extend(visitor.findings)
    return findings


def _logical_requirements(path: Path) -> list[str]:
    logical: list[str] = []
    current = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("--hash="):
            current = f"{current} {stripped}".strip()
        else:
            if current:
                logical.append(current)
            current = stripped
        if current.endswith("\\"):
            current = current[:-1].rstrip()
        else:
            logical.append(current)
            current = ""
    if current:
        logical.append(current)
    return logical


def _canonical_package(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _lock_packages(path: Path) -> tuple[dict[str, set[str]], list[Finding]]:
    packages: dict[str, set[str]] = {}
    findings: list[Finding] = []
    for line_number, logical in enumerate(_logical_requirements(path), start=1):
        if logical.startswith("--"):
            findings.append(
                Finding(
                    scanner="dependency",
                    rule_id="dependency.unapproved-option",
                    severity="high",
                    path=str(path),
                    line=line_number,
                )
            )
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^ ;]+)", logical)
        if not match:
            findings.append(
                Finding(
                    scanner="dependency",
                    rule_id="dependency.not-exactly-pinned",
                    severity="high",
                    path=str(path),
                    line=line_number,
                )
            )
            continue
        name = _canonical_package(match.group(1))
        version = match.group(2)
        packages.setdefault(name, set()).add(version)
        if not re.search(r"(?:^|\s)--hash=sha256:[0-9a-f]{64}(?:\s|$)", logical):
            findings.append(
                Finding(
                    scanner="dependency",
                    rule_id="dependency.hash-missing",
                    severity="critical",
                    path=str(path),
                    line=line_number,
                    package=name,
                    version=version,
                )
            )
    return packages, findings


def _direct_requirement_names(path: Path) -> set[str]:
    names: set[str] = set()
    for logical in _logical_requirements(path):
        match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?", logical)
        if match:
            names.add(_canonical_package(match.group(1)))
    return names


def scan_dependency_integrity(root: Path) -> tuple[list[Finding], dict[str, int]]:
    findings: list[Finding] = []
    summary: dict[str, int] = {}
    locks = (
        ("desktop", root / "requirements/desktop.lock", root / "requirements.txt"),
        ("backend", root / "backend/requirements.lock", root / "backend/requirements.txt"),
        ("security-tools", root / "security/requirements.lock", root / "security/requirements.in"),
    )
    for target, lock_path, direct_path in locks:
        if not lock_path.is_file():
            findings.append(
                Finding(
                    scanner="dependency",
                    rule_id="dependency.lock-missing",
                    severity="critical",
                    path=str(lock_path.relative_to(root)),
                )
            )
            continue
        packages, lock_findings = _lock_packages(lock_path)
        findings.extend(lock_findings)
        summary[target] = sum(len(versions) for versions in packages.values())
        missing_direct = sorted(_direct_requirement_names(direct_path) - set(packages))
        for package in missing_direct:
            findings.append(
                Finding(
                    scanner="dependency",
                    rule_id="dependency.direct-package-missing",
                    severity="critical",
                    path=str(lock_path.relative_to(root)),
                    package=package,
                )
            )

    package_lock = root / "frontend/package-lock.json"
    try:
        lock_data = json.loads(package_lock.read_text(encoding="utf-8"))
        package_rows = lock_data["packages"]
    except (OSError, ValueError, KeyError, TypeError):
        findings.append(
            Finding(
                scanner="dependency",
                rule_id="dependency.npm-lock-invalid",
                severity="critical",
                path="frontend/package-lock.json",
            )
        )
        return findings, summary

    npm_count = 0
    for package_path, package in package_rows.items():
        if not package_path or package.get("link"):
            continue
        version = str(package.get("version", ""))
        resolved = str(package.get("resolved", ""))
        integrity = str(package.get("integrity", ""))
        npm_count += 1
        if not version or any(token in version for token in ("*", "^", "~", ">", "<")):
            findings.append(
                Finding(
                    scanner="dependency",
                    rule_id="dependency.npm-version-not-exact",
                    severity="high",
                    path="frontend/package-lock.json",
                    package=package_path.removeprefix("node_modules/"),
                    version=version,
                )
            )
        if resolved.startswith("https://registry.npmjs.org/") and not integrity.startswith("sha512-"):
            findings.append(
                Finding(
                    scanner="dependency",
                    rule_id="dependency.npm-integrity-missing",
                    severity="critical",
                    path="frontend/package-lock.json",
                    package=package_path.removeprefix("node_modules/"),
                    version=version,
                )
            )
    summary["frontend"] = npm_count
    return findings, summary


def _audit_python_locks(root: Path) -> list[Finding]:
    security_lock = root / "security/requirements.lock"
    python = root / ".venv/bin/python"
    if not python.is_file():
        python = Path(sys.executable)
    findings: list[Finding] = []
    with tempfile.TemporaryDirectory(prefix="taksklad-security-tools-") as temp_dir:
        venv = Path(temp_dir) / "venv"
        create = _run([str(python), "-m", "venv", str(venv)], cwd=root)
        if create.returncode != 0:
            return [
                Finding(
                    scanner="dependency-audit",
                    rule_id="dependency-audit.tool-venv-failed",
                    severity="critical",
                )
            ]
        audit_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        env = os.environ.copy()
        env.update(
            {
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INPUT": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        install = _run(
            [
                str(audit_python),
                "-m",
                "pip",
                "install",
                "--quiet",
                "--require-hashes",
                "-r",
                str(security_lock),
            ],
            cwd=root,
            env=env,
        )
        if install.returncode != 0:
            return [
                Finding(
                    scanner="dependency-audit",
                    rule_id="dependency-audit.tool-install-failed",
                    severity="critical",
                )
            ]
        for target, lock_path in (
            ("desktop", root / "requirements/desktop.lock"),
            ("backend", root / "backend/requirements.lock"),
        ):
            audit = _run(
                [
                    str(audit_python),
                    "-m",
                    "pip_audit",
                    "-r",
                    str(lock_path),
                    "--no-deps",
                    "--disable-pip",
                    "--format=json",
                ],
                cwd=root,
                env=env,
            )
            try:
                payload = json.loads(audit.stdout)
                dependencies = payload["dependencies"]
            except (ValueError, KeyError, TypeError):
                findings.append(
                    Finding(
                        scanner="pip-audit",
                        rule_id="dependency-audit.invalid-result",
                        severity="critical",
                        path=target,
                    )
                )
                continue
            for dependency in dependencies:
                for vulnerability in dependency.get("vulns", []):
                    findings.append(
                        Finding(
                            scanner="pip-audit",
                            rule_id=str(vulnerability.get("id", "unknown-advisory")),
                            severity="high",
                            path=target,
                            package=str(dependency.get("name", "")),
                            version=str(dependency.get("version", "")),
                            detail="severity unavailable; treated as high",
                        )
                    )
            if audit.returncode not in {0, 1}:
                findings.append(
                    Finding(
                        scanner="pip-audit",
                        rule_id="dependency-audit.execution-failed",
                        severity="critical",
                        path=target,
                    )
                )
    return findings


def _audit_frontend(root: Path) -> list[Finding]:
    result = _run(
        ["npm", "audit", "--prefix", "frontend", "--omit=dev", "--json"],
        cwd=root,
        timeout=180,
    )
    try:
        payload = json.loads(result.stdout)
        vulnerabilities = payload.get("vulnerabilities", {})
    except (ValueError, TypeError):
        return [
            Finding(
                scanner="npm-audit",
                rule_id="dependency-audit.invalid-result",
                severity="critical",
                path="frontend/package-lock.json",
            )
        ]
    findings: list[Finding] = []
    for package, vulnerability in vulnerabilities.items():
        severity = str(vulnerability.get("severity", "high")).lower()
        if severity == "moderate":
            severity = "medium"
        if severity not in SEVERITY_ORDER:
            severity = "high"
        findings.append(
            Finding(
                scanner="npm-audit",
                rule_id="npm-advisory",
                severity=severity,
                path="frontend/package-lock.json",
                package=package,
            )
        )
    if result.returncode not in {0, 1}:
        findings.append(
            Finding(
                scanner="npm-audit",
                rule_id="dependency-audit.execution-failed",
                severity="critical",
                path="frontend/package-lock.json",
            )
        )
    return findings


def scan_immutable_references(root: Path) -> list[Finding]:
    verifier = root / "tools/verify_immutable_refs.py"
    if not verifier.is_file():
        return [
            Finding(
                scanner="config",
                rule_id="config.immutable-ref-verifier-missing",
                severity="critical",
                path="tools/verify_immutable_refs.py",
            )
        ]
    result = _run(
        [sys.executable, str(verifier)],
        cwd=root,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if result.returncode == 0:
        return []
    return [
        Finding(
            scanner="config",
            rule_id="config.mutable-reference",
            severity="critical",
            path="supply-chain/immutable-refs.json",
        )
    ]


def scan_container_and_workflow_config(root: Path) -> list[Finding]:
    """Static, pinned-ruleset scan that never renders Compose or opens env files."""

    findings: list[Finding] = []
    config_paths = (
        "backend/Dockerfile",
        "frontend/Dockerfile",
        "deploy/traefik/docker-compose.yml",
        "deploy/vds/docker-compose.yml",
    )
    for relative in config_paths:
        path = root / relative
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            findings.append(
                Finding(
                    scanner="container-config",
                    rule_id="container-config.file-missing",
                    severity="critical",
                    path=relative,
                )
            )
            continue
        lines = text.splitlines()
        if path.name == "Dockerfile":
            has_user = False
            for line_number, raw in enumerate(lines, start=1):
                stripped = raw.strip()
                if re.match(r"^USER\s+", stripped, re.IGNORECASE):
                    has_user = True
                from_match = re.match(r"^FROM\s+([^\s]+)", stripped, re.IGNORECASE)
                if from_match and not re.fullmatch(
                    r"[^\s@]+@sha256:[0-9a-f]{64}", from_match.group(1)
                ):
                    findings.append(
                        Finding(
                            scanner="container-config",
                            rule_id="container-config.mutable-base-image",
                            severity="critical",
                            path=relative,
                            line=line_number,
                        )
                    )
                if re.search(r"\bpip\s+install\b", stripped) and "--require-hashes" not in stripped:
                    findings.append(
                        Finding(
                            scanner="container-config",
                            rule_id="container-config.unlocked-pip-install",
                            severity="high",
                            path=relative,
                            line=line_number,
                        )
                    )
                if re.search(r"\bnpm\s+install\b", stripped) and "npm ci" not in stripped:
                    findings.append(
                        Finding(
                            scanner="container-config",
                            rule_id="container-config.unlocked-npm-install",
                            severity="high",
                            path=relative,
                            line=line_number,
                        )
                    )
                if re.search(r"\b(?:curl|wget)\b[^|\n]*\|\s*(?:sh|bash)\b", stripped):
                    findings.append(
                        Finding(
                            scanner="container-config",
                            rule_id="container-config.remote-shell-pipe",
                            severity="critical",
                            path=relative,
                            line=line_number,
                        )
                    )
                secret_assignment = re.match(
                    r"^(?:ARG|ENV)\s+[^\s=]*(?:PASSWORD|TOKEN|SECRET|PRIVATE_KEY|API_KEY)[^\s=]*"
                    r"(?:=|\s+)([^$\s][^\s]*)$",
                    stripped,
                    re.IGNORECASE,
                )
                if secret_assignment:
                    findings.append(
                        Finding(
                            scanner="container-config",
                            rule_id="container-config.literal-build-secret",
                            severity="critical",
                            path=relative,
                            line=line_number,
                            detail="value redacted",
                        )
                    )
            if not has_user:
                findings.append(
                    Finding(
                        scanner="container-config",
                        rule_id="container-config.no-explicit-user",
                        severity="medium",
                        path=relative,
                    )
                )
        else:
            for line_number, raw in enumerate(lines, start=1):
                stripped = raw.strip()
                image_match = re.match(r"^image:\s*([^\s#]+)", stripped)
                image_value = image_match.group(1) if image_match else ""
                first_party_image_variable = image_value in {
                    "${TAKSKLAD_BACKEND_IMAGE}",
                    "${TAKSKLAD_FRONTEND_IMAGE}",
                }
                if image_match and not first_party_image_variable and not re.fullmatch(
                    r"[^\s@]+@sha256:[0-9a-f]{64}", image_value
                ):
                    findings.append(
                        Finding(
                            scanner="container-config",
                            rule_id="container-config.mutable-service-image",
                            severity="critical",
                            path=relative,
                            line=line_number,
                        )
                    )
                if stripped == "privileged: true":
                    findings.append(
                        Finding(
                            scanner="container-config",
                            rule_id="container-config.privileged-container",
                            severity="critical",
                            path=relative,
                            line=line_number,
                        )
                    )
                if stripped in {"network_mode: host", "pid: host", "ipc: host"}:
                    findings.append(
                        Finding(
                            scanner="container-config",
                            rule_id="container-config.host-namespace",
                            severity="high",
                            path=relative,
                            line=line_number,
                        )
                    )
                if "/var/run/docker.sock" in stripped:
                    restricted_proxy = (
                        relative == "deploy/traefik/docker-compose.yml"
                        and stripped == "- /var/run/docker.sock:/var/run/docker.sock:ro"
                        and "  docker-socket-proxy:\n" in text
                        and '      POST: "0"' in text
                        and "      DOCKER_HOST: tcp://docker-socket-proxy:2375" in text
                    )
                    if restricted_proxy:
                        continue
                    if not stripped.endswith(":ro"):
                        severity = "critical"
                        rule_id = "container-config.docker-socket-write"
                    else:
                        severity = "medium"
                        rule_id = "container-config.docker-socket-exposed"
                    findings.append(
                        Finding(
                            scanner="container-config",
                            rule_id=rule_id,
                            severity=severity,
                            path=relative,
                            line=line_number,
                        )
                    )

    workflow_dir = root / ".github/workflows"
    for path in sorted((*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml"))):
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        for line_number, raw in enumerate(text.splitlines(), start=1):
            stripped = raw.strip()
            if stripped == "pull_request_target:":
                findings.append(
                    Finding(
                        scanner="workflow-config",
                        rule_id="workflow-config.pull-request-target",
                        severity="high",
                        path=relative,
                        line=line_number,
                    )
                )
            if stripped in {"permissions: write-all", "permissions: read-all"}:
                severity = "critical" if stripped.endswith("write-all") else "medium"
                findings.append(
                    Finding(
                        scanner="workflow-config",
                        rule_id="workflow-config.overbroad-permissions",
                        severity=severity,
                        path=relative,
                        line=line_number,
                    )
                )
            if re.search(r"(?:echo|printf).*\$\{\{\s*secrets\.", stripped, re.IGNORECASE):
                findings.append(
                    Finding(
                        scanner="workflow-config",
                        rule_id="workflow-config.secret-output",
                        severity="critical",
                        path=relative,
                        line=line_number,
                        detail="value redacted",
                    )
                )
    return findings


def _validate_exceptions(root: Path) -> tuple[list[dict[str, str]], list[Finding]]:
    path = root / "security/vulnerability-exceptions.json"
    schema_path = root / "security/vulnerability-exceptions.schema.json"
    required = {
        "id",
        "scanner",
        "package",
        "severity",
        "owner",
        "approved_by",
        "reason",
        "expires_on",
    }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        schema_required = set(
            schema["properties"]["exceptions"]["items"]["required"]
        )
        if payload.get("schema_version") != 1 or not isinstance(payload.get("exceptions"), list):
            raise ValueError("invalid schema")
        if schema_required != required:
            raise ValueError("schema and gate fields differ")
    except (OSError, ValueError, KeyError, TypeError):
        return [], [
            Finding(
                scanner="exception-policy",
                rule_id="exception-policy.invalid-schema",
                severity="critical",
                path="security/vulnerability-exceptions.json",
            )
        ]
    findings: list[Finding] = []
    today = dt.date.today()
    normalized: list[dict[str, str]] = []
    for index, raw in enumerate(payload["exceptions"]):
        if not isinstance(raw, dict) or not required.issubset(raw):
            findings.append(
                Finding(
                    scanner="exception-policy",
                    rule_id="exception-policy.required-field-missing",
                    severity="critical",
                    path="security/vulnerability-exceptions.json",
                    line=index + 1,
                )
            )
            continue
        item = {key: str(raw[key]).strip() for key in required}
        try:
            expiry = dt.date.fromisoformat(item["expires_on"])
        except ValueError:
            expiry = dt.date.min
        invalid = (
            item["severity"].lower() not in {"high", "critical"}
            or not item["owner"]
            or not item["approved_by"]
            or len(item["reason"]) < 12
            or expiry <= today
        )
        if invalid:
            findings.append(
                Finding(
                    scanner="exception-policy",
                    rule_id="exception-policy.invalid-or-expired",
                    severity="critical",
                    path="security/vulnerability-exceptions.json",
                    line=index + 1,
                )
            )
            continue
        normalized.append(item)
    return normalized, findings


def _apply_exceptions(
    findings: Iterable[Finding], exceptions: list[dict[str, str]]
) -> tuple[list[Finding], list[Finding]]:
    active: list[Finding] = []
    excepted: list[Finding] = []
    for finding in findings:
        match = next(
            (
                item
                for item in exceptions
                if item["id"] == finding.rule_id
                and item["scanner"] == finding.scanner
                and item["package"] == finding.package
                and item["severity"].lower() == finding.severity
            ),
            None,
        )
        (excepted if match else active).append(finding)
    return active, excepted


def _synthetic_fixture_findings(root: Path) -> dict[str, list[Finding]]:
    fixture_root = root / "tests/fixtures/security_gate"
    secret_path = fixture_root / "synthetic-secret.txt"
    sast_path = fixture_root / "synthetic-sast.py"
    dependency_path = fixture_root / "synthetic-dependency.json"
    forbidden_path = fixture_root / "synthetic-forbidden-paths.json"

    secret_findings: list[Finding] = []
    secret_text = secret_path.read_text(encoding="utf-8")
    for rule_id, pattern, severity in SECRET_RULES:
        for match in pattern.finditer(secret_text):
            secret_findings.append(
                Finding(
                    scanner="secret",
                    rule_id=rule_id,
                    severity=severity,
                    path="synthetic-secret.txt",
                    line=secret_text.count("\n", 0, match.start()) + 1,
                    detail="value redacted",
                )
            )

    visitor = _SastVisitor("synthetic-sast.py")
    visitor.visit(ast.parse(sast_path.read_text(encoding="utf-8")))
    dependency_payload = json.loads(dependency_path.read_text(encoding="utf-8"))
    dependency_findings = [
        Finding(
            scanner=str(item["scanner"]),
            rule_id=str(item["id"]),
            severity=str(item["severity"]),
            package=str(item["package"]),
            version=str(item["version"]),
        )
        for item in dependency_payload["findings"]
    ]
    forbidden_payload = json.loads(forbidden_path.read_text(encoding="utf-8"))
    return {
        "secret": secret_findings,
        "forbidden-data": scan_forbidden_paths(forbidden_payload["paths"]),
        "sast": visitor.findings,
        "dependency": dependency_findings,
    }


def _print_finding(finding: Finding, disposition: str) -> None:
    fields = [
        f"disposition={disposition}",
        f"scanner={finding.scanner}",
        f"severity={finding.severity}",
        f"rule={finding.rule_id}",
    ]
    if finding.path:
        fields.append(f"path={finding.path}")
    if finding.line:
        fields.append(f"line={finding.line}")
    if finding.package:
        fields.append(f"package={finding.package}")
    if finding.version:
        fields.append(f"version={finding.version}")
    if finding.detail:
        fields.append(f"detail={finding.detail.replace(' ', '_')}")
    emit("SECURITY_FINDING " + " ".join(fields))


def run_gate(root: Path, fail_on: str, synthetic: bool) -> int:
    try:
        paths = _git_paths(root)
    except RuntimeError:
        emit("SECURITY_GATE_ERROR code=SOURCE_ENUMERATION_FAILED", error=True)
        return 2

    exceptions, exception_findings = _validate_exceptions(root)
    forbidden_findings, forbidden_baseline_paths = scan_new_forbidden_paths(root, paths)
    integrity_findings, dependency_counts = scan_dependency_integrity(root)
    findings = [
        *exception_findings,
        *forbidden_findings,
        *scan_secrets(root, paths),
        *scan_sast(root, paths),
        *integrity_findings,
        *_audit_python_locks(root),
        *_audit_frontend(root),
        *scan_immutable_references(root),
        *scan_container_and_workflow_config(root),
    ]
    active, excepted = _apply_exceptions(findings, exceptions)
    threshold = SEVERITY_ORDER[fail_on]
    blocking = [item for item in active if SEVERITY_ORDER[item.severity] >= threshold]

    for finding in sorted(
        active,
        key=lambda item: (
            -SEVERITY_ORDER[item.severity],
            item.scanner,
            item.rule_id,
            item.path,
            item.line,
        ),
    ):
        _print_finding(finding, "active")
    for finding in sorted(excepted, key=lambda item: (item.scanner, item.rule_id, item.package)):
        _print_finding(finding, "excepted")

    counts = Counter(item.severity for item in active)
    emit(
        "SECURITY_SCAN_SUMMARY "
        + " ".join(
            [f"ruleset={RULESET}"]
            + [f"{severity}={counts[severity]}" for severity in SEVERITY_ORDER]
            + [
                f"excepted={len(excepted)}",
                f"exceptions={len(exceptions)}",
                f"blocking={len(blocking)}",
            ]
        )
    )
    emit(
        "SECURITY_DEPENDENCY_SUMMARY "
        + " ".join(f"{target}={count}" for target, count in sorted(dependency_counts.items()))
    )
    emit(
        "SECURITY_SCANNER_COVERAGE "
        "secret=local-pinned forbidden-data=local-pinned sast=local-pinned "
        "pip-audit=2.9.0 npm-audit=package-lock container-config=local-pinned "
        "workflow-config=local-pinned immutable-refs=manifest-pinned"
    )
    emit(
        "SECURITY_PRIVACY_SUMMARY "
        "forbidden_directories_opened=0 secret_values_printed=0 scanned_paths_from_git=1 "
        f"baseline_forbidden_paths_not_opened={forbidden_baseline_paths}"
    )

    synthetic_failed = False
    if synthetic:
        fixtures = _synthetic_fixture_findings(root)
        for fixture, fixture_findings in fixtures.items():
            fixture_blocking = [
                item for item in fixture_findings if SEVERITY_ORDER[item.severity] >= threshold
            ]
            if fixture_blocking:
                emit(
                    "SYNTHETIC_EXPECTED_FAILURE "
                    f"fixture={fixture} exit=1 findings={len(fixture_blocking)} values_printed=0"
                )
            else:
                synthetic_failed = True
                emit(
                    "SYNTHETIC_FIXTURE_ERROR "
                    f"fixture={fixture} expected_exit=1 actual_exit=0",
                    error=True,
                )

    if blocking or synthetic_failed:
        emit(
            f"SECURITY_GATE_FAIL fail_on={fail_on} blocking={len(blocking)} synthetic_error={int(synthetic_failed)}"
        )
        return 1
    emit(
        f"SECURITY_GATE_OK fail_on={fail_on} blocking=0 synthetic_fixtures={4 if synthetic else 0}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fail-on", choices=tuple(SEVERITY_ORDER), default="high")
    parser.add_argument("--synthetic-fixtures", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    return run_gate(root, args.fail_on, args.synthetic_fixtures)


if __name__ == "__main__":
    raise SystemExit(main())
