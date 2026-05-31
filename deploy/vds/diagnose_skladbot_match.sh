#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${TAKSKLAD_ENV_FILE:-$SCRIPT_DIR/.env}"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

usage() {
  cat >&2 <<'EOF'
Usage:
  diagnose_skladbot_match.sh [--marker MARKER] [--limit N] [--request-limit N]

Read-only SkladBot matching diagnostic.
It fetches recent SkladBot 3PL requests and compares them with active backend orders.
EOF
}

MARKER=""
LIMIT="20"
REQUEST_LIMIT="20"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --marker)
      if [[ $# -lt 2 ]]; then
        usage
        exit 2
      fi
      MARKER="$2"
      shift 2
      ;;
    --limit)
      if [[ $# -lt 2 || ! "$2" =~ ^[0-9]+$ ]]; then
        usage
        exit 2
      fi
      LIMIT="$2"
      shift 2
      ;;
    --request-limit)
      if [[ $# -lt 2 || ! "$2" =~ ^[0-9]+$ ]]; then
        usage
        exit 2
      fi
      REQUEST_LIMIT="$2"
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

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T backend-api \
  python -m app.skladbot_diagnostic --marker "$MARKER" --limit "$LIMIT" --request-limit "$REQUEST_LIMIT"
