#!/usr/bin/env python3
"""Fail-closed static policy for the shipped container definitions.

The checker reads only committed Docker/Compose definitions. It never renders
Compose, follows env_file entries, or prints environment values.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Any

import yaml


APP_SERVICES = (
    "backend-api",
    "frontend",
    "skladbot-worker",
    "smartup-auto-import-worker",
    "telegram-worker",
    "adminer",
)

BACKEND_SERVICES = {
    "backend-api",
    "skladbot-worker",
    "smartup-auto-import-worker",
    "telegram-worker",
}

EXPECTED_SENSITIVE_NAMES = {
    "backend-api": {
        "DATABASE_URL",
        "SKLADBOT_DAILY_REPORT_CHAT_IDS",
        "TAKSKLAD_API_TOKEN",
        "TAKSKLAD_WEB_PASSWORD_HASH",
        "TAKSKLAD_WEB_SESSION_SECRET",
        "YANDEX_GEOCODER_API_KEY",
    },
    "frontend": set(),
    "skladbot-worker": {"DATABASE_URL", "SKLADBOT_API_TOKEN", "SKLADBOT_API_TOKENS"},
    "smartup-auto-import-worker": {
        "DATABASE_URL",
        "SKLADBOT_API_TOKEN",
        "SKLADBOT_API_TOKENS",
        "SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID",
        "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID",
        "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID",
        "SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY",
        "SMARTUP_PASSWORD",
        "SMARTUP_USERNAME",
        "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID",
        "TELEGRAM_ADMIN_CHAT_IDS",
        "TELEGRAM_BOT_TOKEN",
        "YANDEX_GEOCODER_API_KEY",
    },
    "telegram-worker": {
        "DATABASE_URL",
        "SKLADBOT_API_TOKEN",
        "SKLADBOT_API_TOKENS",
        "SKLADBOT_DAILY_REPORT_CHAT_IDS",
        "SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID",
        "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID",
        "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID",
        "TAKSKLAD_API_TOKEN",
        "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID",
        "TAKSKLAD_DAILY_RECONCILIATION_CHAT_IDS",
        "TELEGRAM_ADMIN_CHAT_IDS",
        "TELEGRAM_ALLOWED_CHAT_IDS",
        "TELEGRAM_BOT_TOKEN",
        "YANDEX_GEOCODER_API_KEY",
    },
    "adminer": set(),
}

ALLOWED_WRITABLE_TARGETS = {
    "backend-api": {"/tmp"},
    "frontend": {"/tmp", "/etc/nginx/conf.d", "/var/cache/nginx", "/run"},
    "skladbot-worker": {"/tmp"},
    "smartup-auto-import-worker": {"/tmp", "/app/outputs"},
    "telegram-worker": {"/tmp"},
    "adminer": {"/tmp"},
}

SENSITIVE_NAME_RE = re.compile(
    r"(?:^DATABASE_URL$|TOKEN|PASSWORD|SECRET|CREDENTIALS|API_KEY|ROUTE_FINGERPRINT_KEY|CHAT_IDS?$|SPREADSHEET_ID$|USERNAME$)"
)
MEMORY_RE = re.compile(r"^(?P<value>[1-9][0-9]*)(?P<unit>[kmgt]?)b?$", re.IGNORECASE)


@dataclass(frozen=True)
class PolicyRow:
    service: str
    uid: str
    writable: tuple[str, ...]
    sensitive_names: tuple[str, ...]
    limits: str


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot parse {path.name}: {type(exc).__name__}") from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("services"), dict):
        raise ValueError(f"{path.name} must contain a services mapping")
    return loaded


def _environment_names(service: dict[str, Any]) -> set[str]:
    environment = service.get("environment", {})
    if isinstance(environment, dict):
        return {str(name) for name in environment}
    if isinstance(environment, list):
        return {str(item).split("=", 1)[0] for item in environment}
    return set()


def _volume_target(raw: Any) -> str:
    if isinstance(raw, str):
        parts = raw.split(":")
        return parts[1] if len(parts) > 1 else parts[0]
    if isinstance(raw, dict):
        return str(raw.get("target", ""))
    return ""


def _volume_is_read_only(raw: Any) -> bool:
    if isinstance(raw, dict):
        return raw.get("read_only") is True
    if isinstance(raw, str):
        parts = raw.split(":")
        return len(parts) > 2 and "ro" in {option.strip() for option in parts[2].split(",")}
    return False


def _writable_targets(service: dict[str, Any]) -> set[str]:
    result = {str(item).split(":", 1)[0] for item in service.get("tmpfs", []) or []}
    result.update(
        _volume_target(item)
        for item in service.get("volumes", []) or []
        if not _volume_is_read_only(item)
    )
    result.discard("")
    return result


def _is_sensitive_name(name: str) -> bool:
    if name.endswith("_TOKEN_ROTATION_MAX_OVERLAP_SECONDS"):
        return False
    return SENSITIVE_NAME_RE.search(name) is not None


def _memory_bytes(value: Any) -> int:
    match = MEMORY_RE.fullmatch(str(value).strip())
    if not match:
        return 0
    multipliers = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    return int(match.group("value")) * multipliers[match.group("unit").lower()]


def parse_memory_bytes(value: Any) -> int:
    """Public testable wrapper for Compose memory values."""

    return _memory_bytes(value)


def _dockerfile_user(path: Path) -> str:
    user = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*USER\s+([^\s#]+)", line, flags=re.IGNORECASE)
        if match:
            user = match.group(1)
    return user


def _check_limits(name: str, service: dict[str, Any], errors: list[str]) -> str:
    try:
        cpus = float(service.get("cpus", 0))
    except (TypeError, ValueError):
        cpus = 0
    memory = _memory_bytes(service.get("mem_limit", ""))
    try:
        pids = int(service.get("pids_limit", 0))
    except (TypeError, ValueError):
        pids = 0
    if not 0 < cpus <= 2:
        errors.append(f"{name}: cpus must be in (0, 2]")
    if not 32 * 1024**2 <= memory <= 2 * 1024**3:
        errors.append(f"{name}: mem_limit must be between 32m and 2g")
    if not 0 < pids <= 512:
        errors.append(f"{name}: pids_limit must be in (0, 512]")
    logging = service.get("logging")
    options = logging.get("options", {}) if isinstance(logging, dict) else {}
    if not isinstance(logging, dict) or logging.get("driver") != "json-file":
        errors.append(f"{name}: json-file logging is required")
    if not options.get("max-size") or not options.get("max-file"):
        errors.append(f"{name}: bounded log rotation is required")
    return f"cpu={cpus:g},memory={service.get('mem_limit', '-')},pids={pids}"


def validate_repository(root: Path) -> tuple[list[str], list[PolicyRow]]:
    errors: list[str] = []
    rows: list[PolicyRow] = []
    try:
        vds = _load_yaml(root / "deploy/vds/docker-compose.yml")
        edge = _load_yaml(root / "deploy/traefik/docker-compose.yml")
    except ValueError as exc:
        return [str(exc)], []
    services = vds["services"]

    missing = sorted(set(APP_SERVICES) - set(services))
    if missing:
        errors.append(f"missing app services: {','.join(missing)}")
    unknown_built_services = sorted(
        name
        for name, service in services.items()
        if isinstance(service, dict) and service.get("build") is not None and name not in APP_SERVICES
    )
    if unknown_built_services:
        errors.append(f"unclassified built services: {','.join(unknown_built_services)}")

    backend_user = _dockerfile_user(root / "backend/Dockerfile")
    frontend_user = _dockerfile_user(root / "frontend/Dockerfile")
    if not re.fullmatch(r"[1-9][0-9]*(?::[1-9][0-9]*)?", backend_user):
        errors.append("backend/Dockerfile: final USER must be a non-root numeric uid")
    if not re.fullmatch(r"[1-9][0-9]*(?::[1-9][0-9]*)?", frontend_user):
        errors.append("frontend/Dockerfile: final USER must be a non-root numeric uid")

    for name in APP_SERVICES:
        raw = services.get(name)
        if not isinstance(raw, dict):
            continue
        uid = str(raw.get("user") or (backend_user if name in BACKEND_SERVICES else frontend_user if name == "frontend" else ""))
        if not uid or uid.split(":", 1)[0] == "0":
            errors.append(f"{name}: effective uid must be non-root")
        if raw.get("init") is not True:
            errors.append(f"{name}: init=true is required")
        if raw.get("read_only") is not True:
            errors.append(f"{name}: read_only=true is required")
        if "ALL" not in (raw.get("cap_drop") or []):
            errors.append(f"{name}: cap_drop must include ALL")
        security_options = {str(item).replace("=", ":") for item in (raw.get("security_opt") or [])}
        if "no-new-privileges:true" not in security_options:
            errors.append(f"{name}: no-new-privileges=true is required")
        if raw.get("env_file") is not None:
            errors.append(f"{name}: env_file is forbidden; configuration must be service-specific")
        if raw.get("ports"):
            errors.append(f"{name}: host ports are forbidden")

        writable = _writable_targets(raw)
        expected_writable = ALLOWED_WRITABLE_TARGETS[name]
        if writable != expected_writable:
            errors.append(
                f"{name}: writable targets differ expected={','.join(sorted(expected_writable))} "
                f"actual={','.join(sorted(writable))}"
            )

        sensitive = {item for item in _environment_names(raw) if _is_sensitive_name(item)}
        if sensitive != EXPECTED_SENSITIVE_NAMES[name]:
            errors.append(
                f"{name}: sensitive-name matrix differs expected={','.join(sorted(EXPECTED_SENSITIVE_NAMES[name]))} "
                f"actual={','.join(sorted(sensitive))}"
            )
        if name == "frontend" and any(
            marker in item for item in _environment_names(raw) for marker in ("TOKEN", "PASSWORD", "SECRET", "KEY")
        ):
            errors.append("frontend: secret-like environment names are forbidden")

        limits = _check_limits(name, raw, errors)
        rows.append(
            PolicyRow(
                service=name,
                uid=uid,
                writable=tuple(sorted(writable)),
                sensitive_names=tuple(sorted(sensitive)),
                limits=limits,
            )
        )

    postgres = services.get("postgres", {})
    if isinstance(postgres, dict):
        if str(postgres.get("user", "")).split(":", 1)[0] == "0" or not postgres.get("user"):
            errors.append("postgres: explicit non-root user is required")
        if postgres.get("ports"):
            errors.append("postgres: host port is forbidden")
        if postgres.get("read_only") is not True:
            errors.append("postgres: read_only=true is required")
        if "ALL" not in (postgres.get("cap_drop") or []):
            errors.append("postgres: cap_drop must include ALL")
        _check_limits("postgres", postgres, errors)

    edge_services = edge["services"]
    proxy = edge_services.get("docker-socket-proxy")
    traefik = edge_services.get("traefik")
    if not isinstance(proxy, dict) or not isinstance(traefik, dict):
        errors.append("edge: docker-socket-proxy and traefik services are required")
    else:
        proxy_volumes = [str(item) for item in proxy.get("volumes", []) or []]
        if proxy_volumes != ["/var/run/docker.sock:/var/run/docker.sock:ro"]:
            errors.append("docker-socket-proxy: the only socket mount must be read-only")
        if traefik.get("volumes") and any("docker.sock" in str(item) for item in traefik["volumes"]):
            errors.append("traefik: direct Docker socket mount is forbidden")
        proxy_env = proxy.get("environment", {})
        if not isinstance(proxy_env, dict):
            proxy_env = {}
        required_allow = {"PING", "VERSION", "EVENTS", "CONTAINERS", "NETWORKS"}
        required_deny = {
            "POST",
            "AUTH",
            "EXEC",
            "IMAGES",
            "SECRETS",
            "SERVICES",
            "SWARM",
            "SYSTEM",
            "TASKS",
            "VOLUMES",
        }
        if any(str(proxy_env.get(name)) != "1" for name in required_allow):
            errors.append("docker-socket-proxy: required read endpoints are not enabled")
        if any(str(proxy_env.get(name)) != "0" for name in required_deny):
            errors.append("docker-socket-proxy: write/sensitive endpoints must be disabled")
        if proxy.get("ports"):
            errors.append("docker-socket-proxy: host ports are forbidden")
        docker_api_network = edge.get("networks", {}).get("docker-api", {})
        if not isinstance(docker_api_network, dict) or docker_api_network.get("internal") is not True:
            errors.append("docker-api network must be internal")
        endpoint = str((traefik.get("environment") or {}).get("DOCKER_HOST", ""))
        if endpoint != "tcp://docker-socket-proxy:2375":
            errors.append("traefik: DOCKER_HOST must point to the restricted socket proxy")
        _check_limits("docker-socket-proxy", proxy, errors)
        _check_limits("traefik", traefik, errors)

    return errors, rows


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--strict", action="store_true", help="fail on every policy error")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    errors, rows = validate_repository(args.root.resolve())
    for row in rows:
        print(
            "CONTAINER_POLICY_SERVICE "
            f"service={row.service} uid={row.uid} rootfs=read-only caps=drop-all nnp=1 "
            f"writable={','.join(row.writable) or 'none'} "
            f"secret_names={','.join(row.sensitive_names) or 'none'} {row.limits}"
        )
    if errors:
        for error in errors:
            print(f"CONTAINER_POLICY_ERROR: {error}", file=sys.stderr)
        return 1
    print(
        "CONTAINER_POLICY_OK "
        f"app_services={len(rows)} socket_proxy=restricted host_ports=0 forbidden_env_files=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
