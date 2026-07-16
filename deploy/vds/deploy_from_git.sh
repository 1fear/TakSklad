#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${TAKSKLAD_DEPLOY_APP_DIR:-/opt/stacks/taksklad/app}"
ENV_FILE="${TAKSKLAD_ENV_FILE:-deploy/vds/.env}"
COMPOSE_FILE="deploy/vds/docker-compose.yml"
DEPLOY_RECORD="${TAKSKLAD_DEPLOY_RECORD:-/opt/stacks/taksklad/deployments/current-release.json}"
HEALTH_URL="${TAKSKLAD_HEALTH_URL:-https://api.taksklad.uz/health}"
READY_URL="${TAKSKLAD_READY_URL:-https://api.taksklad.uz/ready}"
URL_RETRY_ATTEMPTS="${TAKSKLAD_DEPLOY_URL_RETRY_ATTEMPTS:-30}"
URL_RETRY_INTERVAL_SECONDS="${TAKSKLAD_DEPLOY_URL_RETRY_INTERVAL_SECONDS:-2}"
COMPOSE_WAIT_TIMEOUT_SECONDS="${TAKSKLAD_COMPOSE_WAIT_TIMEOUT_SECONDS:-180}"
LOG_SINCE_SECONDS="${TAKSKLAD_DEPLOY_LOG_SINCE_SECONDS:-120}"
ACCEPTANCE_MODE="${TAKSKLAD_DEPLOY_ACCEPTANCE:-required}"
WRITER_SERVICES=(backend-api telegram-worker skladbot-worker smartup-auto-import-worker)
DRY_RUN=0
ARTIFACT_MANIFEST=""
PYTHON_BIN="python3"
if [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
fi

usage() {
  cat >&2 <<'EOF'
Usage:
  deploy_from_git.sh --artifact-manifest PATH
  deploy_from_git.sh --artifact-manifest PATH --acceptance required --wait
  deploy_from_git.sh --dry-run --artifact-manifest PATH [--acceptance required] [--wait]

Production execution requires TAKSKLAD_PRODUCTION_APPROVAL=READY_FOR_PRODUCTION_DEPLOY.
Only a verified GitHub/Sigstore release manifest is accepted outside --dry-run.
EOF
}

fail() {
  echo "deploy_from_git.sh: $*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --artifact-manifest)
      ARTIFACT_MANIFEST="${2:-}"
      shift 2
      ;;
    --acceptance)
      ACCEPTANCE_MODE="${2:-}"
      shift 2
      ;;
    --wait)
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

[[ -n "$ARTIFACT_MANIFEST" && -f "$ARTIFACT_MANIFEST" ]] || fail "artifact manifest is required"
ARTIFACT_MANIFEST="$(cd "$(dirname "$ARTIFACT_MANIFEST")" && pwd -P)/$(basename "$ARTIFACT_MANIFEST")"

if [[ "$DRY_RUN" == "1" ]]; then
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. "$PYTHON_BIN" tools/release_artifacts.py plan \
    --local --manifest "$ARTIFACT_MANIFEST"
  exit 0
fi

[[ "${TAKSKLAD_PRODUCTION_APPROVAL:-}" == "READY_FOR_PRODUCTION_DEPLOY" ]] || \
  fail "exact production approval is required"
[[ "$ACCEPTANCE_MODE" == "required" ]] || fail "acceptance must remain required"
[[ "$URL_RETRY_ATTEMPTS" =~ ^[0-9]+$ && "$URL_RETRY_ATTEMPTS" -gt 0 ]] || fail "invalid retry attempts"
[[ "$URL_RETRY_INTERVAL_SECONDS" =~ ^[0-9]+$ ]] || fail "invalid retry interval"
[[ "$COMPOSE_WAIT_TIMEOUT_SECONDS" =~ ^[0-9]+$ && "$COMPOSE_WAIT_TIMEOUT_SECONDS" -gt 0 ]] || fail "invalid Compose timeout"

cd "$APP_DIR"
[[ -f "$ENV_FILE" ]] || fail "production environment file is missing"
[[ -f "$COMPOSE_FILE" ]] || fail "Compose definition is missing"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. "$PYTHON_BIN" tools/release_artifacts.py verify \
  --manifest "$ARTIFACT_MANIFEST"
eval "$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. "$PYTHON_BIN" tools/release_artifacts.py emit-shell --manifest "$ARTIFACT_MANIFEST")"

export TAKSKLAD_BACKEND_IMAGE="$RELEASE_BACKEND_IMAGE"
export TAKSKLAD_FRONTEND_IMAGE="$RELEASE_FRONTEND_IMAGE"
export TAKSKLAD_COMMIT_SHA="$RELEASE_SOURCE_SHA"
export TAKSKLAD_IMAGE_DIGEST="$RELEASE_BACKEND_DIGEST"

PREVIOUS_MANIFEST=""
if [[ -f "$DEPLOY_RECORD" ]]; then
  PREVIOUS_MANIFEST="$(mktemp -t taksklad-previous-release.XXXXXX)"
  cp "$DEPLOY_RECORD" "$PREVIOUS_MANIFEST"
fi

cleanup() {
  [[ -z "$PREVIOUS_MANIFEST" ]] || rm -f "$PREVIOUS_MANIFEST"
}
trap cleanup EXIT

compose() {
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

validate_daily_report_config() {
  compose config --format json | \
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. "$PYTHON_BIN" tools/validate_daily_report_config.py
}

check_public_url() {
  local label="$1" url="$2" attempt output
  for ((attempt = 1; attempt <= URL_RETRY_ATTEMPTS; attempt += 1)); do
    if output="$(curl -fsS "$url" 2>&1)" && printf '%s' "$output" | \
      python3 tools/validate_deploy_probe.py "$label" \
        --expected-sha "$RELEASE_SOURCE_SHA" --expected-digest "$RELEASE_BACKEND_DIGEST"; then
      printf '%s\n' "$output"
      return 0
    fi
    ((attempt == URL_RETRY_ATTEMPTS)) || sleep "$URL_RETRY_INTERVAL_SECONDS"
  done
  echo "$output" >&2
  return 1
}

run_acceptance() {
  [[ "$ACCEPTANCE_MODE" == "required" ]] || return 1
  [[ -x deploy/vds/acceptance_status.sh ]] || return 1
  [[ -f outputs/taksklad_acceptance/acceptance_manifest.json ]] || return 1
  ./deploy/vds/acceptance_status.sh --require-go
}

run_log_scan() {
  local output
  output="$(compose logs --since "${LOG_SINCE_SECONDS}s" \
    backend-api frontend telegram-worker skladbot-worker smartup-auto-import-worker 2>&1 || true)"
  if printf '%s\n' "$output" | grep -Eiq \
    '\[(ERROR|CRITICAL)\]|(^|[[:space:]])(ERROR|CRITICAL)(:|[[:space:]])|Traceback \(most recent call last\):|(^|[[:space:]])Exception:|(^|[[:space:]])panic:'; then
    printf '%s\n' "$output" >&2
    return 1
  fi
}

verify_db_only_compose() {
  if compose config --services | grep -Fxq "google-sheets-sync-worker"; then
    fail "Compose still declares the retired google-sheets-sync-worker"
  fi
  compose config --format json | python3 -c '
import json, sys
config = json.load(sys.stdin)
environment = ((config.get("services") or {}).get("backend-api") or {}).get("environment") or {}
required = str(environment.get("TAKSKLAD_REQUIRED_WORKERS") or "")
workers = {value.strip() for value in required.split(",") if value.strip()}
if "google_sheets_sync" in workers:
    raise SystemExit("TAKSKLAD_REQUIRED_WORKERS still requires google_sheets_sync")
missing = {"skladbot", "smartup_auto_import", "telegram"} - workers
if missing:
    raise SystemExit("TAKSKLAD_REQUIRED_WORKERS misses DB runtime workers: " + ",".join(sorted(missing)))
'
}

legacy_google_worker_ids() {
  local project
  project="$(compose config --format json | python3 -c '
import json, sys
name = str((json.load(sys.stdin) or {}).get("name") or "").strip()
if not name:
    raise SystemExit("Compose project name is required")
print(name)
')"
  docker ps -aq \
    --filter "label=com.docker.compose.project=$project" \
    --filter "label=com.docker.compose.service=google-sheets-sync-worker"
}

ensure_legacy_google_worker_absent() {
  [[ -z "$(legacy_google_worker_ids)" ]] || fail "retired google-sheets-sync-worker container still exists"
}

remove_legacy_google_worker() {
  local ids
  ids="$(legacy_google_worker_ids)"
  if [[ -n "$ids" ]]; then
    docker container stop -t 45 $ids
    docker container rm -f $ids
  fi
  ensure_legacy_google_worker_absent
}

ensure_writer_services_stopped() {
  local running service
  running="$(compose ps --status running --services)"
  for service in "${WRITER_SERVICES[@]}"; do
    if grep -Fxq "$service" <<<"$running"; then
      fail "legacy writer is still running during DB-only cutover: $service"
    fi
  done
}

active_legacy_google_event_count() {
  compose exec -T postgres sh -ec \
    'psql -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select count(*) from pending_events where event_type = '\''google_sheets_export'\'' and status in ('\''pending'\'','\''failed'\'','\''error'\'','\''processing'\'','\''blocked'\'','\''active'\'','\''waiting_shipment_date'\'','\''waiting_date_choice'\'')"'
}

ensure_no_active_legacy_google_events() {
  local count
  count="$(active_legacy_google_event_count)"
  [[ "$count" == "0" ]] || fail "active legacy Google events remain after cutover migration: $count"
}

rollback_runtime() {
  [[ -n "$PREVIOUS_MANIFEST" ]] || {
    echo "No previous verified digest record is available; database schema remains current." >&2
    return 1
  }
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. "$PYTHON_BIN" tools/release_artifacts.py verify --manifest "$PREVIOUS_MANIFEST"
  eval "$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. "$PYTHON_BIN" tools/release_artifacts.py emit-shell --manifest "$PREVIOUS_MANIFEST")"
  export TAKSKLAD_BACKEND_IMAGE="$RELEASE_BACKEND_IMAGE"
  export TAKSKLAD_FRONTEND_IMAGE="$RELEASE_FRONTEND_IMAGE"
  export TAKSKLAD_COMMIT_SHA="$RELEASE_SOURCE_SHA"
  export TAKSKLAD_IMAGE_DIGEST="$RELEASE_BACKEND_DIGEST"
  docker pull "$TAKSKLAD_BACKEND_IMAGE"
  docker pull "$TAKSKLAD_FRONTEND_IMAGE"
  local database_revision previous_runtime_revision
  database_revision="$(compose exec -T postgres sh -ec \
    'psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select version_num from alembic_version"')"
  previous_runtime_revision="$(compose run --rm --no-deps --pull never \
    backend-api alembic -c alembic.ini heads | tail -n 1 | awk '{print $1}')"
  if [[ -z "$database_revision" || -z "$previous_runtime_revision" || \
        "$database_revision" != "$previous_runtime_revision" ]]; then
    echo "Rollback refused: previous runtime migration head does not match the retained database schema; candidate runtime remains selected." >&2
    return 1
  fi
  compose up -d --no-deps --no-build --pull never --wait --wait-timeout "$COMPOSE_WAIT_TIMEOUT_SECONDS" \
    backend-api frontend "${WRITER_SERVICES[@]:1}"
  ensure_legacy_google_worker_absent
  echo "Runtime rolled back to previous verified digests without the retired Google worker; database schema retained, alembic downgrade=0."
}

verify_db_only_compose
validate_daily_report_config || fail "production daily-report configuration is incomplete"

echo "Pulling verified immutable image subjects..."
docker pull "$TAKSKLAD_BACKEND_IMAGE"
docker pull "$TAKSKLAD_FRONTEND_IMAGE"

echo "Reconciling writable output ownership for non-root workers..."
install -d -m 755 "$APP_DIR/outputs"
TAKSKLAD_OUTPUT_PERMISSIONS_IMAGE="$TAKSKLAD_BACKEND_IMAGE" \
  ./tools/reconcile_output_permissions.sh \
    --path "$APP_DIR/outputs" \
    --expected-parent "$APP_DIR" \
    --apply \
    --confirm PHASE22_CHANGE_OUTPUT_OWNER

echo "Quiescing every legacy database writer before the DB-only cutover..."
if ! compose stop -t 45 "${WRITER_SERVICES[@]}"; then
  rollback_runtime || true
  fail "legacy database writers could not be quiesced; previous runtime selected when available"
fi
ensure_writer_services_stopped

echo "Stopping and removing the retired Google Sheets worker before the exact cutover backup..."
remove_legacy_google_worker

echo "Creating the exact PostgreSQL cutover backup after all legacy writers stopped..."
if ! ./deploy/vds/backup_postgres.sh --no-prune; then
  rollback_runtime || true
  fail "cutover backup failed; previous schema-compatible runtime selected when available"
fi

echo "Applying forward-only migrations from the verified backend image..."
if ! compose run --rm --no-deps backend-api alembic -c alembic.ini upgrade head; then
  rollback_runtime || true
  fail "forward-only migration failed; previous runtime selected only when schema-compatible"
fi
ensure_no_active_legacy_google_events

echo "Recovering leases owned by the stopped worker processes..."
if ! compose run --rm --no-deps backend-api python -m app.event_lease_recovery; then
  rollback_runtime || true
  fail "in-flight event leases could not be recovered; previous runtime selected when available"
fi

echo "Activating verified image digests without source build..."
if ! compose up -d --no-deps --no-build --pull never --wait --wait-timeout "$COMPOSE_WAIT_TIMEOUT_SECONDS" \
  backend-api frontend "${WRITER_SERVICES[@]:1}"; then
  rollback_runtime || true
  fail "candidate containers failed to activate; previous runtime selected when available"
fi
ensure_legacy_google_worker_absent

if ! check_public_url health "$HEALTH_URL" || ! check_public_url readiness "$READY_URL"; then
  rollback_runtime || true
  fail "candidate readiness failed; previous digest selected when available; database schema retained"
fi

run_acceptance || {
  rollback_runtime || true
  fail "mandatory acceptance failed; database schema retained"
}
run_log_scan || {
  rollback_runtime || true
  fail "fresh runtime logs failed; database schema retained"
}

install -d -m 700 "$(dirname "$DEPLOY_RECORD")"
temporary_record="${DEPLOY_RECORD}.tmp.$$"
install -m 600 "$ARTIFACT_MANIFEST" "$temporary_record"
mv -f "$temporary_record" "$DEPLOY_RECORD"
echo "Production deploy completed from verified immutable artifacts."
