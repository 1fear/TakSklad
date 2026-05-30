#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /path/to/taksklad-postgres-YYYYmmddTHHMMSSZ.sql.gz" >&2
  exit 1
fi

BACKUP_FILE="$1"
if [[ ! -f "$BACKUP_FILE" ]]; then
  echo "Backup file not found: $BACKUP_FILE" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

DRILL_DB="taksklad_restore_drill_$(date -u +%Y%m%d%H%M%S)"

cleanup() {
  cd "$APP_DIR"
  docker compose --env-file "$ENV_FILE" -f "$SCRIPT_DIR/docker-compose.yml" exec -T postgres \
    dropdb -U "$POSTGRES_USER" --if-exists "$DRILL_DB" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd "$APP_DIR"
docker compose --env-file "$ENV_FILE" -f "$SCRIPT_DIR/docker-compose.yml" exec -T postgres \
  createdb -U "$POSTGRES_USER" "$DRILL_DB"

gzip -dc "$BACKUP_FILE" | docker compose --env-file "$ENV_FILE" -f "$SCRIPT_DIR/docker-compose.yml" exec -T postgres \
  psql -U "$POSTGRES_USER" "$DRILL_DB" -v ON_ERROR_STOP=1 >/dev/null

docker compose --env-file "$ENV_FILE" -f "$SCRIPT_DIR/docker-compose.yml" exec -T postgres \
  psql -U "$POSTGRES_USER" "$DRILL_DB" -v ON_ERROR_STOP=1 \
  -c "select 'orders' as table_name, count(*) from orders union all select 'order_items', count(*) from order_items union all select 'scan_codes', count(*) from scan_codes union all select 'imports', count(*) from imports;" \
  -c "select 'restore_drill_ok' as status, now() as checked_at;"
