#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${TAKSKLAD_DEPLOY_APP_DIR:-/opt/stacks/taksklad/app}"
DEPLOY_REF="${TAKSKLAD_DEPLOY_REF:-main}"
SERVICES_INPUT="${TAKSKLAD_DEPLOY_SERVICES:-all}"
ACCEPTANCE_MODE="${TAKSKLAD_DEPLOY_ACCEPTANCE:-required}"
ENV_FILE="${TAKSKLAD_ENV_FILE:-deploy/vds/.env}"
COMPOSE_FILE="deploy/vds/docker-compose.yml"
RESTORE_ROOT="${TAKSKLAD_RESTORE_ROOT:-/opt/stacks/taksklad/restore_points}"
HEALTH_URL="${TAKSKLAD_HEALTH_URL:-https://api.taksklad.uz/health}"
READY_URL="${TAKSKLAD_READY_URL:-https://api.taksklad.uz/ready}"
LOG_SINCE_SECONDS="${TAKSKLAD_DEPLOY_LOG_SINCE_SECONDS:-120}"
REMOTE_URL="${TAKSKLAD_DEPLOY_REMOTE_URL:-https://github.com/1fear/TakSklad.git}"
URL_RETRY_ATTEMPTS="${TAKSKLAD_DEPLOY_URL_RETRY_ATTEMPTS:-30}"
URL_RETRY_INTERVAL_SECONDS="${TAKSKLAD_DEPLOY_URL_RETRY_INTERVAL_SECONDS:-2}"
COMPOSE_WAIT_TIMEOUT_SECONDS="${TAKSKLAD_COMPOSE_WAIT_TIMEOUT_SECONDS:-180}"

ALL_SERVICES=(
  backend-api
  frontend
  telegram-worker
  google-sheets-sync-worker
  skladbot-worker
  smartup-auto-import-worker
)

usage() {
  cat >&2 <<'EOF'
Usage:
  deploy_from_git.sh

Environment:
  TAKSKLAD_DEPLOY_APP_DIR       App checkout on VDS. Default: /opt/stacks/taksklad/app
  TAKSKLAD_DEPLOY_REF           Git branch, tag, or commit to deploy. Default: main
  TAKSKLAD_DEPLOY_SERVICES      Space/comma separated services, or all. Default: all
  TAKSKLAD_DEPLOY_ACCEPTANCE    Must be required. No production bypass is supported.
  TAKSKLAD_DEPLOY_REMOTE_URL    Git remote for non-git app dirs. Default: https://github.com/1fear/TakSklad.git
  TAKSKLAD_DEPLOY_URL_RETRY_ATTEMPTS           Public health/readiness attempts. Default: 30
  TAKSKLAD_DEPLOY_URL_RETRY_INTERVAL_SECONDS  Delay between public URL attempts. Default: 2
EOF
}

fail() {
  echo "deploy_from_git.sh: $*" >&2
  exit 1
}

contains() {
  local needle="$1"
  shift
  local item
  for item in "$@"; do
    [[ "$item" == "$needle" ]] && return 0
  done
  return 1
}

resolve_services() {
  local raw="$1"
  local normalized
  normalized="${raw//,/ }"
  if [[ "$normalized" == "all" ]]; then
    printf '%s\n' "${ALL_SERVICES[@]}"
    return 0
  fi
  local service
  for service in $normalized; do
    contains "$service" "${ALL_SERVICES[@]}" || fail "unsupported service: $service"
    printf '%s\n' "$service"
  done
}

restore_point() {
  local restore_id restore_dir path
  restore_id="pre-cicd-deploy-$(date -u +%Y%m%dT%H%M%SZ)"
  restore_dir="$RESTORE_ROOT/$restore_id"
  mkdir -p "$restore_dir"
  for path in backend deploy frontend docs tools .github version.json; do
    if [[ -e "$path" ]]; then
      rsync -a \
        --exclude '.env' \
        --exclude '.env.*' \
        --exclude 'node_modules' \
        --exclude 'dist' \
        --exclude '__pycache__' \
        --exclude '*.pyc' \
        "$path" "$restore_dir/"
    fi
  done
  echo "$restore_dir"
}

checkout_ref() {
  local ref="$1"
  git fetch --prune origin

  if git rev-parse --verify --quiet "origin/$ref^{commit}" >/dev/null; then
    if git show-ref --verify --quiet "refs/heads/$ref"; then
      git checkout --force "$ref"
    else
      git checkout --force -B "$ref" "origin/$ref"
    fi
    git pull --ff-only origin "$ref"
    return 0
  fi

  if git rev-parse --verify --quiet "$ref^{commit}" >/dev/null; then
    git -c advice.detachedHead=false checkout --force "$ref"
    return 0
  fi

  fail "cannot resolve git ref: $ref"
}

sync_ref_from_temporary_checkout() {
  local ref="$1"
  (
    local checkout_dir
    checkout_dir="$(mktemp -d /tmp/taksklad-deploy-checkout-XXXXXX)"
    trap 'rm -rf "$checkout_dir"' EXIT

    git clone --no-checkout "$REMOTE_URL" "$checkout_dir"
    cd "$checkout_dir"
    checkout_ref "$ref"
    git rev-parse --short HEAD
    rsync -a --delete \
      --exclude '.git' \
      --exclude '.env' \
      --exclude '.env.*' \
      --exclude '.venv' \
      --exclude 'venv' \
      --exclude 'outputs' \
      --exclude 'backups' \
      --exclude 'logs' \
      --exclude 'restore_points' \
      --exclude 'node_modules' \
      --exclude 'dist' \
      --exclude '__pycache__' \
      --exclude '*.pyc' \
      "$checkout_dir/" "$APP_DIR/"
  )
}

run_log_scan() {
  local since services
  since="${1:-120}"
  shift
  services=("$@")
  if ((${#services[@]} == 0)); then
    return 0
  fi
  local output
  set +e
  output="$(
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" logs --since "${since}s" "${services[@]}" 2>&1 |
      grep -Ei 'ERROR|CRITICAL|Traceback|Exception|panic'
  )"
  local status="$?"
  set -e
  if [[ "$status" -eq 0 ]]; then
    echo "$output" >&2
    fail "fresh container logs contain error patterns"
  fi
}

check_public_url() {
  local label="$1"
  local url="$2"
  local attempt output status validation_output validation_status

  for ((attempt = 1; attempt <= URL_RETRY_ATTEMPTS; attempt += 1)); do
    set +e
    output="$(curl -fsS "$url" 2>&1)"
    status="$?"
    set -e
    if [[ "$status" -eq 0 ]]; then
      set +e
      validation_output="$(printf '%s' "$output" | python3 tools/validate_deploy_probe.py "$label" 2>&1)"
      validation_status="$?"
      set -e
      if [[ "$validation_status" -eq 0 ]]; then
        echo "$output"
        return 0
      fi
      output="$validation_output"
      status="$validation_status"
    fi
    if ((attempt == URL_RETRY_ATTEMPTS)); then
      echo "$output" >&2
      if [[ "$label" == "readiness" ]]; then
        fail "readiness body contract failed: $url"
      fi
      fail "$label check failed: $url"
    fi
    echo "$label check failed on attempt $attempt/$URL_RETRY_ATTEMPTS; retrying in ${URL_RETRY_INTERVAL_SECONDS}s" >&2
    sleep "$URL_RETRY_INTERVAL_SECONDS"
  done
}

run_acceptance() {
  [[ "$ACCEPTANCE_MODE" == "required" ]] || fail "TAKSKLAD_DEPLOY_ACCEPTANCE must remain required"

  if [[ ! -x deploy/vds/acceptance_status.sh ]]; then
    fail "acceptance_status.sh is not executable"
  fi
  if [[ ! -f outputs/taksklad_acceptance/acceptance_manifest.json ]]; then
    fail "acceptance manifest is missing"
  fi

  ./deploy/vds/acceptance_status.sh --require-go || fail "acceptance_status.sh failed"
}

verify_migration_revision_before_activation() {
  local expected_output current_output expected_revision current_revision
  expected_output="$(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" run --rm backend-api \
    alembic -c alembic.ini heads)"
  current_output="$(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" run --rm backend-api \
    alembic -c alembic.ini current)"
  expected_revision="$(printf '%s\n' "$expected_output" | sed -nE 's/^([[:alnum:]_]+).*/\1/p')"
  current_revision="$(printf '%s\n' "$current_output" | sed -nE 's/^([[:alnum:]_]+).*/\1/p')"
  [[ -n "$expected_revision" ]] || fail "cannot determine expected Alembic head"
  [[ "$(printf '%s\n' "$expected_revision" | wc -l | tr -d ' ')" == "1" ]] || fail "multiple Alembic heads are forbidden"
  [[ "$current_revision" == "$expected_revision" ]] || fail "deployed migration revision is not current"
  echo "Migration pre-activation check: current=$current_revision expected=$expected_revision"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

cd "$APP_DIR"

[[ -f "$ENV_FILE" ]] || fail "env file not found: $APP_DIR/$ENV_FILE"
[[ -f "$COMPOSE_FILE" ]] || fail "compose file not found: $APP_DIR/$COMPOSE_FILE"
[[ "$URL_RETRY_ATTEMPTS" =~ ^[0-9]+$ ]] || fail "TAKSKLAD_DEPLOY_URL_RETRY_ATTEMPTS must be a positive integer"
((URL_RETRY_ATTEMPTS > 0)) || fail "TAKSKLAD_DEPLOY_URL_RETRY_ATTEMPTS must be greater than zero"
[[ "$URL_RETRY_INTERVAL_SECONDS" =~ ^[0-9]+$ ]] || fail "TAKSKLAD_DEPLOY_URL_RETRY_INTERVAL_SECONDS must be a non-negative integer"
[[ "$COMPOSE_WAIT_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] || fail "TAKSKLAD_COMPOSE_WAIT_TIMEOUT_SECONDS must be a positive integer"
((COMPOSE_WAIT_TIMEOUT_SECONDS > 0)) || fail "TAKSKLAD_COMPOSE_WAIT_TIMEOUT_SECONDS must be greater than zero"
[[ "$ACCEPTANCE_MODE" == "required" ]] || fail "TAKSKLAD_DEPLOY_ACCEPTANCE must remain required"

if [[ -d .git ]]; then
  tracked_changes="$(git status --short --untracked-files=no)"
  if [[ -n "$tracked_changes" ]]; then
    echo "$tracked_changes" >&2
    fail "tracked worktree changes must be resolved before CI/CD deploy"
  fi
else
  echo "App dir is not a git checkout; deploy will sync from temporary checkout."
fi

mapfile -t SERVICES < <(resolve_services "$SERVICES_INPUT")
((${#SERVICES[@]} > 0)) || fail "no services selected"

echo "Creating restore point..."
restore_dir="$(restore_point)"
echo "Restore point: $restore_dir"

echo "Creating PostgreSQL backup..."
./deploy/vds/backup_postgres.sh

echo "Checking out ref: $DEPLOY_REF"
if [[ -d .git ]]; then
  checkout_ref "$DEPLOY_REF"
  git rev-parse --short HEAD
else
  sync_ref_from_temporary_checkout "$DEPLOY_REF"
fi

echo "Building backend image for migration..."
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" build backend-api

echo "Applying Alembic migrations..."
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" run --rm backend-api \
  alembic -c alembic.ini upgrade head

echo "Verifying read-only migration revision before activation..."
verify_migration_revision_before_activation

echo "Rebuilding and recreating services: ${SERVICES[*]}"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --build --wait --wait-timeout "$COMPOSE_WAIT_TIMEOUT_SECONDS" "${SERVICES[@]}"

echo "Checking public health..."
check_public_url "health" "$HEALTH_URL"
echo

echo "Checking public readiness..."
check_public_url "readiness" "$READY_URL"
echo

echo "Running acceptance check mode: $ACCEPTANCE_MODE"
run_acceptance

echo "Scanning fresh container logs..."
run_log_scan "$LOG_SINCE_SECONDS" "${SERVICES[@]}"

echo "Production deploy completed."
