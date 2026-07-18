"""Versioned fail-closed Telegram routing contract for production automation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping


MANIFEST_PATH = Path(__file__).with_name("telegram_routing_manifest.json")
CHAT_ID_RE = re.compile(r"-?[1-9]\d*")
TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
EXPECTED_ROLES = ("client", "logistics", "admin")
ROUTING_IDENTITY_ANCHOR_ENV = "TAKSKLAD_TELEGRAM_ROUTING_IDENTITY_ANCHOR_SHA256"
ROUTING_IDENTITY_ANCHOR_RE = re.compile(r"[0-9a-f]{64}")


class TelegramRoutingContractError(RuntimeError):
    """Raised without embedding raw route values."""


class TelegramMessageKind(str, Enum):
    SMARTUP_CLIENT_EXPORT = "smartup_client_export"
    SMARTUP_LOGISTICS_REPORT = "smartup_logistics_report"
    SKLADBOT_DAILY_REPORT = "skladbot_daily_report"
    TRANSFER_KIZ_EXPORT = "transfer_kiz_export"
    ADMIN_ERROR = "admin_error"


@dataclass(frozen=True)
class TelegramRoute:
    kind: TelegramMessageKind
    destination: str
    schedules: tuple[str, ...]
    text_policy_label: str
    text_policy_sha256: str
    error_destination: str


@dataclass(frozen=True)
class TelegramRoutingContract:
    schema_version: int
    timezone: str
    roles: Mapping[str, Mapping[str, str]]
    message_kinds: Mapping[str, Mapping[str, Any]]
    notification_kind_aliases: Mapping[str, str]

    def route_for(self, kind: TelegramMessageKind | str) -> TelegramRoute:
        try:
            typed_kind = kind if isinstance(kind, TelegramMessageKind) else TelegramMessageKind(str(kind))
        except ValueError as exc:
            raise TelegramRoutingContractError("unknown Telegram message kind") from exc
        spec = self.message_kinds.get(typed_kind.value)
        if not isinstance(spec, Mapping):
            raise TelegramRoutingContractError("Telegram message kind is absent from manifest")
        destination = str(spec["destination"])
        return TelegramRoute(
            kind=typed_kind,
            destination=destination,
            schedules=tuple(str(value) for value in spec["schedules"]),
            text_policy_label=str(spec["text_policy_label"]),
            text_policy_sha256=str(spec["text_policy_sha256"]),
            error_destination=str(spec["error_destination"]),
        )

    def route_for_notification_kind(self, payload_kind: object) -> TelegramRoute:
        canonical = self.notification_kind_aliases.get(str(payload_kind or ""))
        if canonical != TelegramMessageKind.ADMIN_ERROR.value:
            raise TelegramRoutingContractError("unknown queued Telegram notification kind")
        return self.route_for(TelegramMessageKind.ADMIN_ERROR)

    def safe_matrix(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "timezone": self.timezone,
            "roles": {
                role: {
                    "alias": role,
                    "target_type": self.roles[role]["target_type"],
                }
                for role in EXPECTED_ROLES
            },
            "message_kinds": {
                kind.value: {
                    "destination_alias": self.route_for(kind).destination,
                    "schedules": list(self.route_for(kind).schedules),
                    "text_policy_label": self.route_for(kind).text_policy_label,
                    "text_policy_sha256": self.route_for(kind).text_policy_sha256,
                    "error_destination_alias": self.route_for(kind).error_destination,
                }
                for kind in TelegramMessageKind
            },
            "raw_chat_ids_redacted": True,
        }


def _require_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TelegramRoutingContractError(f"{field}: expected object")
    return value


def _validate_manifest(payload: Mapping[str, Any]) -> TelegramRoutingContract:
    if payload.get("schema_version") != 1:
        raise TelegramRoutingContractError("schema_version: unsupported Telegram routing manifest")
    timezone = str(payload.get("timezone") or "").strip()
    if timezone != "Asia/Tashkent":
        raise TelegramRoutingContractError("timezone: unexpected Telegram routing timezone")

    roles = _require_mapping(payload.get("roles"), "roles")
    if set(roles) != set(EXPECTED_ROLES):
        raise TelegramRoutingContractError("roles: expected client, logistics and admin only")
    for role in EXPECTED_ROLES:
        spec = _require_mapping(roles[role], f"roles.{role}")
        target_type = str(spec.get("target_type") or "").strip()
        setting = str(spec.get("setting") or "").strip()
        if set(spec) != {"target_type", "setting"} or not setting:
            raise TelegramRoutingContractError(f"roles.{role}: exact policy fields required")
        if role == "admin" and target_type != "personal":
            raise TelegramRoutingContractError("roles.admin: personal route required")
        if role != "admin" and target_type != "group":
            raise TelegramRoutingContractError(f"roles.{role}: group route required")

    message_kinds = _require_mapping(payload.get("message_kinds"), "message_kinds")
    if set(message_kinds) != {kind.value for kind in TelegramMessageKind}:
        raise TelegramRoutingContractError("message_kinds: exact typed manifest entries required")
    for kind in TelegramMessageKind:
        spec = _require_mapping(message_kinds[kind.value], f"message_kinds.{kind.value}")
        if set(spec) != {
            "destination", "schedules", "text_policy_label", "text_policy_sha256", "error_destination",
        }:
            raise TelegramRoutingContractError(f"message_kinds.{kind.value}: unexpected fields")
        destination = str(spec.get("destination") or "")
        error_destination = str(spec.get("error_destination") or "")
        schedules = spec.get("schedules")
        if destination not in EXPECTED_ROLES or error_destination != "admin":
            raise TelegramRoutingContractError(f"message_kinds.{kind.value}: invalid destination")
        if not isinstance(schedules, list) or not schedules or len(set(schedules)) != len(schedules):
            raise TelegramRoutingContractError(f"message_kinds.{kind.value}: invalid schedules")
        allowed_non_time_schedules = {
            TelegramMessageKind.TRANSFER_KIZ_EXPORT.value: {"on_completion"},
            TelegramMessageKind.ADMIN_ERROR.value: {"on_error"},
        }
        allowed_non_time = allowed_non_time_schedules.get(kind.value, set())
        if any(
            str(value) not in allowed_non_time and not TIME_RE.fullmatch(str(value))
            for value in schedules
        ):
            raise TelegramRoutingContractError(f"message_kinds.{kind.value}: invalid schedule")
        if not str(spec.get("text_policy_label") or "").strip():
            raise TelegramRoutingContractError(f"message_kinds.{kind.value}: missing text contract")
        if not re.fullmatch(r"[0-9a-f]{64}", str(spec.get("text_policy_sha256") or "")):
            raise TelegramRoutingContractError(f"message_kinds.{kind.value}: invalid text policy hash")

    expected_schedules = {
        TelegramMessageKind.SMARTUP_CLIENT_EXPORT.value: ["12:00", "15:00", "17:50"],
        TelegramMessageKind.SMARTUP_LOGISTICS_REPORT.value: ["17:50"],
        TelegramMessageKind.SKLADBOT_DAILY_REPORT.value: ["22:00"],
        TelegramMessageKind.TRANSFER_KIZ_EXPORT.value: ["on_completion"],
        TelegramMessageKind.ADMIN_ERROR.value: ["on_error"],
    }
    for kind, schedules in expected_schedules.items():
        if message_kinds[kind]["schedules"] != schedules:
            raise TelegramRoutingContractError(f"message_kinds.{kind}: exact schedule contract required")
    if message_kinds[TelegramMessageKind.SMARTUP_CLIENT_EXPORT.value]["destination"] != "client":
        raise TelegramRoutingContractError("smartup client export must use client route")
    if message_kinds[TelegramMessageKind.SMARTUP_LOGISTICS_REPORT.value]["destination"] != "logistics":
        raise TelegramRoutingContractError("logistics report must use logistics route")
    if message_kinds[TelegramMessageKind.SKLADBOT_DAILY_REPORT.value]["destination"] != "client":
        raise TelegramRoutingContractError("daily report must use client route")
    if message_kinds[TelegramMessageKind.TRANSFER_KIZ_EXPORT.value]["destination"] != "client":
        raise TelegramRoutingContractError("transfer KIZ export must use client route")
    if message_kinds[TelegramMessageKind.ADMIN_ERROR.value]["destination"] != "admin":
        raise TelegramRoutingContractError("admin errors must use admin route")

    aliases = _require_mapping(payload.get("notification_kind_aliases"), "notification_kind_aliases")
    if not aliases or any(value != TelegramMessageKind.ADMIN_ERROR.value for value in aliases.values()):
        raise TelegramRoutingContractError("notification aliases must resolve only to admin_error")
    return TelegramRoutingContract(
        schema_version=1,
        timezone=timezone,
        roles=roles,
        message_kinds=message_kinds,
        notification_kind_aliases={str(key): str(value) for key, value in aliases.items()},
    )


@lru_cache(maxsize=1)
def load_telegram_routing_contract() -> TelegramRoutingContract:
    try:
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TelegramRoutingContractError("Telegram routing manifest is unreadable") from exc
    return _validate_manifest(_require_mapping(payload, "manifest"))


def csv_values(value: object) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            part.strip()
            for part in str(value or "").replace(";", ",").split(",")
            if part.strip()
        )
    )


def validate_route_values(client: object, logistics: object, admin: object) -> list[str]:
    values = {
        "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID": str(client or "").strip(),
        "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID": str(logistics or "").strip(),
        "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": str(admin or "").strip(),
    }
    errors = [setting for setting, value in values.items() if not CHAT_ID_RE.fullmatch(value)]
    if values["SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID"] and not values["SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID"].startswith("-"):
        errors.append("SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID")
    if values["SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID"] and not values["SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID"].startswith("-"):
        errors.append("SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID")
    if values["TAKSKLAD_AUTOMATION_ALERT_CHAT_ID"].startswith("-"):
        errors.append("TAKSKLAD_AUTOMATION_ALERT_CHAT_ID")
    if len(set(values.values())) != 3:
        errors.append("TELEGRAM_ROUTE_ROLE_COLLISION")
    return sorted(set(errors))


def canonical_route_identity_sha256(client: object, logistics: object, admin: object) -> str:
    values = {
        "client": str(client or "").strip(),
        "logistics": str(logistics or "").strip(),
        "admin": str(admin or "").strip(),
    }
    errors = validate_route_values(values["client"], values["logistics"], values["admin"])
    if errors:
        raise TelegramRoutingContractError("routing identity values are invalid")
    canonical = {
        "roles": [
            {"role": "client", "target_type": "group", "chat_id": values["client"]},
            {"role": "logistics", "target_type": "group", "chat_id": values["logistics"]},
            {"role": "admin", "target_type": "personal", "chat_id": values["admin"]},
        ]
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_route_identity_anchor(
    client: object,
    logistics: object,
    admin: object,
    expected_sha256: object,
) -> None:
    expected = str(expected_sha256 or "").strip()
    if not ROUTING_IDENTITY_ANCHOR_RE.fullmatch(expected):
        raise TelegramRoutingContractError("protected routing identity anchor is missing or malformed")
    actual = canonical_route_identity_sha256(client, logistics, admin)
    if actual != expected:
        raise TelegramRoutingContractError("protected routing identity anchor mismatch")


def production_environment_errors(environ: Mapping[str, object]) -> list[str]:
    load_telegram_routing_contract()
    client = str(environ.get("SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID") or "").strip()
    logistics = str(environ.get("SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID") or "").strip()
    admin = str(environ.get("TAKSKLAD_AUTOMATION_ALERT_CHAT_ID") or "").strip()
    errors: list[str] = []
    errors.extend(validate_route_values(client, logistics, admin))
    exact_values = {
        "SMARTUP_AUTO_IMPORT_TIMES": "12:00,15:00,17:50",
        "SMARTUP_AUTO_IMPORT_FINAL_TIME": "17:50",
        "SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME": "17:50",
        "SKLADBOT_DAILY_REPORT_HOUR": "22",
        "SKLADBOT_DAILY_REPORT_MINUTE": "0",
    }
    for setting, expected in exact_values.items():
        if str(environ.get(setting) or "").strip() != expected:
            errors.append(setting)
    set_values = {
        "TELEGRAM_ALLOWED_CHAT_IDS": {client, logistics, admin},
        "TELEGRAM_ADMIN_CHAT_IDS": {admin},
        "SKLADBOT_DAILY_REPORT_CHAT_IDS": {client},
    }
    for setting, expected in set_values.items():
        if set(csv_values(environ.get(setting))) != expected:
            errors.append(setting)
    if str(environ.get("SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID") or "").strip():
        errors.append("SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID")
    return sorted(set(errors))
