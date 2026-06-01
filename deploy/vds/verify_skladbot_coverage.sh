#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${TAKSKLAD_ENV_FILE:-$SCRIPT_DIR/.env}"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
MARKER=""
DETAIL_LIMIT="20"

usage() {
  cat >&2 <<'EOF'
Usage:
  verify_skladbot_coverage.sh [--marker MARKER] [--detail-limit N]

Read-only check that active backend orders have SkladBot request numbers.
EOF
}

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

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T backend-api \
  python -m app.skladbot_coverage_diagnostic --marker "$MARKER" --detail-limit "$DETAIL_LIMIT"
