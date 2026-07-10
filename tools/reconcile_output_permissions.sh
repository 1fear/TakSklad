#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  echo "usage: $0 --path ROOT/outputs --expected-parent ROOT --check | --path ROOT/outputs --expected-parent ROOT --apply --confirm PHASE22_CHANGE_OUTPUT_OWNER" >&2
}

TARGET=""
EXPECTED_PARENT=""
MODE=""
CONFIRM=""
while (($#)); do
  case "$1" in
    --path)
      TARGET="${2:-}"
      shift 2
      ;;
    --expected-parent)
      EXPECTED_PARENT="${2:-}"
      shift 2
      ;;
    --check|--apply)
      [[ -z "$MODE" ]] || { usage; exit 2; }
      MODE="$1"
      shift
      ;;
    --confirm)
      CONFIRM="${2:-}"
      shift 2
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

[[ "$TARGET" == /* && "$EXPECTED_PARENT" == /* && -d "$TARGET" && -d "$EXPECTED_PARENT" && ! -L "$TARGET" && ! -L "$EXPECTED_PARENT" ]] || {
  echo "output-permissions: target must be an existing absolute non-symlink directory" >&2
  exit 2
}
TARGET_CANONICAL="$(cd "$TARGET" && pwd -P)"
PARENT_CANONICAL="$(cd "$EXPECTED_PARENT" && pwd -P)"
[[ "$PARENT_CANONICAL" != "/" && "$TARGET_CANONICAL" == "$PARENT_CANONICAL/outputs" ]] || {
  echo "output-permissions: target must be the outputs child of the expected non-root parent" >&2
  exit 2
}
[[ "$TARGET_CANONICAL" != *$'\n'* && "$TARGET_CANONICAL" != *,* ]] || {
  echo "output-permissions: unsafe target" >&2
  exit 2
}
[[ "$MODE" == "--check" || "$MODE" == "--apply" ]] || { usage; exit 2; }

ROOT_CONTRACT=0
if [[ -f "$PARENT_CANONICAL/deploy/vds/docker-compose.yml" && -f "$PARENT_CANONICAL/backend/Dockerfile" ]]; then
  ROOT_CONTRACT=1
elif [[ -f "$PARENT_CANONICAL/.taksklad-phase22-synthetic-root" ]]; then
  marker="$(<"$PARENT_CANONICAL/.taksklad-phase22-synthetic-root")"
  [[ "$marker" == "TAKSKLAD_PHASE22_SYNTHETIC_OUTPUT_ROOT" ]] && ROOT_CONTRACT=1
fi
[[ "$ROOT_CONTRACT" == "1" ]] || {
  echo "output-permissions: expected parent is not a confirmed TakSklad or synthetic root" >&2
  exit 2
}

IMAGE="vds-backend-api:latest"
docker image inspect "$IMAGE" >/dev/null
MOUNT_SOURCE="$TARGET_CANONICAL"
if [[ "$(uname -s)" == "Darwin" && "$MOUNT_SOURCE" == /private/var/* ]]; then
  MOUNT_SOURCE="${MOUNT_SOURCE#/private}"
fi
MOUNT="type=bind,src=$MOUNT_SOURCE,dst=/app/outputs"

check_write_access() {
  docker run --rm \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --user 10001:10001 \
    --mount "$MOUNT" \
    "$IMAGE" \
    python -c "import os; raise SystemExit(0 if os.access('/app/outputs', os.W_OK) else 1)"
}

if [[ "$MODE" == "--apply" ]]; then
  [[ "$CONFIRM" == "PHASE22_CHANGE_OUTPUT_OWNER" ]] || {
    echo "output-permissions: exact confirmation is required" >&2
    exit 2
  }
  docker run --rm \
    --read-only \
    --cap-drop ALL \
    --cap-add CHOWN \
    --security-opt no-new-privileges:true \
    --user 0:0 \
    --mount "$MOUNT" \
    "$IMAGE" \
    chown -R 10001:10001 /app/outputs
fi

check_write_access
echo "OUTPUT_PERMISSIONS_OK mode=${MODE#--} uid=10001 path_value_redacted=1"
