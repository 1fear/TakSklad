#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="${TAKSKLAD_ENV_FILE:-$SCRIPT_DIR/.env}"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
MANIFEST_FILE="$APP_DIR/outputs/taksklad_acceptance/acceptance_manifest.json"
VERSION_FILE="$APP_DIR/version.json"
VERIFY_SCRIPT="$SCRIPT_DIR/verify_acceptance_marker.sh"

usage() {
  cat >&2 <<'EOF'
Usage:
  acceptance_status.sh [--marker MARKER] [--expect-orders N] [--expect-scans N] [--expect-completed]

Read-only status check for TakSklad acceptance.
EOF
}

MARKER=""
VERIFY_ARGS=()

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
if [[ ! -x "$VERIFY_SCRIPT" ]]; then
  echo "Verifier is not executable: $VERIFY_SCRIPT" >&2
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
    "expected": manifest["expected"],
}, ensure_ascii=False, sort_keys=True))
PY
)"

MARKER="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["marker"])' "$MANIFEST_INFO")"
EXCEL_FILE="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["excel_file"])' "$MANIFEST_INFO")"
EXPECTED_SHA="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["excel_sha256"])' "$MANIFEST_INFO")"
EXCEL_PATH="$APP_DIR/outputs/taksklad_acceptance/$EXCEL_FILE"

if [[ ! -f "$EXCEL_PATH" ]]; then
  echo "Acceptance Excel not found: $EXCEL_PATH" >&2
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
    "download_url_onedir_set": bool(version.get("download_url_onedir")),
}, ensure_ascii=False, sort_keys=True))
PY
)"

set +e
HEALTH_OUTPUT="$(
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T backend-api \
    python - <<'PY' 2>&1
from urllib.request import urlopen
print(urlopen("http://127.0.0.1:8000/health", timeout=5).read().decode())
PY
)"
HEALTH_STATUS="$?"
COMPOSE_OUTPUT="$(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps --format json 2>&1)"
COMPOSE_STATUS="$?"
VERIFY_OUTPUT="$("$VERIFY_SCRIPT" "$MARKER" "${VERIFY_ARGS[@]}" 2>&1)"
VERIFY_STATUS="$?"
set -e

python3 - "$MANIFEST_INFO" "$VERSION_INFO" "$SHA_STATUS" "$ACTUAL_SHA" "$EXPECTED_SHA" "$HEALTH_STATUS" "$HEALTH_OUTPUT" "$COMPOSE_STATUS" "$COMPOSE_OUTPUT" "$VERIFY_STATUS" "$VERIFY_OUTPUT" <<'PY'
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

errors = []
if sha_status != "ok":
    errors.append("acceptance Excel SHA mismatch")
if health_status != 0:
    errors.append(f"backend health failed with exit {health_status}")
if compose_status != 0:
    errors.append(f"docker compose ps failed with exit {compose_status}")
if verify_status != 0:
    errors.append(f"acceptance verifier failed with exit {verify_status}")

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
}

print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
if errors:
    sys.exit(3)
PY
