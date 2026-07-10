#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="${TAKSKLAD_ENV_FILE:-$SCRIPT_DIR/.env}"
COMPOSE_FILE="${TAKSKLAD_COMPOSE_FILE:-$SCRIPT_DIR/docker-compose.yml}"
BACKUP_DIR="${TAKSKLAD_BACKUP_DIR:-/opt/taksklad/backups/postgres}"
RETENTION_DAYS="${TAKSKLAD_BACKUP_RETENTION_DAYS:-14}"
POSTGRES_IMAGE="postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
TEST_MODE=false
SYNTHETIC_DB=false
SIMULATE_FAILURE=false
container_name=""
staging_dir=""

usage() {
  cat <<'EOF'
Usage:
  backup_postgres.sh
  backup_postgres.sh --test-mode --synthetic-db [--simulate-failure]

Test mode starts a digest-pinned disposable PostgreSQL with its data directory
on tmpfs, migrates it to Alembic head, inserts one content-free probe, creates a
real custom archive, and removes the container before publishing evidence.
It never reads an env file or contacts production.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --test-mode) TEST_MODE=true ;;
    --synthetic-db) SYNTHETIC_DB=true ;;
    --simulate-failure) SIMULATE_FAILURE=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ "$TEST_MODE" == true && "$SYNTHETIC_DB" != true ]]; then
  echo "--test-mode requires --synthetic-db" >&2
  exit 2
fi
if [[ "$TEST_MODE" != true && "$SYNTHETIC_DB" == true ]]; then
  echo "--synthetic-db is allowed only with --test-mode" >&2
  exit 2
fi
if [[ "$TEST_MODE" != true && "$SIMULATE_FAILURE" == true ]]; then
  echo "--simulate-failure is allowed only with --test-mode" >&2
  exit 2
fi

sha256_file() {
  shasum -a 256 "$1" | awk '{print $1}'
}

file_size() {
  if stat -f '%z' "$1" >/dev/null 2>&1; then
    stat -f '%z' "$1"
  else
    stat -c '%s' "$1"
  fi
}

cleanup() {
  if [[ -n "$container_name" ]]; then
    docker rm -f "$container_name" >/dev/null 2>&1 || true
  fi
  if [[ -n "$staging_dir" && -d "$staging_dir" ]]; then
    rm -rf "$staging_dir"
  fi
}
trap cleanup EXIT

if [[ "$TEST_MODE" == true ]]; then
  BACKUP_DIR="${TAKSKLAD_BACKUP_TEST_DIR:-$APP_DIR/test-artifacts/phase24/backups}"
fi
completed_root="$BACKUP_DIR/completed"
mkdir -p "$completed_root"
chmod 700 "$BACKUP_DIR" "$completed_root"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_id="taksklad-postgres-$timestamp"
if [[ "$TEST_MODE" == true ]]; then
  backup_id="$backup_id-synthetic-$$"
fi
bundle_dir="$completed_root/$backup_id"
staging_dir="$BACKUP_DIR/.staging-$backup_id-$$"
[[ ! -e "$bundle_dir" && ! -e "$staging_dir" ]] || {
  echo "Refusing to overwrite existing backup ID: $backup_id" >&2
  exit 1
}
mkdir -m 700 "$staging_dir"

archive_name="$backup_id.dump"
list_name="$backup_id.list"
checksum_name="$backup_id.sha256"
manifest_name="$backup_id.manifest.json"
archive_file="$staging_dir/$archive_name"
list_file="$staging_dir/$list_name"
checksum_file="$staging_dir/$checksum_name"
manifest_file="$staging_dir/$manifest_name"
source_kind="postgresql"
contains_customer_content=true
actual_postgresql=true
migration_head="unknown"
probe_count=0
disposable_cleanup_count=-1

if [[ "$TEST_MODE" == true ]]; then
  command -v docker >/dev/null || { echo "docker is required for actual synthetic PostgreSQL" >&2; exit 1; }
  [[ -x "$APP_DIR/.venv/bin/alembic" ]] || { echo "Project Alembic executable is required" >&2; exit 1; }

  source_kind="synthetic-postgresql"
  contains_customer_content=false
  container_name="taksklad-backup-synthetic-$$-$RANDOM"
  synthetic_user="synthetic"
  synthetic_password="synthetic-local-only-$RANDOM"
  synthetic_database="taksklad_synthetic"

  docker run -d --rm --name "$container_name" \
    -e "POSTGRES_USER=$synthetic_user" \
    -e "POSTGRES_PASSWORD=$synthetic_password" \
    -e "POSTGRES_DB=$synthetic_database" \
    --tmpfs /var/lib/postgresql/data:rw,nosuid,nodev,noexec,size=384m \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
    -p 127.0.0.1::5432 "$POSTGRES_IMAGE" >/dev/null

  ready=false
  for _ in $(seq 1 120); do
    if docker exec "$container_name" pg_isready -U "$synthetic_user" -d "$synthetic_database" >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 0.25
  done
  [[ "$ready" == true ]] || { echo "Disposable PostgreSQL did not become ready" >&2; exit 1; }

  mapped_port="$(docker port "$container_name" 5432/tcp | awk -F: '/127[.]0[.]0[.]1:/ {print $NF; exit}')"
  [[ "$mapped_port" =~ ^[0-9]+$ ]] || { echo "Disposable PostgreSQL port was not published safely" >&2; exit 1; }
  database_url="postgresql+psycopg://${synthetic_user}:${synthetic_password}@127.0.0.1:${mapped_port}/${synthetic_database}"
  migration_head="$(
    DATABASE_URL="$database_url" TAKSKLAD_ENV=test PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$APP_DIR/backend" \
      "$APP_DIR/.venv/bin/alembic" -c "$APP_DIR/backend/alembic.ini" heads | awk 'NR == 1 {print $1}'
  )"
  [[ -n "$migration_head" ]] || { echo "Alembic head was not resolved" >&2; exit 1; }
  DATABASE_URL="$database_url" TAKSKLAD_ENV=test PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$APP_DIR/backend" \
    "$APP_DIR/.venv/bin/alembic" -c "$APP_DIR/backend/alembic.ini" upgrade head >/dev/null

  current_revision="$(docker exec "$container_name" psql -U "$synthetic_user" -d "$synthetic_database" -At \
    -v ON_ERROR_STOP=1 -c 'select version_num from alembic_version;')"
  [[ "$current_revision" == "$migration_head" ]] || {
    echo "Synthetic PostgreSQL migration mismatch: current=$current_revision head=$migration_head" >&2
    exit 1
  }
  docker exec "$container_name" psql -U "$synthetic_user" -d "$synthetic_database" -v ON_ERROR_STOP=1 \
    -c 'create table synthetic_restore_probe (id integer primary key); insert into synthetic_restore_probe values (1);' \
    >/dev/null
  probe_count="$(docker exec "$container_name" psql -U "$synthetic_user" -d "$synthetic_database" -At \
    -v ON_ERROR_STOP=1 -c 'select count(*) from synthetic_restore_probe;')"
  business_rows="$(docker exec "$container_name" psql -U "$synthetic_user" -d "$synthetic_database" -At \
    -v ON_ERROR_STOP=1 -c 'select (select count(*) from orders) + (select count(*) from order_items) + (select count(*) from scan_codes) + (select count(*) from imports);')"
  [[ "$probe_count" == "1" && "$business_rows" == "0" ]] || {
    echo "Synthetic database content invariant failed" >&2
    exit 1
  }

  docker exec "$container_name" pg_dump -Fc -U "$synthetic_user" "$synthetic_database" >"$archive_file"
  docker exec -i "$container_name" pg_restore --list <"$archive_file" >"$list_file"
  docker rm -f "$container_name" >/dev/null
  container_name=""
  disposable_cleanup_count="$(docker ps -a --filter "name=^/taksklad-backup-synthetic-$$-" -q | wc -l | tr -d ' ')"
  [[ "$disposable_cleanup_count" == "0" ]] || { echo "Disposable PostgreSQL cleanup failed" >&2; exit 1; }
else
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "Missing env file: $ENV_FILE" >&2
    exit 1
  fi
  if [[ ! -f "$COMPOSE_FILE" ]]; then
    echo "Missing compose file: $COMPOSE_FILE" >&2
    exit 1
  fi
  ENV_FILE="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"
  COMPOSE_FILE="$(cd "$(dirname "$COMPOSE_FILE")" && pwd)/$(basename "$COMPOSE_FILE")"
  set -a
  # shellcheck disable=SC1090 -- operator-selected production env file.
  source "$ENV_FILE"
  set +a
  cd "$APP_DIR"
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
    pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB" >"$archive_file"
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
    pg_restore --list <"$archive_file" >"$list_file"
fi

chmod 600 "$archive_file" "$list_file"
if [[ "$SIMULATE_FAILURE" == true ]]; then
  echo "Synthetic failure requested before atomic bundle publication" >&2
  exit 86
fi
[[ "$(head -c 5 "$archive_file")" == "PGDMP" ]] || { echo "Invalid PostgreSQL custom archive header" >&2; exit 1; }
[[ -s "$list_file" ]] || { echo "PostgreSQL archive list is empty" >&2; exit 1; }
grep -q 'TABLE .* synthetic_restore_probe' "$list_file" || {
  if [[ "$TEST_MODE" == true ]]; then
    echo "Synthetic probe missing from real pg_restore list" >&2
    exit 1
  fi
}

archive_sha256="$(sha256_file "$archive_file")"
archive_bytes="$(file_size "$archive_file")"
list_sha256="$(sha256_file "$list_file")"
list_entries="$(awk 'NF && $1 !~ /^;/ {count += 1} END {print count + 0}' "$list_file")"
printf '%s  %s\n' "$archive_sha256" "$archive_name" >"$checksum_file"
chmod 600 "$checksum_file"
created_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

python3 - "$manifest_file" "$backup_id" "$archive_name" "$archive_sha256" \
  "$archive_bytes" "$created_at" "$source_kind" "$contains_customer_content" \
  "$actual_postgresql" "$migration_head" "$probe_count" "$disposable_cleanup_count" \
  "$checksum_name" "$list_name" "$list_sha256" "$list_entries" "$POSTGRES_IMAGE" <<'PY'
import json
import os
import sys

(destination, backup_id, archive_name, digest, size, created_at, source_kind,
 contains_customer_content, actual_postgresql, migration_head, probe_count,
 cleanup_count, checksum_name, list_name, list_sha256, list_entries,
 postgres_image) = sys.argv[1:]
payload = {
    "schema_version": 2,
    "backup_id": backup_id,
    "created_at_utc": created_at,
    "archive": {
        "filename": archive_name,
        "format": "postgresql-custom",
        "sha256": digest,
        "bytes": int(size),
        "validated": True,
        "validation": ["custom-header", "real-pg-restore-list"],
        "checksum_sidecar": checksum_name,
        "list": {
            "filename": list_name,
            "sha256": list_sha256,
            "entries": int(list_entries),
            "validated": True,
            "generated_by": "pg_restore --list",
        },
    },
    "source": source_kind,
    "actual_postgresql": actual_postgresql == "true",
    "postgres_image": postgres_image,
    "migration_head": migration_head,
    "synthetic_probe_count": int(probe_count),
    "disposable_cleanup_count": int(cleanup_count),
    "contains_customer_content": contains_customer_content == "true",
    "sanitized_manifest": True,
    "atomic_bundle": True,
}
with open(destination, "x", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
directory_fd = os.open(os.path.dirname(destination), os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
chmod 600 "$manifest_file"

mv "$staging_dir" "$bundle_dir"
staging_dir=""
trap - EXIT

if [[ "$TEST_MODE" != true ]]; then
  find "$completed_root" -mindepth 1 -maxdepth 1 -type d -name 'taksklad-postgres-*' \
    -mtime "+$RETENTION_DAYS" -exec rm -rf {} +
fi

maintenance_marker="${TAKSKLAD_MAINTENANCE_MARKER:-/run/taksklad-observability/maintenance.json}"
if [[ "$TEST_MODE" == true && -z "${TAKSKLAD_MAINTENANCE_MARKER:-}" ]]; then
  maintenance_marker="$BACKUP_DIR/maintenance.json"
fi
python3 "$APP_DIR/tools/write_maintenance_marker.py" backup --path "$maintenance_marker" >/dev/null

printf 'BACKUP_OK backup_id=%s format=postgresql-custom actual_postgresql=%s sha256=%s bytes=%s list_entries=%s cleanup_count=%s bundle=%s manifest=%s source=%s\n' \
  "$backup_id" "$actual_postgresql" "$archive_sha256" "$archive_bytes" "$list_entries" \
  "$disposable_cleanup_count" "$bundle_dir" "$bundle_dir/$manifest_name" "$source_kind"
