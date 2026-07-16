#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="${TAKSKLAD_ENV_FILE:-$SCRIPT_DIR/.env}"
COMPOSE_FILE="${TAKSKLAD_COMPOSE_FILE:-$SCRIPT_DIR/docker-compose.yml}"
STATE_DIR="${TAKSKLAD_RESTORE_STATE_DIR:-/opt/taksklad/restore-state}"
WRITER_SERVICES=(backend-api skladbot-worker smartup-auto-import-worker telegram-worker)

usage() {
  cat >&2 <<'EOF'
Usage:
  TAKSKLAD_PRODUCTION_RESTORE_APPROVAL='APPROVE_TAKSKLAD_PRODUCTION_RESTORE <backup-id> <sha256>' \
    ./deploy/vds/restore_postgres.sh --restore /path/to/<backup-id>.manifest.json

  TAKSKLAD_PRODUCTION_RESTORE_OPERATOR_APPROVAL='APPROVE_TAKSKLAD_PRODUCTION_RESTORE_OPERATOR <restore-id>' \
    ./deploy/vds/restore_postgres.sh --resume-after-operator-check /opt/taksklad/restore-state/<restore-id>.json
EOF
  exit 2
}

[[ $# -eq 2 ]] || usage
MODE="$1"
INPUT_FILE="$2"
[[ "$MODE" == "--restore" || "$MODE" == "--resume-after-operator-check" ]] || usage
[[ -f "$INPUT_FILE" ]] || { echo "Required file not found: $INPUT_FILE" >&2; exit 1; }
[[ -f "$COMPOSE_FILE" ]] || { echo "Missing compose file: $COMPOSE_FILE" >&2; exit 1; }

COMPOSE_FILE="$(cd "$(dirname "$COMPOSE_FILE")" && pwd)/$(basename "$COMPOSE_FILE")"
INPUT_FILE="$(cd "$(dirname "$INPUT_FILE")" && pwd)/$(basename "$INPUT_FILE")"

if [[ "$MODE" == "--resume-after-operator-check" ]]; then
  [[ -f "$ENV_FILE" ]] || { echo "Missing env file: $ENV_FILE" >&2; exit 1; }
  ENV_FILE="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"
  restore_id="$(python3 - "$INPUT_FILE" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload.get("schema") != "taksklad-production-restore-state-v1":
    raise SystemExit("invalid restore state schema")
if payload.get("status") != "awaiting_operator_validation":
    raise SystemExit("restore state is not awaiting operator validation")
print(payload["restore_id"])
PY
)"
  expected="APPROVE_TAKSKLAD_PRODUCTION_RESTORE_OPERATOR $restore_id"
  if [[ "${TAKSKLAD_PRODUCTION_RESTORE_OPERATOR_APPROVAL:-}" != "$expected" ]]; then
    echo "Operator validation is required. Set the exact approval: $expected" >&2
    exit 1
  fi
  set -a
  # shellcheck disable=SC1090 -- production-only operator-provided file.
  source "$ENV_FILE"
  set +a
  cd "$APP_DIR"
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --no-build "${WRITER_SERVICES[@]}"
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T backend-api \
    python -c "import json; from urllib.request import urlopen; p=json.load(urlopen('http://127.0.0.1:8000/ready', timeout=10)); assert p.get('ready') is True and (p.get('migrations') or {}).get('status') == 'ok'"
  "$SCRIPT_DIR/acceptance_status.sh" --require-go
  python3 - "$INPUT_FILE" <<'PY'
import json, os, pathlib, sys, tempfile
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["status"] = "complete"
payload["operator_validation"] = "approved"
fd, partial = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, indent=2)
        stream.write("\n")
    os.replace(partial, path)
finally:
    if os.path.exists(partial):
        os.unlink(partial)
PY
  echo "PRODUCTION_RESTORE_COMPLETE restore_id=$restore_id readiness=ok acceptance=go operator_validation=approved writers=running"
  exit 0
fi

manifest_values="$(python3 - "$INPUT_FILE" <<'PY'
import hashlib
import json
import pathlib
import sys

manifest_path = pathlib.Path(sys.argv[1])
def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
archive_info = manifest.get("archive") or {}
list_info = archive_info.get("list") or {}
if (
    manifest.get("schema_version") != 2
    or manifest.get("sanitized_manifest") is not True
    or manifest.get("atomic_bundle") is not True
):
    raise SystemExit("unsupported or unsanitized backup manifest")
if manifest.get("source") not in {"postgresql", "legacy-postgresql-plain-sql"} or manifest.get("contains_customer_content") is not True:
    raise SystemExit("synthetic/non-production backup cannot be used for production restore")
archive_format = archive_info.get("format")
if archive_format not in {"postgresql-custom", "postgresql-plain-sql-gzip-legacy-transition"} or archive_info.get("validated") is not True:
    raise SystemExit("backup archive was not validated")
backup_id = manifest.get("backup_id")
filename = archive_info.get("filename")
expected_sha = archive_info.get("sha256")
if not all(isinstance(v, str) and v for v in (backup_id, filename, expected_sha)):
    raise SystemExit("backup manifest is incomplete")
archive = (manifest_path.parent / filename).resolve()
if archive.parent != manifest_path.parent.resolve() or not archive.is_file():
    raise SystemExit("manifest archive path is invalid")
digest = file_sha256(archive)
if digest != expected_sha:
    raise SystemExit("backup checksum mismatch")
list_file = (manifest_path.parent / str(list_info.get("filename"))).resolve()
if list_file.parent != manifest_path.parent.resolve() or not list_file.is_file():
    raise SystemExit("manifest archive list path is invalid")
if file_sha256(list_file) != list_info.get("sha256"):
    raise SystemExit("backup archive list checksum mismatch")
if list_info.get("validated") is not True or not isinstance(list_info.get("entries"), int) or list_info["entries"] < 1:
    raise SystemExit("backup archive list was not validated")
sidecar = (manifest_path.parent / str(archive_info.get("checksum_sidecar"))).resolve()
if sidecar.parent != manifest_path.parent.resolve() or not sidecar.is_file():
    raise SystemExit("backup checksum sidecar is invalid")
if sidecar.read_text(encoding="utf-8").strip() != f"{digest}  {archive.name}":
    raise SystemExit("backup checksum sidecar mismatch")
print(f"{archive}\t{backup_id}\t{digest}\t{archive_format}\t{list_file}")
PY
)
"
IFS=$'\t' read -r BACKUP_FILE BACKUP_ID BACKUP_SHA256 BACKUP_FORMAT LIST_FILE <<<"$manifest_values"
if [[ "$BACKUP_FORMAT" == "postgresql-plain-sql-gzip-legacy-transition" ]]; then
  gzip -t "$BACKUP_FILE"
fi

expected_approval="APPROVE_TAKSKLAD_PRODUCTION_RESTORE $BACKUP_ID $BACKUP_SHA256"
if [[ "${TAKSKLAD_PRODUCTION_RESTORE_APPROVAL:-}" != "$expected_approval" ]]; then
  echo "Destructive restore denied. Set the exact approval: $expected_approval" >&2
  exit 1
fi

# Prove the selected artifact in a disposable PostgreSQL instance before any
# production environment file, service or database is touched.
"$SCRIPT_DIR/restore_drill.sh" "$INPUT_FILE"

[[ -f "$ENV_FILE" ]] || { echo "Missing env file: $ENV_FILE" >&2; exit 1; }
ENV_FILE="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"
set -a
# shellcheck disable=SC1090 -- production-only operator-provided file.
source "$ENV_FILE"
set +a

cd "$APP_DIR"
restore_id="restore-$(date -u +%Y%m%dT%H%M%SZ)-${BACKUP_ID//[^a-zA-Z0-9_.-]/_}"
restore_failed=true
keep_writers_drained() {
  if [[ "$restore_failed" == true ]]; then
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" stop "${WRITER_SERVICES[@]}" >/dev/null 2>&1 || true
    echo "Restore did not complete automated validation; all writers remain stopped." >&2
  fi
}
trap keep_writers_drained EXIT

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" stop "${WRITER_SERVICES[@]}"
running_services="$(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps --status running --services)"
for service in "${WRITER_SERVICES[@]}"; do
  if grep -Fxq "$service" <<<"$running_services"; then
    echo "Writer drain failed: $service is still running" >&2
    exit 1
  fi
done

active_sessions="$(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
  psql -U "$POSTGRES_USER" "$POSTGRES_DB" -At -v ON_ERROR_STOP=1 \
  -c "select count(*) from pg_stat_activity where datname = current_database() and pid <> pg_backend_pid() and state <> 'idle';")"
[[ "$active_sessions" == "0" ]] || {
  echo "Writer drain failed: active database sessions=$active_sessions" >&2
  exit 1
}

pre_restore_output="$($SCRIPT_DIR/backup_postgres.sh)"
pre_restore_manifest="$(awk -F'manifest=' '/^BACKUP_OK / {print $2}' <<<"$pre_restore_output" | awk '{print $1}' | tail -n 1)"
[[ -n "$pre_restore_manifest" && -f "$pre_restore_manifest" ]] || {
  echo "Pre-restore backup did not produce a verified manifest." >&2
  exit 1
}
python3 - "$pre_restore_manifest" <<'PY'
import hashlib, json, pathlib, sys
manifest_path = pathlib.Path(sys.argv[1])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
archive_info = manifest.get("archive") or {}
list_info = archive_info.get("list") or {}
if (
    manifest.get("schema_version") != 2
    or manifest.get("source") != "postgresql"
    or manifest.get("atomic_bundle") is not True
):
    raise SystemExit("pre-restore backup manifest is invalid")
if archive_info.get("validated") is not True or list_info.get("validated") is not True:
    raise SystemExit("pre-restore backup validation is incomplete")
def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()
archive = manifest_path.parent / str(archive_info.get("filename"))
inventory = manifest_path.parent / str(list_info.get("filename"))
if digest(archive) != archive_info.get("sha256") or digest(inventory) != list_info.get("sha256"):
    raise SystemExit("pre-restore backup checksum validation failed")
PY

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
  psql -U "$POSTGRES_USER" "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
  -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
if [[ "$BACKUP_FORMAT" == "postgresql-custom" ]]; then
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
    pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --exit-on-error --no-owner --no-acl <"$BACKUP_FILE" >/dev/null
else
  gzip -dc "$BACKUP_FILE" | docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
    psql -U "$POSTGRES_USER" "$POSTGRES_DB" -v ON_ERROR_STOP=1 >/dev/null
fi

# Recovery is forward-only: migrations may advance to head, but this procedure never downgrades.
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" run --rm --no-deps backend-api \
  alembic -c alembic.ini upgrade head
head_revision="$(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" run --rm --no-deps backend-api \
  alembic -c alembic.ini heads | awk 'NR == 1 {print $1}')"
current_revision="$(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" run --rm --no-deps backend-api \
  alembic -c alembic.ini current | awk 'NR == 1 {print $1}')"
[[ -n "$head_revision" && "$current_revision" == "$head_revision" ]] || {
  echo "Restore migration mismatch: current=$current_revision head=$head_revision" >&2
  exit 1
}

counts="$(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
  psql -U "$POSTGRES_USER" "$POSTGRES_DB" -At -v ON_ERROR_STOP=1 \
  -c "select json_build_object('orders',(select count(*) from orders),'order_items',(select count(*) from order_items),'scan_codes',(select count(*) from scan_codes),'imports',(select count(*) from imports));")"
orphan_count="$(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T postgres \
  psql -U "$POSTGRES_USER" "$POSTGRES_DB" -At -v ON_ERROR_STOP=1 \
  -c "select (select count(*) from order_items i left join orders o on o.id=i.order_id where o.id is null) + (select count(*) from scan_codes s left join order_items i on i.id=s.order_item_id where i.id is null);")"
[[ "$orphan_count" == "0" ]] || { echo "Restore invariant failed: orphan_count=$orphan_count" >&2; exit 1; }

readiness="$(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" run --rm --no-deps backend-api \
  python -c "import json; from app.db import SessionLocal, settings; from app.health_service import build_readiness_report; db=SessionLocal(); p=build_readiness_report(db, settings); db.close(); print(json.dumps({'ready':p.get('ready'),'database':(p.get('database') or {}).get('status'),'migrations':(p.get('migrations') or {}).get('status')}))")"
grep -q '"database": "ok"' <<<"$readiness" || { echo "Restore database readiness failed: $readiness" >&2; exit 1; }
grep -q '"migrations": "ok"' <<<"$readiness" || { echo "Restore migration readiness failed: $readiness" >&2; exit 1; }

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
state_file="$STATE_DIR/$restore_id.json"
python3 - "$state_file" "$restore_id" "$BACKUP_ID" "$BACKUP_SHA256" "$pre_restore_manifest" "$head_revision" "$counts" <<'PY'
import json, os, pathlib, sys, tempfile
path = pathlib.Path(sys.argv[1])
payload = {
    "schema": "taksklad-production-restore-state-v1",
    "restore_id": sys.argv[2],
    "backup_id": sys.argv[3],
    "backup_sha256": sys.argv[4],
    "pre_restore_manifest_id": pathlib.Path(sys.argv[5]).stem,
    "migration_head": sys.argv[6],
    "counts": json.loads(sys.argv[7]),
    "invariants": "ok",
    "readiness": "database-and-migrations-ok",
    "full_policy_readiness": "deferred-until-operator-resume",
    "status": "awaiting_operator_validation",
    "customer_content_in_evidence": False,
}
fd, partial = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, indent=2)
        stream.write("\n")
    os.replace(partial, path)
finally:
    if os.path.exists(partial):
        os.unlink(partial)
PY
chmod 600 "$state_file"
restore_failed=false
trap - EXIT

echo "RESTORE_AUTOMATED_CHECKS_OK restore_id=$restore_id backup_id=$BACKUP_ID sha256=$BACKUP_SHA256 migration_head=$head_revision counts=$counts invariants=ok readiness=database-and-migrations-ok full_policy_readiness=deferred writers=stopped active_sessions=0 disposable_prevalidation=pass"
echo "AWAITING_OPERATOR_CHECK state=$state_file"
echo "After warehouse/operator validation, resume with exact approval: APPROVE_TAKSKLAD_PRODUCTION_RESTORE_OPERATOR $restore_id"
