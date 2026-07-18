#!/usr/bin/env python3
"""Fail-closed validation of the rendered one-shot principal provisioner service."""

from __future__ import annotations

import json
import re
import sys


SERVICE = "principal-provisioner"
ALLOWED_KEYS = {
    "cap_drop", "command", "cpus", "depends_on", "entrypoint", "environment", "image", "init",
    "labels", "logging", "mem_limit", "network_mode", "pids_limit", "profiles",
    "networks", "read_only", "restart", "security_opt", "tmpfs", "user", "volumes",
}


def validate(
    payload: dict,
    expected_image: str,
    expected_uid: str,
    expected_gid: str,
    expected_handoff_source: str,
) -> tuple[str, str]:
    services = payload.get("services")
    if not isinstance(services, dict) or set(services).isdisjoint({SERVICE}):
        raise ValueError("service_missing")
    service = services[SERVICE]
    if not isinstance(service, dict) or set(service) - ALLOWED_KEYS:
        raise ValueError("service_keys_unsafe")
    if "command" not in service or service["command"] is not None:
        raise ValueError("service_keys_unsafe")
    if service.get("image") != expected_image:
        raise ValueError("image_mismatch")
    service_networks = service.get("networks")
    if not isinstance(service_networks, dict) or set(service_networks) != {"principal-admin"}:
        raise ValueError("network_unsafe")
    networks = payload.get("networks")
    if not isinstance(networks, dict):
        raise ValueError("admin_network_missing")
    admin_network = networks.get("principal-admin", {})
    if not isinstance(admin_network, dict) or admin_network.get("external") is not True:
        raise ValueError("admin_network_not_external")
    project = payload.get("name")
    network_name = admin_network.get("name")
    if (
        not isinstance(project, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project)
        or not isinstance(network_name, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,126}", network_name)
    ):
        raise ValueError("project_or_network_name_invalid")
    for name, candidate in services.items():
        attached = candidate.get("networks", {}) if isinstance(candidate, dict) else {}
        if name not in {"postgres", SERVICE} and isinstance(attached, dict) and "principal-admin" in attached:
            raise ValueError("admin_network_service_leak")
    postgres_networks = services.get("postgres", {}).get("networks", {})
    if isinstance(postgres_networks, dict) and "principal-admin" in postgres_networks:
        raise ValueError("postgres_admin_network_persistent")
    if service.get("labels") not in (None, [], {}):
        raise ValueError("labels_unsafe")
    if service.get("restart") != "no" or service.get("ports") or service.get("expose"):
        raise ValueError("runtime_unsafe")
    if (
        service.get("init") is not True
        or service.get("read_only") is not True
        or service.get("cap_drop") != ["ALL"]
        or service.get("security_opt") != ["no-new-privileges:true"]
        or service.get("pids_limit") != 256
        or float(service.get("cpus", 0)) != 1.0
        or int(service.get("mem_limit", 0)) != 805306368
        or service.get("tmpfs") != ["/tmp:rw,noexec,nosuid,nodev,size=64m"]
    ):
        raise ValueError("hardening_mismatch")
    logging = service.get("logging")
    if not isinstance(logging, dict) or logging.get("driver") != "json-file" or logging.get("options") != {
        "max-file": "3", "max-size": "10m"
    }:
        raise ValueError("logging_mismatch")
    if service.get("user") != f"{expected_uid}:{expected_gid}":
        raise ValueError("user_mismatch")
    if service.get("entrypoint") != ["python", "-m", "app.principal_handoff"]:
        raise ValueError("entrypoint_mismatch")
    if service.get("profiles") != ["principal-admin"]:
        raise ValueError("profile_mismatch")
    depends_on = service.get("depends_on")
    if not isinstance(depends_on, dict) or set(depends_on) != {"postgres"}:
        raise ValueError("depends_on_mismatch")
    postgres_dependency = depends_on["postgres"]
    if not isinstance(postgres_dependency, dict) or postgres_dependency.get("condition") != "service_healthy":
        raise ValueError("depends_on_mismatch")
    environment = service.get("environment")
    if not isinstance(environment, dict) or set(environment) != {
        "DATABASE_URL",
        "TAKSKLAD_PRINCIPAL_COMMAND_APPROVAL",
        "TAKSKLAD_PRINCIPAL_HANDOFF_ROOT",
    }:
        raise ValueError("environment_unsafe")
    if not isinstance(environment.get("DATABASE_URL"), str) or not environment["DATABASE_URL"]:
        raise ValueError("database_environment_missing")
    if environment.get("TAKSKLAD_PRINCIPAL_HANDOFF_ROOT") != "/run/taksklad-private":
        raise ValueError("handoff_environment_invalid")
    volumes = service.get("volumes")
    if not isinstance(volumes, list) or len(volumes) != 1:
        raise ValueError("volumes_unsafe")
    by_target = {item.get("target"): item for item in volumes if isinstance(item, dict)}
    handoff = by_target.get("/run/taksklad-private")
    if (
        not handoff
        or handoff.get("type") != "bind"
        or handoff.get("source") != expected_handoff_source
        or handoff.get("read_only") is True
    ):
        raise ValueError("handoff_volume_unsafe")
    return project, network_name


def main(argv=None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) != 4:
        print("PRINCIPAL_COMPOSE_BLOCKED reason=usage", file=sys.stderr)
        return 2
    try:
        payload = json.load(sys.stdin)
        project, network_name = validate(payload, values[0], values[1], values[2], values[3])
    except (ValueError, json.JSONDecodeError, OSError):
        print("PRINCIPAL_COMPOSE_BLOCKED reason=unsafe_rendered_service", file=sys.stderr)
        return 1
    print(f"{project}|{network_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
