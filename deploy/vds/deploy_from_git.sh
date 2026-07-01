#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${TAKSKLAD_DEPLOY_APP_DIR:-/opt/stacks/taksklad/app}"
DEPLOY_REF="${TAKSKLAD_DEPLOY_REF:-main}"
SERVICES_INPUT="${TAKSKLAD_DEPLOY_SERVICES:-all}"
ACCEPTANCE_MODE="${TAKSKLAD_DEPLOY_ACCEPTANCE:-optional}"
ENV_FILE="${TAKSKLAD_ENV_FILE:-deploy/vds/.env}"
COMPOSE_FILE="deploy/vds/docker-compose.yml"
RESTORE_ROOT="${TAKSKLAD_RESTORE_ROOT:-/opt/stacks/taksklad/restore_points}"
HEALTH_URL="${TAKSKLAD_HEALTH_URL:-https://api.taksklad.uz/health}"
READY_URL="${TAKSKLAD_READY_URL:-https://api.taksklad.uz/ready}"
LOG_SINCE_SECONDS="${TAKSKLAD_DEPLOY_LOG_SINCE_SECONDS:-120}"
REMOTE_URL="${TAKSKLAD_DEPLOY_REMOTE_URL:-https://github.com/1fear/TakSklad.git}"

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
  TAKSKLAD_DEPLOY_ACCEPTANCE    optional|required|skip. Default: optional
  TAKSKLAD_DEPLOY_REMOTE_URL    Git remote for non-git app dirs. Default: https://github.com/1fear/TakSklad.git
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

run_acceptance() {
  case "$ACCEPTANCE_MODE" in
    optional|required|skip) ;;
    *) fail "TAKSKLAD_DEPLOY_ACCEPTANCE must be optional, required, or skip" ;;
  esac
  [[ "$ACCEPTANCE_MODE" == "skip" ]] && return 0

  if [[ ! -x deploy/vds/acceptance_status.sh ]]; then
    [[ "$ACCEPTANCE_MODE" == "required" ]] && fail "acceptance_status.sh is not executable"
    echo "acceptance_status.sh skipped: script is not executable"
    return 0
  fi
  if [[ ! -f outputs/taksklad_acceptance/acceptance_manifest.json ]]; then
    [[ "$ACCEPTANCE_MODE" == "required" ]] && fail "acceptance manifest is missing"
    echo "acceptance_status.sh skipped: acceptance manifest is missing"
    return 0
  fi

  if ./deploy/vds/acceptance_status.sh; then
    return 0
  fi

  [[ "$ACCEPTANCE_MODE" == "required" ]] && fail "acceptance_status.sh failed"
  echo "acceptance_status.sh reported no-go; continuing because acceptance mode is optional"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

cd "$APP_DIR"

[[ -f "$ENV_FILE" ]] || fail "env file not found: $APP_DIR/$ENV_FILE"
[[ -f "$COMPOSE_FILE" ]] || fail "compose file not found: $APP_DIR/$COMPOSE_FILE"

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

echo "Rebuilding and recreating services: ${SERVICES[*]}"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --build "${SERVICES[@]}"

echo "Checking public health..."
curl -fsS "$HEALTH_URL"
echo

echo "Checking public readiness..."
curl -fsS "$READY_URL"
echo

echo "Running acceptance check mode: $ACCEPTANCE_MODE"
run_acceptance

echo "Scanning fresh container logs..."
run_log_scan "$LOG_SINCE_SECONDS" "${SERVICES[@]}"

echo "Production deploy completed."
