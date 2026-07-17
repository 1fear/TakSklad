#!/usr/bin/env bash

# Exact-path lifecycle guard for sensitive routing candidates and env rollback.

phase27_candidate_guard_cleanup() {
  rm -f -- "$PHASE27_CANDIDATE_ENV" "$PHASE27_CANDIDATE_COMPOSE"
}

phase27_candidate_guard_on_exit() {
  local status=$?
  trap - EXIT
  phase27_candidate_guard_cleanup
  if test "$status" -ne 0 && test "$PHASE27_ENV_RECOVERY_APPLIED" -eq 1; then
    install -m 600 "$PHASE27_ENV_BACKUP" "$PHASE27_PERSISTED_ENV"
    echo "PRODUCTION_CONFIG_RECOVERY_ROLLED_BACK values_redacted=1" >&2
  fi
  exit "$status"
}

phase27_candidate_guard_init() {
  test "$#" -eq 3
  PHASE27_STATE_DIR=$1
  PHASE27_PERSISTED_ENV=$2
  PHASE27_ENV_BACKUP=$3
  test -d "$PHASE27_STATE_DIR"
  test ! -L "$PHASE27_STATE_DIR"
  chmod 700 "$PHASE27_STATE_DIR"
  test -f "$PHASE27_PERSISTED_ENV"
  test ! -L "$PHASE27_PERSISTED_ENV"
  test -f "$PHASE27_ENV_BACKUP"
  test ! -L "$PHASE27_ENV_BACKUP"
  PHASE27_CANDIDATE_ENV="$PHASE27_STATE_DIR/phase27-env-candidate"
  PHASE27_CANDIDATE_COMPOSE="$PHASE27_STATE_DIR/phase27-compose-candidate.json"
  PHASE27_ENV_RECOVERY_APPLIED=0
  umask 077
  phase27_candidate_guard_cleanup
  trap phase27_candidate_guard_on_exit EXIT
}

phase27_candidate_guard_create_compose() {
  test ! -e "$PHASE27_CANDIDATE_COMPOSE"
  test ! -L "$PHASE27_CANDIDATE_COMPOSE"
  install -m 600 /dev/null "$PHASE27_CANDIDATE_COMPOSE"
}

phase27_candidate_guard_mode() {
  if stat -c '%a' "$1" >/dev/null 2>&1; then
    stat -c '%a' "$1"
  else
    stat -f '%Lp' "$1"
  fi
}

phase27_candidate_guard_verify_modes() {
  test -f "$PHASE27_CANDIDATE_ENV"
  test ! -L "$PHASE27_CANDIDATE_ENV"
  test "$(phase27_candidate_guard_mode "$PHASE27_CANDIDATE_ENV")" = 600
  test -f "$PHASE27_CANDIDATE_COMPOSE"
  test ! -L "$PHASE27_CANDIDATE_COMPOSE"
  test "$(phase27_candidate_guard_mode "$PHASE27_CANDIDATE_COMPOSE")" = 600
  test "$(phase27_candidate_guard_mode "$PHASE27_STATE_DIR")" = 700
}

phase27_candidate_guard_mark_installed() {
  PHASE27_ENV_RECOVERY_APPLIED=1
}

phase27_candidate_guard_commit() {
  PHASE27_ENV_RECOVERY_APPLIED=0
  phase27_candidate_guard_cleanup
  trap - EXIT
}
