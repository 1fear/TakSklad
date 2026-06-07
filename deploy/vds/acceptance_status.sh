#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="${TAKSKLAD_ENV_FILE:-$SCRIPT_DIR/.env}"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
MANIFEST_FILE="$APP_DIR/outputs/taksklad_acceptance/acceptance_manifest.json"
VERSION_FILE="$APP_DIR/version.json"
VERIFY_SCRIPT="$SCRIPT_DIR/verify_acceptance_marker.sh"
TELEGRAM_MENU_SCRIPT="$SCRIPT_DIR/verify_telegram_menu.sh"
GOOGLE_SYNC_SCRIPT="$SCRIPT_DIR/verify_google_backend_sync.sh"
SKLADBOT_COVERAGE_SCRIPT="$SCRIPT_DIR/verify_skladbot_coverage.sh"
RESULTS_FILE="$APP_DIR/outputs/taksklad_acceptance/ACCEPTANCE_RESULTS.md"
GO_NO_GO_SCRIPT="$APP_DIR/tools/release_go_no_go.py"
HEALTH_ATTEMPTS="${ACCEPTANCE_HEALTH_ATTEMPTS:-6}"
HEALTH_RETRY_DELAY_SECONDS="${ACCEPTANCE_HEALTH_RETRY_DELAY_SECONDS:-2}"

usage() {
  cat >&2 <<'EOF'
Usage:
  acceptance_status.sh [--marker MARKER] [--expect-orders N] [--expect-scans N] [--expect-completed] [--require-go]

Read-only status check for TakSklad acceptance.
EOF
}

MARKER=""
VERIFY_ARGS=()
REQUIRE_GO=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --marker)
      if [[ $# -lt 2 || -z "$2" ]]; then
        usage
        exit 2
      fi
      MARKER="$2"
      shift 2
      ;;
    --expect-orders)
      if [[ $# -lt 2 || ! "$2" =~ ^[0-9]+$ ]]; then
        usage
        exit 2
      fi
      VERIFY_ARGS+=("--expect-orders" "$2")
      shift 2
      ;;
    --expect-scans)
      if [[ $# -lt 2 || ! "$2" =~ ^[0-9]+$ ]]; then
        usage
        exit 2
      fi
      VERIFY_ARGS+=("--expect-scans" "$2")
      shift 2
      ;;
    --expect-completed)
      VERIFY_ARGS+=("--expect-completed")
      shift
      ;;
    --require-go)
      REQUIRE_GO=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ ! -f "$MANIFEST_FILE" ]]; then
  echo "Manifest not found: $MANIFEST_FILE" >&2
  exit 1
fi
if [[ ! -f "$VERSION_FILE" ]]; then
  echo "version.json not found: $VERSION_FILE" >&2
  exit 1
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Env file not found: $ENV_FILE" >&2
  exit 1
fi
ENV_FILE="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"
if [[ ! -x "$VERIFY_SCRIPT" ]]; then
  echo "Verifier is not executable: $VERIFY_SCRIPT" >&2
  exit 1
fi
if [[ ! -x "$TELEGRAM_MENU_SCRIPT" ]]; then
  echo "Telegram menu verifier is not executable: $TELEGRAM_MENU_SCRIPT" >&2
  exit 1
fi
if [[ ! -x "$GOOGLE_SYNC_SCRIPT" ]]; then
  echo "Google/backend sync verifier is not executable: $GOOGLE_SYNC_SCRIPT" >&2
  exit 1
fi
if [[ ! -x "$SKLADBOT_COVERAGE_SCRIPT" ]]; then
  echo "SkladBot coverage verifier is not executable: $SKLADBOT_COVERAGE_SCRIPT" >&2
  exit 1
fi
if [[ ! -f "$GO_NO_GO_SCRIPT" ]]; then
  echo "GO/NO-GO script not found: $GO_NO_GO_SCRIPT" >&2
  exit 1
fi

MANIFEST_INFO="$(
  python3 - "$MANIFEST_FILE" "$MARKER" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
marker = sys.argv[2] or manifest["marker"]
print(json.dumps({
    "marker": marker,
    "excel_file": manifest["excel_file"],
    "excel_sha256": manifest["excel_sha256"],
    "result_template": manifest.get("result_template"),
    "result_file": manifest.get("result_file") or "ACCEPTANCE_RESULTS.md",
    "expected": manifest["expected"],
    "safety": manifest.get("safety") or {},
}, ensure_ascii=False, sort_keys=True))
PY
)"

MARKER="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["marker"])' "$MANIFEST_INFO")"
EXCEL_FILE="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["excel_file"])' "$MANIFEST_INFO")"
EXPECTED_SHA="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["excel_sha256"])' "$MANIFEST_INFO")"
RESULT_TEMPLATE="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("result_template") or "")' "$MANIFEST_INFO")"
RESULT_FILE="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("result_file") or "ACCEPTANCE_RESULTS.md")' "$MANIFEST_INFO")"
EXCEL_PATH="$APP_DIR/outputs/taksklad_acceptance/$EXCEL_FILE"
RESULT_TEMPLATE_PATH="$APP_DIR/outputs/taksklad_acceptance/$RESULT_TEMPLATE"
RESULTS_FILE="$APP_DIR/outputs/taksklad_acceptance/$RESULT_FILE"

if [[ ! -f "$EXCEL_PATH" ]]; then
  echo "Acceptance Excel not found: $EXCEL_PATH" >&2
  exit 1
fi
if [[ -z "$RESULT_TEMPLATE" || ! -f "$RESULT_TEMPLATE_PATH" ]]; then
  echo "Acceptance result template not found: $RESULT_TEMPLATE_PATH" >&2
  exit 1
fi
if [[ -z "$RESULT_FILE" || ! -f "$RESULTS_FILE" ]]; then
  echo "Acceptance result file not found: $RESULTS_FILE" >&2
  exit 1
fi

ACTUAL_SHA="$(sha256sum "$EXCEL_PATH" | awk '{print $1}')"
SHA_STATUS="ok"
if [[ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]]; then
  SHA_STATUS="failed"
fi

VERSION_INFO="$(
  python3 - "$VERSION_FILE" <<'PY'
import json
import sys
from pathlib import Path

version = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(json.dumps({
    "latest_version": version.get("latest_version"),
    "min_supported_version": version.get("min_supported_version"),
    "mandatory": version.get("mandatory"),
    "package_type": version.get("package_type"),
    "download_url_set": bool(version.get("download_url")),
    "sha256_set": bool(version.get("sha256")),
    "download_url_onedir_set": bool(version.get("download_url_onedir")),
    "sha256_onedir_set": bool(version.get("sha256_onedir")),
}, ensure_ascii=False, sort_keys=True))
PY
)"

set +e
HEALTH_OUTPUT=""
HEALTH_STATUS="1"
for ((health_attempt = 1; health_attempt <= HEALTH_ATTEMPTS; health_attempt++)); do
  HEALTH_OUTPUT="$(
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T backend-api \
      python - <<'PY' 2>&1
from urllib.request import urlopen
print(urlopen("http://127.0.0.1:8000/health", timeout=5).read().decode())
PY
  )"
  HEALTH_STATUS="$?"
  if [[ "$HEALTH_STATUS" -eq 0 ]]; then
    break
  fi
  if [[ "$health_attempt" -lt "$HEALTH_ATTEMPTS" ]]; then
    sleep "$HEALTH_RETRY_DELAY_SECONDS"
  fi
done
COMPOSE_OUTPUT="$(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps --format json 2>&1)"
COMPOSE_STATUS="$?"
if ((${#VERIFY_ARGS[@]})); then
  VERIFY_OUTPUT="$("$VERIFY_SCRIPT" "$MARKER" "${VERIFY_ARGS[@]}" 2>&1)"
else
  VERIFY_OUTPUT="$("$VERIFY_SCRIPT" "$MARKER" 2>&1)"
fi
VERIFY_STATUS="$?"
TELEGRAM_MENU_OUTPUT="$("$TELEGRAM_MENU_SCRIPT" 2>&1)"
TELEGRAM_MENU_STATUS="$?"
GOOGLE_SYNC_OUTPUT="$("$GOOGLE_SYNC_SCRIPT" 2>&1)"
GOOGLE_SYNC_STATUS="$?"
SKLADBOT_COVERAGE_OUTPUT="$("$SKLADBOT_COVERAGE_SCRIPT" 2>&1)"
SKLADBOT_COVERAGE_STATUS="$?"
GO_NO_GO_OUTPUT="$(python3 "$GO_NO_GO_SCRIPT" --results "$RESULTS_FILE" 2>&1)"
GO_NO_GO_STATUS="$?"
set -e

python3 - "$MANIFEST_INFO" "$VERSION_INFO" "$SHA_STATUS" "$ACTUAL_SHA" "$EXPECTED_SHA" "$HEALTH_STATUS" "$HEALTH_OUTPUT" "$COMPOSE_STATUS" "$COMPOSE_OUTPUT" "$VERIFY_STATUS" "$VERIFY_OUTPUT" "$TELEGRAM_MENU_STATUS" "$TELEGRAM_MENU_OUTPUT" "$GOOGLE_SYNC_STATUS" "$GOOGLE_SYNC_OUTPUT" "$SKLADBOT_COVERAGE_STATUS" "$SKLADBOT_COVERAGE_OUTPUT" "$GO_NO_GO_STATUS" "$GO_NO_GO_OUTPUT" "$REQUIRE_GO" <<'PY'
import json
import sys

manifest_info = json.loads(sys.argv[1])
version_info = json.loads(sys.argv[2])
sha_status = sys.argv[3]
actual_sha = sys.argv[4]
expected_sha = sys.argv[5]
health_status = int(sys.argv[6])
health_output = sys.argv[7].strip()
compose_status = int(sys.argv[8])
compose_output = sys.argv[9].strip()
verify_status = int(sys.argv[10])
verify_output = sys.argv[11].strip()
telegram_menu_status = int(sys.argv[12])
telegram_menu_output = sys.argv[13].strip()
google_sync_status = int(sys.argv[14])
google_sync_output = sys.argv[15].strip()
skladbot_coverage_status = int(sys.argv[16])
skladbot_coverage_output = sys.argv[17].strip()
go_no_go_status = int(sys.argv[18])
go_no_go_output = sys.argv[19].strip()
require_go = sys.argv[20] == "1"

try:
    health = json.loads(health_output)
except Exception:
    health = {"raw": health_output}

services = []
if compose_status == 0 and compose_output:
    for line in compose_output.splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        services.append({
            "name": row.get("Name") or row.get("Names"),
            "service": row.get("Service"),
            "state": row.get("State"),
            "status": row.get("Status"),
        })

try:
    verifier = json.loads(verify_output.splitlines()[-1])
except Exception:
    verifier = {"raw": verify_output}

try:
    telegram_menu = json.loads(telegram_menu_output.splitlines()[-1])
except Exception:
    telegram_menu = {"status": "failed", "raw": telegram_menu_output}

try:
    google_sync = json.loads(google_sync_output.splitlines()[-1])
except Exception:
    google_sync = {"status": "failed", "raw": google_sync_output}

try:
    skladbot_coverage = json.loads(skladbot_coverage_output.splitlines()[-1])
except Exception:
    skladbot_coverage = {"status": "failed", "raw": skladbot_coverage_output}

try:
    release_gate = json.loads(go_no_go_output.splitlines()[-1])
except Exception:
    release_gate = {"status": "no_go", "raw": go_no_go_output}

errors = []
if sha_status != "ok":
    errors.append("acceptance Excel SHA mismatch")
if version_info.get("latest_version") != "2.0.9":
    errors.append("version.json latest_version must be 2.0.9")
if version_info.get("min_supported_version") != "2.0.9":
    errors.append("version.json min_supported_version must be 2.0.9 for forced rollout")
if version_info.get("mandatory") is not True:
    errors.append("version.json mandatory must be true during forced rollout")
if version_info.get("package_type") != "onefile_exe":
    errors.append("version.json package_type must be onefile_exe")
if not version_info.get("download_url_set") or not version_info.get("sha256_set"):
    errors.append("version.json onefile download_url and sha256 must be set")
if not version_info.get("download_url_onedir_set") or not version_info.get("sha256_onedir_set"):
    errors.append("version.json onedir download_url_onedir and sha256_onedir must be set")
safety = manifest_info.get("safety") or {}
for key in ("version_json_staged_rollout", "github_release_published", "push_notifications_allowed", "mandatory_update_enabled"):
    if safety.get(key) is not True:
        errors.append(f"manifest safety.{key} must be true")
if safety.get("contains_secrets") is not False:
    errors.append("manifest safety.contains_secrets must be false")
if health_status != 0:
    errors.append(f"backend health failed with exit {health_status}")
if compose_status != 0:
    errors.append(f"docker compose ps failed with exit {compose_status}")
if verify_status != 0:
    errors.append(f"acceptance verifier failed with exit {verify_status}")
if telegram_menu_status != 0 or telegram_menu.get("status") != "ok":
    errors.append(f"telegram menu verifier failed with exit {telegram_menu_status}")
if google_sync_status != 0 or google_sync.get("status") != "ok":
    errors.append(f"google/backend sync verifier failed with exit {google_sync_status}")
if skladbot_coverage_status != 0 or skladbot_coverage.get("status") != "ok":
    errors.append(f"skladbot coverage verifier failed with exit {skladbot_coverage_status}")
release_status = release_gate.get("status")
if require_go and release_status != "go":
    errors.append(f"release GO/NO-GO is not go: {release_status or 'unknown'}")

summary = {
    "status": "failed" if errors else "ok",
    "errors": errors,
    "manifest": manifest_info,
    "excel_sha256": {
        "status": sha_status,
        "expected": expected_sha,
        "actual": actual_sha,
    },
    "version_json": version_info,
    "backend_health": {
        "exit_code": health_status,
        "response": health,
    },
    "compose": {
        "exit_code": compose_status,
        "services": services,
    },
    "acceptance_verifier": {
        "exit_code": verify_status,
        "response": verifier,
    },
    "telegram_menu": {
        "exit_code": telegram_menu_status,
        "response": telegram_menu,
    },
    "google_backend_sync": {
        "exit_code": google_sync_status,
        "response": google_sync,
    },
    "skladbot_coverage": {
        "exit_code": skladbot_coverage_status,
        "response": skladbot_coverage,
    },
    "release_go_no_go": {
        "exit_code": go_no_go_status,
        "response": release_gate,
        "required": require_go,
    },
}

print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
if errors:
    sys.exit(3)
PY
