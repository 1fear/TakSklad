#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="${TAKSKLAD_ENV_FILE:-$SCRIPT_DIR/.env}"
SQL_DIR="$APP_DIR/backend/sql"

if [[ "${TAKSKLAD_LEGACY_SQL_BOOTSTRAP:-}" != "ALLOW_EMPTY_UNVERSIONED_DATABASE_ONLY" ]]; then
  echo "Legacy SQL bootstrap is disabled. Use Alembic for every normal empty database and upgrade." >&2
  echo "Set TAKSKLAD_LEGACY_SQL_BOOTSTRAP=ALLOW_EMPTY_UNVERSIONED_DATABASE_ONLY only for a reviewed legacy recovery." >&2
  exit 1
fi

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
existing_application_tables="$(
  docker compose --env-file "$ENV_FILE" -f "$SCRIPT_DIR/docker-compose.yml" exec -T postgres \
    psql -U "$POSTGRES_USER" "$POSTGRES_DB" -At -v ON_ERROR_STOP=1 -c \
    "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version';" \
    2>/dev/null
)"
alembic_version_tables="$(
  docker compose --env-file "$ENV_FILE" -f "$SCRIPT_DIR/docker-compose.yml" exec -T postgres \
    psql -U "$POSTGRES_USER" "$POSTGRES_DB" -At -v ON_ERROR_STOP=1 -c \
    "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public' AND tablename = 'alembic_version';" \
    2>/dev/null
)"

if [[ "$existing_application_tables" != "0" ]]; then
  echo "Refusing legacy SQL bootstrap: existing application tables detected." >&2
  exit 1
fi
if [[ "$alembic_version_tables" != "0" ]]; then
  echo "Refusing legacy SQL bootstrap: alembic_version already exists." >&2
  exit 1
fi

for schema_file in "$SQL_DIR"/*.sql; do
  echo "Applying schema: $schema_file"
  docker compose --env-file "$ENV_FILE" -f "$SCRIPT_DIR/docker-compose.yml" exec -T postgres \
    psql -U "$POSTGRES_USER" "$POSTGRES_DB" -v ON_ERROR_STOP=1 < "$schema_file"
done

echo "Legacy schema applied from $SQL_DIR. Alembic upgrade to head is mandatory before any runtime writer starts."
