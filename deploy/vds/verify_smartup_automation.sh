#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="${TAKSKLAD_ENV_FILE:-$SCRIPT_DIR/.env}"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
RUNTIME_REQUIRED="${SMARTUP_AUTOMATION_RUNTIME_REQUIRED:-0}"

RUNTIME_EXIT=0
RUNTIME_OUTPUT=""
RUNTIME_SKIP_REASON=""

if [[ -f "$ENV_FILE" ]] && command -v docker >/dev/null 2>&1; then
  ENV_FILE="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"
  set +e
  RUNTIME_OUTPUT="$(
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T smartup-auto-import-worker \
      python -m app.smartup_auto_import_worker status --json 2>&1
  )"
  RUNTIME_EXIT="$?"
  set -e
else
  RUNTIME_SKIP_REASON="env file or docker is not available"
fi

python3 - "$APP_DIR" "$COMPOSE_FILE" "$RUNTIME_EXIT" "$RUNTIME_SKIP_REASON" "$RUNTIME_OUTPUT" "$RUNTIME_REQUIRED" <<'PY'
import json
import sys
from pathlib import Path

app_dir = Path(sys.argv[1])
compose_file = Path(sys.argv[2])
runtime_exit = int(sys.argv[3])
runtime_skip_reason = sys.argv[4]
runtime_output = sys.argv[5]
runtime_required = sys.argv[6] == "1"

checks = {}
errors = []


def read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        errors.append(f"missing file: {path.relative_to(app_dir)}")
        return ""


required_files = [
    "backend/app/smartup_auto_import.py",
    "backend/app/smartup_auto_import_worker.py",
    "backend/app/smartup_auto_import_history_service.py",
]
for relative in required_files:
    exists = (app_dir / relative).is_file()
    checks[f"file:{relative}"] = exists
    if not exists:
        errors.append(f"required Smartup file is missing: {relative}")

compose = read_text(compose_file)
source = read_text(app_dir / "backend/app/smartup_auto_import.py")
worker = read_text(app_dir / "backend/app/smartup_auto_import_worker.py")
imports = read_text(app_dir / "backend/app/imports_service.py")
logistics = read_text(app_dir / "backend/app/logistics_service.py")


def compose_service_block(service_name):
    marker = f"  {service_name}:"
    capture = False
    lines = []
    for line in compose.splitlines():
        if line == marker:
            capture = True
            lines.append(line)
            continue
        if capture and line.startswith("  ") and not line.startswith("    ") and line.strip().endswith(":"):
            break
        if capture:
            lines.append(line)
    return "\n".join(lines)


smartup_worker_compose = compose_service_block("smartup-auto-import-worker")

required_fragments = [
    ("compose smartup service", compose, "smartup-auto-import-worker"),
    ("compose backend import gate", compose, "SMARTUP_AUTO_IMPORT_BACKEND_IMPORT_ENABLED"),
    ("compose change status gate", compose, "SMARTUP_AUTO_IMPORT_CHANGE_STATUS_ENABLED"),
    ("compose durable saga gate", smartup_worker_compose, "SMARTUP_AUTO_IMPORT_SAGA_MODE"),
    ("compose client chat route", compose, "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID"),
    ("compose logistics chat route", compose, "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID"),
    ("compose unified alert chat route", smartup_worker_compose, "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID"),
    ("compose legacy alert compatibility", smartup_worker_compose, "SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID"),
    ("compose admin allowlist", smartup_worker_compose, "TELEGRAM_ADMIN_CHAT_IDS"),
    ("compose route fingerprint key", smartup_worker_compose, "SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY"),
    ("compose independent logistics due time", smartup_worker_compose, "SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME"),
    ("compose final logistics due time", smartup_worker_compose, "SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME"),
    ("compose smartup geocoder key", smartup_worker_compose, "YANDEX_GEOCODER_API_KEY"),
    ("status command", worker, "build_smartup_auto_import_status"),
    ("run-once delivery date option", worker, "--delivery-date"),
    ("backend import requires status change", source, "SMARTUP_AUTO_IMPORT_BACKEND_IMPORT_ENABLED=true требует"),
    ("Smartup import uses dry-run first", source, 'skladbot_create_mode="dry_run"'),
    ("Smartup status change exists", source, "client.change_status"),
    ("SkladBot queue after status", source, "queue_skladbot_after_smartup_status"),
    ("partial status success filter", source, "successful_deal_ids"),
    ("partial status SkladBot skip", source, "smartup_status_not_confirmed"),
    ("preview failure audit artifact", source, "failed_preview"),
    ("client export routing", source, "send_smartup_export_to_client"),
    ("final logistics routing", source, "send_final_logistics_reports"),
    ("independent logistics recovery", source, "run_due_smartup_logistics_reports"),
    ("route correction idempotency", source, "smartup:logistics_report:v2"),
    ("keyed route fingerprint", source, "hmac.new"),
    ("production route contract", source, "smartup_production_route_errors"),
    ("durable logistics dependency proof", source, "smartup_logistics_dependency_proof"),
    ("client delivery dependency gate", source, "client_export_not_terminal"),
    ("fulfillment terminal dependency gate", source, "fulfillment_not_terminal"),
    ("ambiguous delivery manual recovery", source, "manual_recovery_required"),
    ("production runtime contract", source, "smartup_production_runtime_errors"),
    ("production backend import required", source, "SMARTUP_AUTO_IMPORT_BACKEND_IMPORT_ENABLED=true"),
    ("production create mode required", source, "SKLADBOT_CREATE_REQUESTS_MODE=enabled"),
    ("production three-slot invariant", source, "ровно 3 уникальных slot time"),
    ("production final-slot invariant", source, "FINAL_TIME должен входить"),
    ("unified alert route contract", source, "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID"),
    ("bounded logistics retry", source, "retry_exhausted"),
    ("legacy logistics fail-safe", source, "legacy_assumed_delivered"),
    ("reverse geocode guard", source, "reverse_geocode_yandex"),
    ("delivery-date target guard", source, "target_delivery_date"),
    ("source import id dedupe", imports, "source_import_id"),
    ("repriced totals import", imports, "imported_line_total"),
    ("logistics template boxes header", logistics, '"Короба",'),
    ("logistics boxes from order quantity", logistics, "set_cell(row, 31, quantity_blocks)"),
]

for label, text, fragment in required_fragments:
    ok = fragment in text
    checks[label] = ok
    if not ok:
        errors.append(f"missing Smartup guard fragment: {label}")

runtime_status = {"status": "skipped", "reason": runtime_skip_reason}
if runtime_skip_reason and runtime_required:
    errors.append(f"Smartup runtime status is required but skipped: {runtime_skip_reason}")
if not runtime_skip_reason:
    if runtime_exit != 0:
        if not runtime_required and "is not running" in runtime_output:
            runtime_status = {
                "status": "skipped",
                "reason": "smartup-auto-import-worker service is not running",
            }
        else:
            runtime_status = {
                "status": "failed",
                "exit_code": runtime_exit,
                "raw": runtime_output[-1000:],
            }
            errors.append(f"Smartup runtime status command failed with exit {runtime_exit}")
    else:
        try:
            start = runtime_output.index("{")
            end = runtime_output.rindex("}") + 1
            runtime_status = json.loads(runtime_output[start:end])
        except Exception:
            runtime_status = {
                "status": "failed",
                "exit_code": runtime_exit,
                "raw": runtime_output[-1000:],
            }
            errors.append("Smartup runtime status command did not return JSON")
        else:
            if runtime_status.get("status") != "ok":
                errors.append(f"Smartup runtime status is not ok: {runtime_status.get('status') or 'unknown'}")

summary = {
    "status": "failed" if errors else "ok",
    "errors": errors,
    "source_checks": checks,
    "runtime_status": runtime_status,
}
print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
if errors:
    sys.exit(3)
PY
