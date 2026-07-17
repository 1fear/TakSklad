#!/usr/bin/env python3
"""Prepare an exact, fail-closed notification routing env candidate.

Runtime values are inspected only to reject unknown routes. The versioned
manifest is the only authority for destinations and schedules. No value is
printed, and unrelated env lines are preserved byte-for-byte.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
from typing import Iterable, Mapping

from backend.app.telegram_routing_contract import (
    ROUTING_IDENTITY_ANCHOR_ENV,
    TelegramMessageKind,
    TelegramRoutingContractError,
    load_telegram_routing_contract,
    validate_route_identity_anchor,
    validate_route_values,
)


FINGERPRINT_KEY_MIN_LENGTH = 32
ENV_ASSIGNMENT_RE = re.compile(r"^(?P<prefix>\s*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*?)(?P<newline>\r\n|\n|\r)?$")


def is_unrelated_auth_setting(setting_name: str) -> bool:
    normalized = str(setting_name or "").strip().upper()
    if not normalized.startswith("TAKSKLAD_"):
        return False
    return (
        "AUTH" in normalized
        or "TOKEN" in normalized
        or "PASSWORD" in normalized
        or "SESSION" in normalized
        or "COOKIE" in normalized
        or normalized == "TAKSKLAD_INSECURE_LOCAL_ANONYMOUS"
        or normalized == "TAKSKLAD_TRUSTED_PROXY_CIDRS"
    )


def require_notification_only_updates(updates: Mapping[str, str]) -> None:
    blocked = sorted(key for key in updates if is_unrelated_auth_setting(key))
    if blocked:
        raise NotificationRoutingConfigError(
            "notification routing update contains unrelated auth configuration"
        )


class NotificationRoutingConfigError(RuntimeError):
    """Raised without embedding sensitive configuration values."""


@dataclass(frozen=True)
class RoutingPreparation:
    updates: dict[str, str]
    repaired_route_roles: tuple[str, ...]
    slot_count: int

    def safe_summary(self) -> dict[str, object]:
        return {
            "status": "ok",
            "contract_version": 1,
            "values_redacted": True,
            "updated_field_count": len(self.updates),
            "route_aliases": ["admin", "client", "logistics"],
            "repaired_route_roles": list(self.repaired_route_roles),
            "slot_count": self.slot_count,
            "raw_chat_ids_redacted": True,
        }


def csv_items(value: object) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            part.strip()
            for part in str(value or "").replace(";", ",").split(",")
            if part.strip()
        )
    )


def parse_env_assignments(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        stripped = raw_line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        match = ENV_ASSIGNMENT_RE.match(raw_line)
        if match is None:
            continue
        key = match.group("key")
        value = match.group("value").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[key] = value
    return result


def duplicate_env_keys(text: str) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for raw_line in text.splitlines():
        if raw_line.lstrip().startswith("#"):
            continue
        match = ENV_ASSIGNMENT_RE.match(raw_line)
        if match is None:
            continue
        key = match.group("key")
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return duplicates


def container_env_mapping(entries: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in entries:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        result[key] = value
    return result


def inspect_container_env(container_id: str) -> dict[str, str]:
    import subprocess

    if not str(container_id or "").strip():
        raise NotificationRoutingConfigError("runtime container id is missing")
    try:
        raw = subprocess.check_output(
            ["docker", "inspect", "--format", "{{json .Config.Env}}", container_id],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        entries = json.loads(raw)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise NotificationRoutingConfigError("cannot inspect runtime container environment") from exc
    if not isinstance(entries, list) or any(not isinstance(item, str) for item in entries):
        raise NotificationRoutingConfigError("runtime container environment has invalid shape")
    return container_env_mapping(entries)


def _validate_runtime_matches_operator_config(
    telegram_env: Mapping[str, str],
    smartup_env: Mapping[str, str],
    persisted_env: Mapping[str, str],
) -> dict[str, str]:
    operator_routes = {
        "client": str(persisted_env.get("SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID") or "").strip(),
        "logistics": str(persisted_env.get("SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID") or "").strip(),
        "admin": str(persisted_env.get("TAKSKLAD_AUTOMATION_ALERT_CHAT_ID") or "").strip(),
    }
    route_errors = validate_route_values(
        operator_routes["client"], operator_routes["logistics"], operator_routes["admin"]
    )
    if route_errors:
        raise NotificationRoutingConfigError(
            "operator routing config is missing, invalid or has a role collision"
        )
    runtime_role_settings = {
        "client": ("SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID",),
        "logistics": ("SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID",),
        "admin": ("TAKSKLAD_AUTOMATION_ALERT_CHAT_ID",),
    }
    for role, settings in runtime_role_settings.items():
        observed = {
            str(source.get(setting) or "").strip()
            for source in (telegram_env, smartup_env)
            for setting in settings
            if str(source.get(setting) or "").strip()
        }
        if observed and observed != {operator_routes[role]}:
            raise NotificationRoutingConfigError(f"{role} route: runtime disagrees with operator config")

    known_ids = set(operator_routes.values())
    for setting in (
        "TELEGRAM_ALLOWED_CHAT_IDS",
        "TELEGRAM_ADMIN_CHAT_IDS",
        "SKLADBOT_DAILY_REPORT_CHAT_IDS",
    ):
        observed = {
            value
            for source in (telegram_env, smartup_env)
            for value in csv_items(source.get(setting))
        }
        if observed - known_ids:
            raise NotificationRoutingConfigError(f"{setting}: unknown runtime route")
    legacy_values = {
        str(source.get("SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID") or "").strip()
        for source in (smartup_env, persisted_env)
        if str(source.get("SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID") or "").strip()
    }
    if legacy_values:
        raise NotificationRoutingConfigError(
            "SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID: legacy route must be removed by operator"
        )
    return operator_routes


def prepare_notification_routing(
    telegram_env: Mapping[str, str],
    smartup_env: Mapping[str, str],
    persisted_env: Mapping[str, str],
    expected_identity_anchor_sha256: object,
) -> RoutingPreparation:
    try:
        contract = load_telegram_routing_contract()
        operator_routes = _validate_runtime_matches_operator_config(
            telegram_env, smartup_env, persisted_env
        )
        validate_route_identity_anchor(
            operator_routes["client"],
            operator_routes["logistics"],
            operator_routes["admin"],
            expected_identity_anchor_sha256,
        )
    except TelegramRoutingContractError as exc:
        raise NotificationRoutingConfigError(str(exc)) from exc

    fingerprint_values = {
        str(source.get("SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY") or "").strip()
        for source in (smartup_env, persisted_env)
        if str(source.get("SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY") or "").strip()
    }
    if len(fingerprint_values) != 1:
        raise NotificationRoutingConfigError(
            "SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY: one stable existing key is required"
        )
    fingerprint_key = next(iter(fingerprint_values))
    if len(fingerprint_key) < FINGERPRINT_KEY_MIN_LENGTH or any(char.isspace() for char in fingerprint_key):
        raise NotificationRoutingConfigError(
            "SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY: key is too weak or malformed"
        )

    client = operator_routes["client"]
    logistics = operator_routes["logistics"]
    admin = operator_routes["admin"]
    client_slots = contract.route_for(TelegramMessageKind.SMARTUP_CLIENT_EXPORT).schedules
    logistics_slots = contract.route_for(TelegramMessageKind.SMARTUP_LOGISTICS_REPORT).schedules
    updates = {
        "TELEGRAM_ALLOWED_CHAT_IDS": ",".join((client, logistics, admin)),
        "TELEGRAM_ADMIN_CHAT_IDS": admin,
        "SKLADBOT_DAILY_REPORT_CHAT_IDS": client,
        "SKLADBOT_DAILY_REPORT_HOUR": "22",
        "SKLADBOT_DAILY_REPORT_MINUTE": "0",
        "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": admin,
        "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID": client,
        "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID": logistics,
        "SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID": "",
        "SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY": fingerprint_key,
        "SMARTUP_AUTO_IMPORT_TIMES": ",".join(client_slots),
        "SMARTUP_AUTO_IMPORT_FINAL_TIME": logistics_slots[0],
        "SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME": logistics_slots[0],
    }
    return RoutingPreparation(
        updates=updates,
        repaired_route_roles=(),
        slot_count=len(client_slots),
    )


def render_env_candidate(source_text: str, updates: Mapping[str, str]) -> str:
    require_notification_only_updates(updates)
    duplicates = duplicate_env_keys(source_text) & set(updates)
    if duplicates:
        raise NotificationRoutingConfigError("routing env contains duplicate managed keys")
    if any("\n" in str(value) or "\r" in str(value) for value in updates.values()):
        raise NotificationRoutingConfigError("multiline env value is forbidden")

    output: list[str] = []
    written: set[str] = set()
    for raw_line in source_text.splitlines(keepends=True):
        if raw_line.lstrip().startswith("#"):
            output.append(raw_line)
            continue
        match = ENV_ASSIGNMENT_RE.match(raw_line)
        if match is None or match.group("key") not in updates:
            output.append(raw_line)
            continue
        key = match.group("key")
        newline = match.group("newline") or ""
        output.append(f"{match.group('prefix')}{key}={updates[key]}{newline}")
        written.add(key)

    missing = [key for key in updates if key not in written]
    if missing:
        newline = "\r\n" if "\r\n" in source_text else "\n"
        rendered = "".join(output)
        if rendered and not rendered.endswith(("\n", "\r")):
            rendered += newline
        rendered += "".join(f"{key}={updates[key]}{newline}" for key in missing)
        return rendered
    return "".join(output)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-path", required=True)
    parser.add_argument("--candidate-path", required=True)
    parser.add_argument("--telegram-container-id", required=True)
    parser.add_argument("--smartup-container-id", required=True)
    return parser.parse_args(argv)


def write_candidate_file(candidate_path: Path, payload: bytes) -> None:
    parent = candidate_path.parent
    try:
        parent_stat = parent.stat()
    except OSError as exc:
        raise NotificationRoutingConfigError("candidate state directory is unavailable") from exc
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent.is_symlink()
        or stat.S_IMODE(parent_stat.st_mode) != 0o700
    ):
        raise NotificationRoutingConfigError("candidate state directory must be a protected mode-700 directory")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    created = False
    try:
        descriptor = os.open(candidate_path, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
        os.chmod(candidate_path, 0o600, follow_symlinks=False)
    except OSError as exc:
        if created:
            try:
                candidate_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise NotificationRoutingConfigError("candidate file could not be created safely") from exc


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env_path = Path(args.env_path)
    candidate_path = Path(args.candidate_path)
    try:
        source_text = env_path.read_bytes().decode("utf-8")
        persisted_env = parse_env_assignments(source_text)
        if ROUTING_IDENTITY_ANCHOR_ENV in persisted_env:
            raise NotificationRoutingConfigError(
                "protected routing identity anchor must not come from persisted env"
            )
        preparation = prepare_notification_routing(
            inspect_container_env(args.telegram_container_id),
            inspect_container_env(args.smartup_container_id),
            persisted_env,
            os.environ.get(ROUTING_IDENTITY_ANCHOR_ENV),
        )
        write_candidate_file(
            candidate_path,
            render_env_candidate(source_text, preparation.updates).encode("utf-8"),
        )
    except (OSError, NotificationRoutingConfigError) as exc:
        print(f"NOTIFICATION_ROUTING_CANDIDATE_BLOCKED reason={exc}")
        return 1
    print(
        "NOTIFICATION_ROUTING_CANDIDATE_READY "
        + json.dumps(preparation.safe_summary(), sort_keys=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
