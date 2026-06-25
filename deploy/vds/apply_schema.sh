#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="${TAKSKLAD_ENV_FILE:-$SCRIPT_DIR/.env}"
SQL_DIR="$APP_DIR/backend/sql"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2
  exit 1
fi

if [[ ! -d "$SQL_DIR" ]]; then
  echo "Missing SQL directory: $SQL_DIR" >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

cd "$APP_DIR"
for schema_file in "$SQL_DIR"/*.sql; do
  echo "Applying schema: $schema_file"
  docker compose --env-file "$ENV_FILE" -f "$SCRIPT_DIR/docker-compose.yml" exec -T postgres \
    psql -U "$POSTGRES_USER" "$POSTGRES_DB" -v ON_ERROR_STOP=1 < "$schema_file"
done

echo "Schema applied from $SQL_DIR"
