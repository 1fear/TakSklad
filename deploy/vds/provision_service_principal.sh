#!/usr/bin/env bash
set -Eeuo pipefail

TAKSKLAD_BACKEND_IMAGE="${1:-}"
COMMAND="${2:-}"
KIND="${3:-}"
IDENTIFIER="${4:-}"
OPERATION_ID="${5:-}"
SOURCE_SHA="${6:-}"
RELEASE_TAG="${7:-}"
HANDOFF_DIR="/opt/stacks/taksklad/private"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTROL_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
if [[ "$KIND" == "acceptance" ]]; then
  HANDOFF_FILE="$HANDOFF_DIR/acceptance-canary.token"
  CONTAINER_HANDOFF_FILE="/run/taksklad-private/acceptance-canary.token"
else
  HANDOFF_FILE="$HANDOFF_DIR/desktop-token"
  CONTAINER_HANDOFF_FILE="/run/taksklad-private/desktop-token"
fi
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"
ENV_FILE="/opt/stacks/taksklad/app/deploy/vds/.env"
TOOLS_DIR="$CONTROL_ROOT/tools"
BACKUP_ROOT="${TAKSKLAD_PRINCIPAL_BACKUP_ROOT:-}"
BACKUP_RESULT_FILE="${TAKSKLAD_PRINCIPAL_BACKUP_RESULT_FILE:-}"
BACKUP_ARCHIVE_FILE="${TAKSKLAD_PRINCIPAL_BACKUP_ARCHIVE_FILE:-}"

fail() { echo "principal provisioning blocked: $*" >&2; exit 1; }

stat_uid() {
  if stat -c '%u' "$1" >/dev/null 2>&1; then
    stat -c '%u' "$1"
  else
    stat -f '%u' "$1"
  fi
}

stat_mode() {
  if stat -c '%a' "$1" >/dev/null 2>&1; then
    stat -c '%a' "$1"
  else
    stat -f '%Lp' "$1"
  fi
}

handoff_parent_safe() {
  [[ "$HANDOFF_DIR" == /* && ! -L "$HANDOFF_DIR" && -d "$HANDOFF_DIR" ]] || return 1
  [[ "$(stat_uid "$HANDOFF_DIR")" == "$(id -u)" ]] || return 1
  [[ "$(stat_mode "$HANDOFF_DIR")" =~ ^(700|750)$ ]]
}

validate_operation_backup() {
  python3 "$TOOLS_DIR/validate_fresh_principal_backup.py" \
    --root "$BACKUP_ROOT" \
    --result-file "$BACKUP_RESULT_FILE" \
    --expected-archive "$BACKUP_ARCHIVE_FILE" \
    --operation-id "$OPERATION_ID" \
    --expected-migration-head "$current_revision" >/dev/null
}

[[ "$COMMAND" =~ ^(provision|rotate|revoke|destroy-handoff|reactivate)$ ]] || fail "invalid command"
[[ "$KIND" =~ ^(acceptance|desktop)$ ]] || fail "invalid kind"
[[ "$COMMAND" != "destroy-handoff" || "$KIND" == "desktop" ]] || fail "destroy-handoff is desktop-only"
[[ "$IDENTIFIER" =~ ^[a-z0-9][a-z0-9._-]{2,119}$ ]] || fail "invalid identifier"
[[ "$KIND" != "acceptance" || "$IDENTIFIER" == "acceptance.release" ]] || fail "acceptance identifier must be canonical"
[[ "$OPERATION_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] || \
  fail "stable operation ID is required"
[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "exact release source SHA is required"
[[ "$RELEASE_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "exact immutable release tag is required"
[[ "$TAKSKLAD_BACKEND_IMAGE" =~ ^ghcr.io/1fear/taksklad-backend@sha256:[0-9a-f]{64}$ ]] || \
  fail "exact backend image digest is required"
[[ "$BACKUP_ROOT" == /* && "$BACKUP_RESULT_FILE" == /* && "$BACKUP_ARCHIVE_FILE" == /* ]] || \
  fail "exact operation-bound backup paths are required"
manual_approval="MANUAL_P0_BRIDGE:${COMMAND}:${KIND}:${IDENTIFIER}:${OPERATION_ID}:${SOURCE_SHA}:${RELEASE_TAG}:${TAKSKLAD_BACKEND_IMAGE}:BACKUP:${OPERATION_ID}"
[[ "${TAKSKLAD_MANUAL_P0_BRIDGE_APPROVAL:-}" == "$manual_approval" ]] || \
  fail "exact manual P0 bridge approval is required"
release_authority="VERIFIED_TAGGED_MAIN_RELEASE:${RELEASE_TAG}:${SOURCE_SHA}:${TAKSKLAD_BACKEND_IMAGE}"
[[ "${TAKSKLAD_MANUAL_P0_RELEASE_AUTHORITY:-}" == "$release_authority" ]] || \
  fail "external tagged-main release authority evidence is required"
[[ "${TAKSKLAD_PRINCIPAL_WRITE_APPROVAL:-}" == "ALLOW_SERVICE_PRINCIPAL_WRITE" ]] || \
  fail "exact prod-write approval is required"
if [[ "$COMMAND" == "destroy-handoff" ]]; then
  required_command_approval="DESTROY_${KIND^^}_HANDOFF"
else
  required_command_approval="${COMMAND^^}_${KIND^^}_PRINCIPAL"
fi
[[ "${TAKSKLAD_PRINCIPAL_COMMAND_APPROVAL:-}" == "$required_command_approval" ]] || \
  fail "exact command and role approval is required"
HANDOFF_PARENT_SAFE=0
if handoff_parent_safe; then
  HANDOFF_PARENT_SAFE=1
fi
if [[ "$COMMAND" != "revoke" && "$HANDOFF_PARENT_SAFE" != 1 ]]; then
  fail "protected handoff directory is missing or unsafe"
fi
if [[ "$COMMAND" != "revoke" ]]; then
  python3 "$TOOLS_DIR/validate_principal_handoff_residue.py" "$HANDOFF_DIR" >/dev/null || \
    fail "handoff crash residue requires explicit reconciliation"
fi
[[ -f "$COMPOSE_FILE" && ! -L "$COMPOSE_FILE" ]] || fail "pinned compose file is unavailable"
[[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "pinned environment file is unavailable"
apply_arg="--apply"
export TAKSKLAD_BACKEND_IMAGE
export TAKSKLAD_PROVISIONER_UID="$(id -u)"
export TAKSKLAD_PROVISIONER_GID="$(id -g)"
export TAKSKLAD_PRINCIPAL_COMMAND_APPROVAL="${TAKSKLAD_PRINCIPAL_COMMAND_APPROVAL:-}"
TAKSKLAD_PRINCIPAL_ADMIN_NETWORK="taksklad-principal-${OPERATION_ID//-/}"
export TAKSKLAD_PRINCIPAL_ADMIN_NETWORK
REVOKE_TEMP_DIR=""
REVOKE_CLEANUP_UNVERIFIED=0
TAKSKLAD_PRINCIPAL_HANDOFF_HOST_PATH="$HANDOFF_DIR"
export TAKSKLAD_PRINCIPAL_HANDOFF_HOST_PATH
cleanup_revoke_temp() {
  if [[ -n "$REVOKE_TEMP_DIR" && -d "$REVOKE_TEMP_DIR" ]]; then
    rmdir "$REVOKE_TEMP_DIR" || return 1
  fi
  return 0
}
trap 'status=$?; trap - EXIT; cleanup_revoke_temp || exit 1; exit "$status"' EXIT
rendered_identity="$(docker compose --profile principal-admin --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --format json | \
  python3 "$TOOLS_DIR/validate_principal_provisioner_compose.py" \
    "$TAKSKLAD_BACKEND_IMAGE" "$TAKSKLAD_PROVISIONER_UID" "$TAKSKLAD_PROVISIONER_GID" \
    "$TAKSKLAD_PRINCIPAL_HANDOFF_HOST_PATH")"
IFS='|' read -r COMPOSE_PROJECT ADMIN_NETWORK <<<"$rendered_identity"
[[ -n "$COMPOSE_PROJECT" && -n "$ADMIN_NETWORK" ]] || fail "rendered compose identity is invalid"
[[ "$ADMIN_NETWORK" == "$TAKSKLAD_PRINCIPAL_ADMIN_NETWORK" ]] || fail "operation network identity is invalid"

docker image inspect "$TAKSKLAD_BACKEND_IMAGE" >/dev/null 2>&1 || \
  fail "exact attested backend image is not locally staged"

postgres_short_ids="$(docker ps -q \
  --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
  --filter "label=com.docker.compose.service=postgres")"
[[ -n "$postgres_short_ids" && "$postgres_short_ids" != *$'\n'* ]] || fail "exact current postgres container was not resolved"
POSTGRES_CONTAINER="$(docker inspect --format '{{.Id}}' "$postgres_short_ids")"
[[ "$POSTGRES_CONTAINER" =~ ^[0-9a-f]{64}$ ]] || fail "current postgres identity is invalid"

current_revision="$(docker exec "$POSTGRES_CONTAINER" sh -c \
  'psql -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select version_num from alembic_version;"')"
[[ "$current_revision" =~ ^[A-Za-z0-9_]{8,64}$ ]] || \
  fail "live database migration identity is missing, multiple, or invalid"
target_heads="$(docker run --rm --network none --pull never --entrypoint python \
  "$TAKSKLAD_BACKEND_IMAGE" -m alembic -c /app/alembic.ini heads)" || \
  fail "candidate image migration head could not be resolved"
target_revision="$(printf '%s\n' "$target_heads" | \
  python3 "$TOOLS_DIR/validate_principal_schema_identity.py" --current-revision "$current_revision")" || \
  fail "candidate image and live database migration identities differ"
validate_operation_backup || \
  fail "operation-bound fresh backup is unverified"
if [[ "$COMMAND" == "revoke" && "$HANDOFF_PARENT_SAFE" != 1 ]]; then
  REVOKE_TEMP_DIR="$(mktemp -d /tmp/taksklad-revoke-handoff.XXXXXX)"
  chmod 700 "$REVOKE_TEMP_DIR"
  TAKSKLAD_PRINCIPAL_HANDOFF_HOST_PATH="$REVOKE_TEMP_DIR"
  export TAKSKLAD_PRINCIPAL_HANDOFF_HOST_PATH
  revoke_identity="$(docker compose --profile principal-admin --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --format json | \
    python3 "$TOOLS_DIR/validate_principal_provisioner_compose.py" \
      "$TAKSKLAD_BACKEND_IMAGE" "$TAKSKLAD_PROVISIONER_UID" "$TAKSKLAD_PROVISIONER_GID" \
      "$TAKSKLAD_PRINCIPAL_HANDOFF_HOST_PATH")"
  [[ "$revoke_identity" == "$rendered_identity" ]] || fail "revoke isolation changed compose identity"
  REVOKE_CLEANUP_UNVERIFIED=1
fi

NETWORK_CREATED=0
POSTGRES_CONNECTED=0
cleanup_network() {
  cleanup_failed=0
  if [[ "$POSTGRES_CONNECTED" == "1" ]]; then
    docker network disconnect "$ADMIN_NETWORK" "$POSTGRES_CONTAINER" >/dev/null 2>&1 || cleanup_failed=1
  fi
  if [[ "$NETWORK_CREATED" == "1" ]]; then
    docker network rm "$ADMIN_NETWORK" >/dev/null 2>&1 || cleanup_failed=1
  fi
  if [[ "$cleanup_failed" == "1" ]]; then
    echo "principal provisioning fatal: ephemeral network cleanup unverified" >&2
    return 1
  fi
  return 0
}
cleanup_on_exit() {
  original_status="$?"
  trap - EXIT
  cleanup_network || exit 1
  cleanup_revoke_temp || exit 1
  exit "$original_status"
}
trap cleanup_on_exit EXIT

validate_operation_backup || fail "operation-bound backup changed before mutation"

if docker network inspect "$ADMIN_NETWORK" >/dev/null 2>&1; then
  fail "operation network already exists and requires explicit reconciliation"
fi
docker network create --internal \
  --label "com.taksklad.principal.owner=$COMPOSE_PROJECT" \
  --label "com.taksklad.principal.operation=$OPERATION_ID" \
  "$ADMIN_NETWORK" >/dev/null
NETWORK_CREATED=1
attachment="$(docker network inspect "$ADMIN_NETWORK" | \
  python3 "$TOOLS_DIR/validate_principal_admin_network.py" \
    "$ADMIN_NETWORK" "$COMPOSE_PROJECT" "$POSTGRES_CONTAINER" "$OPERATION_ID")"
[[ "$attachment" == "attached=0" ]] || fail "operation network was not empty"
docker network connect --alias postgres "$ADMIN_NETWORK" "$POSTGRES_CONTAINER"
POSTGRES_CONNECTED=1
docker network inspect "$ADMIN_NETWORK" | \
  python3 "$TOOLS_DIR/validate_principal_admin_network.py" \
    "$ADMIN_NETWORK" "$COMPOSE_PROJECT" "$POSTGRES_CONTAINER" "$OPERATION_ID" | \
  grep -Fxq 'attached=1'
docker inspect "$POSTGRES_CONTAINER" | python3 -c '
import json,sys
network=sys.argv[1]
payload=json.load(sys.stdin)
assert isinstance(payload,list) and len(payload)==1
aliases=((payload[0].get("NetworkSettings") or {}).get("Networks") or {}).get(network,{}).get("Aliases") or []
assert "postgres" in aliases
' "$ADMIN_NETWORK"

docker compose --profile principal-admin --env-file "$ENV_FILE" -f "$COMPOSE_FILE" run \
  --rm --no-deps --pull never \
  --user "$TAKSKLAD_PROVISIONER_UID:$TAKSKLAD_PROVISIONER_GID" \
  --env "TAKSKLAD_PRINCIPAL_COMMAND_APPROVAL=${TAKSKLAD_PRINCIPAL_COMMAND_APPROVAL:-}" \
  principal-provisioner \
  "$COMMAND" $apply_arg --kind "$KIND" --identifier "$IDENTIFIER" \
  --operation-id "$OPERATION_ID" --handoff-file "$CONTAINER_HANDOFF_FILE"
docker network inspect "$ADMIN_NETWORK" | \
  python3 "$TOOLS_DIR/validate_principal_admin_network.py" \
    "$ADMIN_NETWORK" "$COMPOSE_PROJECT" "$POSTGRES_CONTAINER" "$OPERATION_ID" | \
  grep -Fxq 'attached=1'
trap - EXIT
cleanup_network || fail "ephemeral network cleanup unverified"
cleanup_revoke_temp || fail "temporary revoke handoff cleanup unverified"
if [[ "$REVOKE_CLEANUP_UNVERIFIED" == 1 ]]; then
  echo "DB_REVOKED cleanup=unverified reason=handoff_parent_unsafe" >&2
  exit 4
fi
echo "PRINCIPAL_ONE_SHOT_OK command=$COMMAND kind=$KIND secret_output=0 container_removed=1"
