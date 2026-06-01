#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${TAKSKLAD_ENV_FILE:-$SCRIPT_DIR/.env}"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
DETAIL_LIMIT="20"
MAX_ATTEMPTS="${GOOGLE_BACKEND_SYNC_ATTEMPTS:-4}"
RETRY_DELAY_SECONDS="${GOOGLE_BACKEND_SYNC_RETRY_DELAY_SECONDS:-20}"

usage() {
  cat >&2 <<'EOF'
Usage:
  verify_google_backend_sync.sh [--detail-limit N]

Read-only check that Google Sheets data rows and active backend orders are synchronized.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --detail-limit)
      if [[ $# -lt 2 || ! "$2" =~ ^[0-9]+$ ]]; then
        usage
        exit 2
      fi
      DETAIL_LIMIT="$2"
      shift 2
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

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Env file not found: $ENV_FILE" >&2
  exit 1
fi

attempt=1
while true; do
  set +e
  OUTPUT="$(
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T backend-api \
      python -m app.google_backend_sync_diagnostic --detail-limit "$DETAIL_LIMIT" 2>&1
  )"
  STATUS="$?"
  set -e

  if [[ "$STATUS" -eq 0 ]]; then
    echo "$OUTPUT"
    exit 0
  fi

  if [[ "$OUTPUT" != *"Quota exceeded"* && "$OUTPUT" != *"APIError: [429]"* ]]; then
    echo "$OUTPUT" >&2
    exit "$STATUS"
  fi

  if [[ "$attempt" -ge "$MAX_ATTEMPTS" ]]; then
    echo "$OUTPUT" >&2
    exit "$STATUS"
  fi

  sleep "$RETRY_DELAY_SECONDS"
  attempt=$((attempt + 1))
done
