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
    backend-api frontend telegram-worker google-sheets-sync-worker skladbot-worker smartup-auto-import-worker 2>&1 || true)"
  if printf '%s\n' "$output" | grep -Eiq 'ERROR|CRITICAL|Traceback|Exception|panic'; then
    printf '%s\n' "$output" >&2
    return 1
  fi
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
    backend-api frontend telegram-worker google-sheets-sync-worker skladbot-worker smartup-auto-import-worker
  echo "Runtime rolled back to previous verified digests; database schema retained, alembic downgrade=0."
}

echo "Creating PostgreSQL backup before forward-only migration..."
./deploy/vds/backup_postgres.sh --no-prune

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

echo "Applying forward-only migrations from the verified backend image..."
compose run --rm --no-deps backend-api alembic -c alembic.ini upgrade head

echo "Quiescing background workers before runtime replacement..."
if ! compose stop -t 45 \
  telegram-worker google-sheets-sync-worker skladbot-worker smartup-auto-import-worker; then
  rollback_runtime || true
  fail "background workers could not be quiesced; previous runtime selected when available"
fi

echo "Recovering leases owned by the stopped worker processes..."
if ! compose run --rm --no-deps backend-api python -m app.event_lease_recovery; then
  rollback_runtime || true
  fail "in-flight event leases could not be recovered; previous runtime selected when available"
fi

echo "Activating verified image digests without source build..."
if ! compose up -d --no-deps --no-build --pull never --wait --wait-timeout "$COMPOSE_WAIT_TIMEOUT_SECONDS" \
  backend-api frontend telegram-worker google-sheets-sync-worker skladbot-worker smartup-auto-import-worker; then
  rollback_runtime || true
  fail "candidate containers failed to activate; previous runtime selected when available"
fi

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
