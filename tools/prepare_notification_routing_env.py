#!/usr/bin/env python3
"""Prepare a fail-closed production notification routing env candidate.

The tool reads the current Telegram and Smartup container environments, but
never prints their values. It only writes a candidate env file after proving
that runtime has exactly one personal admin route and exactly one report group.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from typing import Iterable, Mapping


CHAT_ID_RE = re.compile(r"-?[1-9]\d*")
TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
FINGERPRINT_KEY_MIN_LENGTH = 32


class NotificationRoutingConfigError(RuntimeError):
    """Raised without embedding sensitive configuration values."""


@dataclass(frozen=True)
class RoutingPreparation:
    updates: dict[str, str]
    repaired_personal_logistics_route: bool
    generated_fingerprint_key: bool
    slot_count: int

    def safe_summary(self) -> dict[str, object]:
        return {
            "status": "ok",
            "values_redacted": True,
            "updated_field_count": len(self.updates),
            "admin_route_count": 1,
            "report_group_count": 1,
            "slot_count": self.slot_count,
            "repaired_personal_logistics_route": self.repaired_personal_logistics_route,
            "generated_fingerprint_key": self.generated_fingerprint_key,
            "recovery_enabled": bool(
                self.updates.get("SMARTUP_AUTO_IMPORT_LOGISTICS_ROUTE_RECOVERY_EXPORT_DATE")
            ),
        }


def csv_items(value: object) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            part.strip()
            for part in str(value or "").replace(";", ",").split(",")
            if part.strip()
        )
    )


def require_chat_ids(values: Iterable[str], setting_name: str) -> tuple[str, ...]:
    normalized = tuple(values)
    if not normalized or any(not CHAT_ID_RE.fullmatch(value) for value in normalized):
        raise NotificationRoutingConfigError(f"{setting_name}: invalid or missing chat route")
    return normalized


def parse_schedule(value: object) -> tuple[str, ...]:
    slots = csv_items(value)
    if len(slots) != 3 or len(set(slots)) != 3 or any(not TIME_RE.fullmatch(slot) for slot in slots):
        raise NotificationRoutingConfigError(
            "SMARTUP_AUTO_IMPORT_TIMES: production requires exactly three unique HH:MM slots"
        )
    return slots


def parse_env_assignments(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        stripped = raw_line.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[key] = value
    return result


def container_env_mapping(entries: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in entries:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        result[key] = value
    return result


def inspect_container_env(container_id: str) -> dict[str, str]:
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


def _single_runtime_value(
    setting_name: str,
    *sources: Mapping[str, str],
    allow_empty: bool = True,
) -> str:
    values = {
        str(source.get(setting_name) or "").strip()
        for source in sources
        if str(source.get(setting_name) or "").strip()
    }
    if len(values) > 1:
        raise NotificationRoutingConfigError(f"{setting_name}: runtime values disagree")
    value = next(iter(values), "")
    if not value and not allow_empty:
        raise NotificationRoutingConfigError(f"{setting_name}: runtime value is missing")
    return value


def prepare_notification_routing(
    telegram_env: Mapping[str, str],
    smartup_env: Mapping[str, str],
    persisted_env: Mapping[str, str],
    *,
    recovery_export_date: str,
) -> RoutingPreparation:
    try:
        date.fromisoformat(recovery_export_date)
    except ValueError as exc:
        raise NotificationRoutingConfigError("recovery export date must be YYYY-MM-DD") from exc

    allowed = require_chat_ids(
        csv_items(telegram_env.get("TELEGRAM_ALLOWED_CHAT_IDS")),
        "TELEGRAM_ALLOWED_CHAT_IDS",
    )
    admins = require_chat_ids(
        csv_items(telegram_env.get("TELEGRAM_ADMIN_CHAT_IDS")),
        "TELEGRAM_ADMIN_CHAT_IDS",
    )
    daily_routes = require_chat_ids(
        csv_items(telegram_env.get("SKLADBOT_DAILY_REPORT_CHAT_IDS")),
        "SKLADBOT_DAILY_REPORT_CHAT_IDS",
    )

    personal_admins = tuple(value for value in admins if int(value) > 0)
    if len(admins) != 1 or len(personal_admins) != 1:
        raise NotificationRoutingConfigError(
            "TELEGRAM_ADMIN_CHAT_IDS: production requires exactly one personal admin route"
        )
    admin_route = personal_admins[0]
    if admin_route not in allowed:
        raise NotificationRoutingConfigError(
            "TELEGRAM_ADMIN_CHAT_IDS: personal admin route is outside allowlist"
        )

    client_route = str(smartup_env.get("SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID") or "").strip()
    logistics_route = str(smartup_env.get("SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID") or "").strip()
    for setting_name, value in (
        ("SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID", client_route),
        ("SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID", logistics_route),
    ):
        if value and not CHAT_ID_RE.fullmatch(value):
            raise NotificationRoutingConfigError(f"{setting_name}: invalid runtime route")

    group_candidates = {
        value
        for value in (*allowed, *daily_routes, client_route, logistics_route)
        if value and CHAT_ID_RE.fullmatch(value) and int(value) < 0
    }
    if len(group_candidates) != 1:
        raise NotificationRoutingConfigError(
            "notification routing is ambiguous: expected exactly one report group"
        )
    report_group = next(iter(group_candidates))
    if set(allowed) != {admin_route, report_group}:
        raise NotificationRoutingConfigError(
            "TELEGRAM_ALLOWED_CHAT_IDS: unexpected routes require operator review"
        )
    if set(daily_routes) != {report_group}:
        raise NotificationRoutingConfigError(
            "SKLADBOT_DAILY_REPORT_CHAT_IDS: expected exactly the proven report group"
        )
    if client_route != report_group:
        raise NotificationRoutingConfigError(
            "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID: expected the proven report group"
        )
    if logistics_route not in {admin_route, report_group}:
        raise NotificationRoutingConfigError(
            "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID: route cannot be repaired safely"
        )
    repaired_personal_logistics_route = logistics_route == admin_route

    for setting_name in (
        "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID",
        "SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID",
    ):
        configured = _single_runtime_value(
            setting_name,
            telegram_env,
            smartup_env,
            allow_empty=True,
        )
        if configured and configured != admin_route:
            raise NotificationRoutingConfigError(
                f"{setting_name}: alert route disagrees with the proven personal admin"
            )

    slots = parse_schedule(smartup_env.get("SMARTUP_AUTO_IMPORT_TIMES"))
    final_time = str(smartup_env.get("SMARTUP_AUTO_IMPORT_FINAL_TIME") or "").strip()
    if final_time not in slots:
        raise NotificationRoutingConfigError(
            "SMARTUP_AUTO_IMPORT_FINAL_TIME: final time must be one of the three slots"
        )
    logistics_due_time = str(
        smartup_env.get("SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME") or final_time
    ).strip()
    if not TIME_RE.fullmatch(logistics_due_time):
        raise NotificationRoutingConfigError(
            "SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME: expected HH:MM"
        )

    recovery_markers = {
        str(source.get("SMARTUP_AUTO_IMPORT_LOGISTICS_ROUTE_RECOVERY_EXPORT_DATE") or "").strip()
        for source in (smartup_env, persisted_env)
        if str(source.get("SMARTUP_AUTO_IMPORT_LOGISTICS_ROUTE_RECOVERY_EXPORT_DATE") or "").strip()
    }
    if recovery_markers - {recovery_export_date}:
        raise NotificationRoutingConfigError(
            "SMARTUP_AUTO_IMPORT_LOGISTICS_ROUTE_RECOVERY_EXPORT_DATE: "
            "existing recovery date requires operator review"
        )
    recovery_enabled = repaired_personal_logistics_route or bool(recovery_markers)

    fingerprint_values = {
        str(source.get("SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY") or "").strip()
        for source in (telegram_env, smartup_env, persisted_env)
        if str(source.get("SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY") or "").strip()
    }
    if len(fingerprint_values) > 1:
        raise NotificationRoutingConfigError(
            "SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY: persisted and runtime values disagree"
        )
    generated_fingerprint_key = not fingerprint_values
    fingerprint_key = next(iter(fingerprint_values), "") or secrets.token_hex(32)
    if len(fingerprint_key) < FINGERPRINT_KEY_MIN_LENGTH or any(char.isspace() for char in fingerprint_key):
        raise NotificationRoutingConfigError(
            "SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY: key is too weak or malformed"
        )

    updates = {
        "TAKSKLAD_LEGACY_AUTH_EXPIRES_AT": "2026-07-17T00:00:00+00:00",
        "TELEGRAM_ALLOWED_CHAT_IDS": ",".join(allowed),
        "TELEGRAM_ADMIN_CHAT_IDS": admin_route,
        "SKLADBOT_DAILY_REPORT_CHAT_IDS": report_group,
        "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": admin_route,
        "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID": report_group,
        "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID": report_group,
        "SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID": admin_route,
        "SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY": fingerprint_key,
        "SMARTUP_AUTO_IMPORT_ENABLED": "true",
        "SMARTUP_AUTO_IMPORT_BACKEND_IMPORT_ENABLED": "true",
        "SMARTUP_AUTO_IMPORT_CHANGE_STATUS_ENABLED": "true",
        "SKLADBOT_CREATE_REQUESTS_MODE": "enabled",
        "SMARTUP_AUTO_IMPORT_TIMES": ",".join(slots),
        "SMARTUP_AUTO_IMPORT_FINAL_TIME": final_time,
        "SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME": logistics_due_time,
        "SMARTUP_AUTO_IMPORT_LOGISTICS_RETRY_BASE_SECONDS": "60",
        "SMARTUP_AUTO_IMPORT_LOGISTICS_MAX_ATTEMPTS": "5",
        "SMARTUP_AUTO_IMPORT_LOGISTICS_CATCHUP_DAYS": "2",
        "SMARTUP_AUTO_IMPORT_LOGISTICS_CLAIM_TIMEOUT_MINUTES": "10",
        "SMARTUP_AUTO_IMPORT_LOGISTICS_ROUTE_RECOVERY_EXPORT_DATE": (
            recovery_export_date if recovery_enabled else ""
        ),
        "SMARTUP_AUTO_IMPORT_SAGA_MODE": "disabled",
        "SMARTUP_AUTO_IMPORT_PROCESS_SKLADBOT_NOW": "false",
    }
    return RoutingPreparation(
        updates=updates,
        repaired_personal_logistics_route=repaired_personal_logistics_route,
        generated_fingerprint_key=generated_fingerprint_key,
        slot_count=len(slots),
    )


def render_env_candidate(source_text: str, updates: Mapping[str, str]) -> str:
    output: list[str] = []
    written: set[str] = set()
    for raw_line in source_text.splitlines():
        stripped = raw_line.lstrip()
        key = raw_line.split("=", 1)[0].strip() if "=" in raw_line and not stripped.startswith("#") else ""
        if key not in updates:
            output.append(raw_line)
            continue
        if key in written:
            continue
        value = str(updates[key])
        if "\n" in value or "\r" in value:
            raise NotificationRoutingConfigError(f"{key}: multiline env value is forbidden")
        output.append(f"{key}={value}")
        written.add(key)
    for key, value in updates.items():
        if key not in written:
            output.append(f"{key}={value}")
    return "\n".join(output) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-path", required=True)
    parser.add_argument("--candidate-path", required=True)
    parser.add_argument("--telegram-container-id", required=True)
    parser.add_argument("--smartup-container-id", required=True)
    parser.add_argument("--recovery-export-date", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env_path = Path(args.env_path)
    candidate_path = Path(args.candidate_path)
    try:
        source_text = env_path.read_text(encoding="utf-8")
        preparation = prepare_notification_routing(
            inspect_container_env(args.telegram_container_id),
            inspect_container_env(args.smartup_container_id),
            parse_env_assignments(source_text),
            recovery_export_date=args.recovery_export_date,
        )
        candidate_path.write_text(
            render_env_candidate(source_text, preparation.updates),
            encoding="utf-8",
        )
        os.chmod(candidate_path, 0o600)
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
