#!/usr/bin/env python3
"""Verify the one safe deploy bypass for a rollback-incompatible Telegram worker."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REQUIRED_SERVICES = {
    "backend-api",
    "frontend",
    "postgres",
    "skladbot-worker",
    "smartup-auto-import-worker",
    "telegram-worker",
}


class RepairPreflightBlocked(RuntimeError):
    pass


def _load(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _compose_rows(path: str) -> list[dict]:
    raw = Path(path).read_text(encoding="utf-8").strip()
    if not raw:
        raise RepairPreflightBlocked("compose_status_missing")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = [json.loads(line) for line in raw.splitlines() if line.strip()]
    rows = parsed if isinstance(parsed, list) else [parsed]
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise RepairPreflightBlocked("compose_status_invalid")
    return rows


def _service(row: dict) -> str:
    return str(row.get("Service") or row.get("service") or "").strip()


def _state(row: dict) -> str:
    return str(row.get("State") or row.get("state") or "").strip().casefold()


def _health(row: dict) -> str:
    health = str(row.get("Health") or row.get("health") or "").strip().casefold()
    status = str(row.get("Status") or row.get("status") or "").strip().casefold()
    if health:
        return health
    if "unhealthy" in status:
        return "unhealthy"
    if "healthy" in status:
        return "healthy"
    return ""


def verify(ready_wrapper: dict, compose_rows: list[dict]) -> dict:
    if int(ready_wrapper.get("http_status") or 0) != 503:
        raise RepairPreflightBlocked("readiness_must_be_503")
    ready = ready_wrapper.get("payload")
    if not isinstance(ready, dict) or ready.get("ready") is not False:
        raise RepairPreflightBlocked("readiness_payload_invalid")
    if (ready.get("database") or {}).get("status") != "ok":
        raise RepairPreflightBlocked("database_not_ready")
    if (ready.get("migrations") or {}).get("status") != "ok":
        raise RepairPreflightBlocked("migrations_not_ready")
    queue = ready.get("queue") or {}
    if any(int(queue.get(key) or 0) != 0 for key in (
        "hot_path_stale_processing_count",
        "hot_path_blocking_count",
        "hot_path_error_count",
    )):
        raise RepairPreflightBlocked("queue_blocker_present")
    if int((ready.get("imports") or {}).get("recent_error_count") or 0) != 0:
        raise RepairPreflightBlocked("import_error_present")
    workers = ready.get("workers") or {}
    if int(workers.get("unhealthy_count") or 0) != 1 or int(workers.get("missing_count") or 0) != 0:
        raise RepairPreflightBlocked("exactly_one_unhealthy_worker_required")
    for section in ("daily_report", "desktop_pairing"):
        if (ready.get(section) or {}).get("status") != "ok":
            raise RepairPreflightBlocked(f"{section}_not_ready")

    services = {_service(row): row for row in compose_rows if _service(row)}
    if set(services) != REQUIRED_SERVICES:
        raise RepairPreflightBlocked("compose_service_set_mismatch")
    for name, row in services.items():
        if _state(row) != "running":
            raise RepairPreflightBlocked("service_not_running")
        health = _health(row)
        if name == "telegram-worker":
            if health != "unhealthy":
                raise RepairPreflightBlocked("telegram_worker_not_uniquely_unhealthy")
        elif health != "healthy":
            raise RepairPreflightBlocked("unrelated_service_unhealthy")
    return {
        "status": "repairable",
        "unhealthy_service": "telegram-worker",
        "unhealthy_count": 1,
        "queue_blockers": 0,
        "import_errors": 0,
        "values_redacted": True,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready-json", required=True)
    parser.add_argument("--compose-ps-json", required=True)
    args = parser.parse_args(argv)
    try:
        result = verify(_load(args.ready_json), _compose_rows(args.compose_ps_json))
    except (OSError, ValueError, RepairPreflightBlocked) as exc:
        reason = exc if isinstance(exc, RepairPreflightBlocked) else "invalid_input"
        print(f"TELEGRAM_WORKER_REPAIR_PREFLIGHT_BLOCKED reason={reason}", file=sys.stderr)
        return 3
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
