#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
SCHEMA_FILE="$APP_DIR/backend/sql/001_initial_schema.sql"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2
  exit 1
fi

if [[ ! -f "$SCHEMA_FILE" ]]; then
  echo "Missing schema file: $SCHEMA_FILE" >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

cd "$APP_DIR"
docker compose --env-file "$ENV_FILE" -f "$SCRIPT_DIR/docker-compose.yml" exec -T postgres \
  psql -U "$POSTGRES_USER" "$POSTGRES_DB" -v ON_ERROR_STOP=1 < "$SCHEMA_FILE"

echo "Schema applied from $SCHEMA_FILE"
