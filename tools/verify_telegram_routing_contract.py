#!/usr/bin/env python3
"""No-send verifier for candidate Telegram routing and runtime outputs."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.daily_report_config import (  # noqa: E402
    DailyReportConfigurationError,
    validate_production_daily_report_config,
)
from backend.app.telegram_output_contract import (  # noqa: E402
    runtime_output_artifacts,
    runtime_output_policy_hashes,
)
from backend.app.telegram_routing_contract import (  # noqa: E402
    ROUTING_IDENTITY_ANCHOR_ENV,
    TelegramRoutingContractError,
    load_telegram_routing_contract,
    validate_route_identity_anchor,
)
from tools.prepare_notification_routing_env import duplicate_env_keys, parse_env_assignments  # noqa: E402


TELEGRAM_SERVICE_KEYS = (
    "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID",
    "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID",
    "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID",
    "SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID",
    "SMARTUP_AUTO_IMPORT_TIMES",
    "SMARTUP_AUTO_IMPORT_FINAL_TIME",
    "SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME",
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "TELEGRAM_ADMIN_CHAT_IDS",
    "SKLADBOT_DAILY_REPORT_CHAT_IDS",
    "SKLADBOT_DAILY_REPORT_HOUR",
    "SKLADBOT_DAILY_REPORT_MINUTE",
)
SMARTUP_SERVICE_KEYS = (
    "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID",
    "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID",
    "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID",
    "SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID",
    "SMARTUP_AUTO_IMPORT_TIMES",
    "SMARTUP_AUTO_IMPORT_FINAL_TIME",
    "SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME",
    "TELEGRAM_ADMIN_CHAT_IDS",
)


class CandidateRoutingError(RuntimeError):
    """Raised with setting names only and never raw values."""


def _require_protected_candidate_files(env_path: Path, compose_path: Path) -> None:
    if env_path.parent != compose_path.parent:
        raise CandidateRoutingError("candidate_state_directory")
    parent = env_path.parent
    try:
        parent_stat = parent.stat()
        file_stats = tuple(path.lstat() for path in (env_path, compose_path))
    except OSError as exc:
        raise CandidateRoutingError("candidate_files") from exc
    if (
        parent.is_symlink()
        or not stat.S_ISDIR(parent_stat.st_mode)
        or stat.S_IMODE(parent_stat.st_mode) != 0o700
    ):
        raise CandidateRoutingError("candidate_state_directory_mode")
    if any(
        not stat.S_ISREG(file_stat.st_mode) or stat.S_IMODE(file_stat.st_mode) != 0o600
        for file_stat in file_stats
    ):
        raise CandidateRoutingError("candidate_file_mode")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-path", required=True)
    parser.add_argument("--compose-config-json", required=True)
    parser.add_argument("--json", action="store_true", help="Print the redacted matrix as JSON")
    return parser.parse_args(argv)


def _compose_environment(payload: Mapping[str, object], service: str) -> dict[str, str]:
    try:
        environment = payload["services"][service]["environment"]  # type: ignore[index]
    except (KeyError, TypeError) as exc:
        raise CandidateRoutingError(f"compose.{service}.environment") from exc
    if not isinstance(environment, Mapping):
        raise CandidateRoutingError(f"compose.{service}.environment")
    return {str(key): str(value or "") for key, value in environment.items()}


def _require_service_matches_candidate(
    service: str,
    service_environment: Mapping[str, str],
    candidate: Mapping[str, str],
    keys: tuple[str, ...],
) -> None:
    mismatches = [
        f"compose.{service}.{key}"
        for key in keys
        if str(service_environment.get(key) or "").strip()
        != str(candidate.get(key) or "").strip()
    ]
    if mismatches:
        raise CandidateRoutingError(",".join(sorted(mismatches)))


def validate_candidate_files(
    env_path: Path,
    compose_path: Path,
    expected_identity_anchor_sha256: object,
) -> None:
    _require_protected_candidate_files(env_path, compose_path)
    try:
        candidate_text = env_path.read_bytes().decode("utf-8")
        duplicate_managed = duplicate_env_keys(candidate_text) & set(TELEGRAM_SERVICE_KEYS)
        if duplicate_managed:
            raise CandidateRoutingError("candidate_duplicate_routing_keys")
        candidate = parse_env_assignments(candidate_text)
        if ROUTING_IDENTITY_ANCHOR_ENV in candidate:
            raise CandidateRoutingError("candidate_protected_identity_anchor")
        compose_payload = json.loads(compose_path.read_text(encoding="utf-8"))
    except CandidateRoutingError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateRoutingError("candidate_files") from exc

    try:
        validate_production_daily_report_config(candidate)
    except DailyReportConfigurationError as exc:
        raise CandidateRoutingError(",".join(exc.setting_names)) from exc

    telegram_environment = _compose_environment(compose_payload, "telegram-worker")
    smartup_environment = _compose_environment(compose_payload, "smartup-auto-import-worker")
    if any(
        ROUTING_IDENTITY_ANCHOR_ENV in environment
        for environment in (telegram_environment, smartup_environment)
    ):
        raise CandidateRoutingError("compose_protected_identity_anchor")
    validate_route_identity_anchor(
        candidate.get("SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID"),
        candidate.get("SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID"),
        candidate.get("TAKSKLAD_AUTOMATION_ALERT_CHAT_ID"),
        expected_identity_anchor_sha256,
    )
    try:
        validate_production_daily_report_config(telegram_environment)
    except DailyReportConfigurationError as exc:
        raise CandidateRoutingError(",".join(
            f"compose.telegram-worker.{name}" for name in exc.setting_names
        )) from exc
    _require_service_matches_candidate(
        "telegram-worker", telegram_environment, candidate, TELEGRAM_SERVICE_KEYS
    )
    _require_service_matches_candidate(
        "smartup-auto-import-worker", smartup_environment, candidate, SMARTUP_SERVICE_KEYS
    )


def validate_runtime_outputs() -> None:
    contract = load_telegram_routing_contract()
    artifacts = runtime_output_artifacts()
    hashes = runtime_output_policy_hashes()
    if set(artifacts) != set(contract.message_kinds) or set(hashes) != set(contract.message_kinds):
        raise CandidateRoutingError("runtime_output_kinds")
    mismatches = [
        kind
        for kind, digest in hashes.items()
        if contract.route_for(kind).text_policy_sha256 != digest
    ]
    if mismatches:
        raise CandidateRoutingError("runtime_output_hashes:" + ",".join(sorted(mismatches)))
    rendered = json.dumps(artifacts, ensure_ascii=False, sort_keys=True)
    forbidden = (
        "AUTO" + " Smartup ·",
        "MANUAL" + " /logistics ·",
        "_" + "AUTO.xlsx",
        "_" + "MANUAL.xlsx",
    )
    if any(marker in rendered for marker in forbidden):
        raise CandidateRoutingError("runtime_output_provenance")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_candidate_files(
            Path(args.env_path),
            Path(args.compose_config_json),
            os.environ.get(ROUTING_IDENTITY_ANCHOR_ENV),
        )
        validate_runtime_outputs()
        matrix = load_telegram_routing_contract().safe_matrix()
    except (CandidateRoutingError, TelegramRoutingContractError) as exc:
        print(f"TELEGRAM_ROUTING_CONTRACT_BLOCKED fields={exc}", file=sys.stderr)
        return 1
    matrix["candidate_config_validated"] = True
    matrix["protected_identity_anchor_validated"] = True
    matrix["runtime_outputs_validated"] = True
    if args.json:
        print(json.dumps(matrix, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        kinds = matrix["message_kinds"]
        summary = ",".join(
            f"{kind}->{spec['destination_alias']}@{'|'.join(spec['schedules'])}"
            for kind, spec in kinds.items()
        )
        print(
            f"TELEGRAM_ROUTING_CONTRACT_OK {summary} "
            "candidate_config_validated=1 runtime_outputs_validated=1 "
            "raw_chat_ids_redacted=1 no_send=1"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
