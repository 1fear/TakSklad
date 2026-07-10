#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-all}"
IMAGE="${TAKSKLAD_POSTGRES_TEST_IMAGE:-postgres:16-alpine}"
CONTAINER="taksklad-phase2-pg-$$-${RANDOM}"
PASSWORD="synthetic-phase2-only"
PYTHON_BIN="${TAKSKLAD_TEST_PYTHON:-$ROOT_DIR/.venv/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

cleanup() {
  if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  fi
  local remaining
  remaining="$(docker ps -aq --filter "name=^/${CONTAINER}$" | wc -l | tr -d ' ')"
  echo "postgres-test: cleanup containers=$remaining volumes=0"
  [[ "$remaining" == "0" ]]
}
trap cleanup EXIT INT TERM

case "$MODE" in
  seed-reference)
    TEST_MODULE=""
    ;;
  migrations)
    TEST_MODULE="tests.test_postgres_migrations"
    ;;
  smoke)
    TEST_MODULE="tests.test_postgres_concurrency"
    ;;
  readiness)
    TEST_MODULE="tests.test_postgres_readiness"
    ;;
  observability)
    TEST_MODULE="tests.test_postgres_observability"
    ;;
  queue-concurrency)
    TEST_MODULE="tests.test_postgres_queue_concurrency"
    ;;
  queue-failures)
    TEST_MODULE="tests.test_postgres_queue_failures"
    ;;
  import-identity)
    TEST_MODULE="tests.test_postgres_import_identity"
    ;;
  invariants)
    TEST_MODULE="tests.test_postgres_invariants"
    ;;
  outbox)
    TEST_MODULE="tests.test_postgres_outbox"
    ;;
  outbox-faults)
    TEST_MODULE="tests.test_postgres_outbox_faults"
    ;;
  smartup-saga)
    TEST_MODULE="tests.test_postgres_smartup_saga"
    ;;
  auth-identities)
    TEST_MODULE="tests.test_postgres_auth_identities"
    ;;
  rbac-audit)
    TEST_MODULE="tests.test_postgres_rbac_audit"
    ;;
  input-safety)
    TEST_MODULE="tests.test_postgres_input_safety"
    ;;
  query-parity)
    TEST_MODULE="tests.test_postgres_query_parity"
    ;;
  db-resilience)
    TEST_MODULE="tests.test_postgres_db_resilience"
    ;;
  cursor-capabilities)
    TEST_MODULE="tests.test_postgres_cursor_capabilities"
    ;;
  all)
    TEST_MODULE="tests.test_postgres_migrations tests.test_postgres_concurrency tests.test_postgres_readiness tests.test_postgres_observability tests.test_postgres_queue_concurrency tests.test_postgres_queue_failures tests.test_postgres_import_identity tests.test_postgres_invariants tests.test_postgres_outbox tests.test_postgres_outbox_faults tests.test_postgres_smartup_saga tests.test_postgres_auth_identities tests.test_postgres_rbac_audit tests.test_postgres_input_safety tests.test_postgres_query_parity tests.test_postgres_db_resilience tests.test_postgres_cursor_capabilities"
    ;;
  *)
    echo "Usage: $0 {seed-reference|migrations|smoke|readiness|observability|queue-concurrency|queue-failures|import-identity|invariants|outbox|outbox-faults|smartup-saga|auth-identities|rbac-audit|input-safety|query-parity|db-resilience|cursor-capabilities|all}" >&2
    exit 2
    ;;
esac

docker run --detach --rm \
  --name "$CONTAINER" \
  --tmpfs /var/lib/postgresql/data:rw,nosuid,nodev,size=512m \
  --env POSTGRES_PASSWORD="$PASSWORD" \
  --env POSTGRES_DB=postgres \
  --publish 127.0.0.1::5432 \
  "$IMAGE" >/dev/null

ready=0
for _attempt in $(seq 1 60); do
  if docker exec "$CONTAINER" pg_isready -U postgres -d postgres >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.5
done
if [[ "$ready" != "1" ]]; then
  echo "postgres-test: PostgreSQL did not become ready" >&2
  exit 1
fi

mapping="$(docker port "$CONTAINER" 5432/tcp | head -n1)"
port="${mapping##*:}"
export TAKSKLAD_TEST_DATABASE_URL="postgresql+psycopg://postgres:${PASSWORD}@127.0.0.1:${port}/postgres"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$ROOT_DIR"

echo "postgres-test: image=$IMAGE mode=$MODE"
docker exec "$CONTAINER" postgres --version

if [[ "${TAKSKLAD_POSTGRES_TEST_FORCE_FAILURE:-0}" == "1" ]]; then
  echo "postgres-test: forced synthetic failure" >&2
  exit 17
fi

cd "$ROOT_DIR"
if [[ "$MODE" == "seed-reference" ]]; then
  export DATABASE_URL="$TAKSKLAD_TEST_DATABASE_URL"
  export TAKSKLAD_ENV="test"
  export TAKSKLAD_API_TOKEN="synthetic-only-test-token"
  "$PYTHON_BIN" -m alembic -c backend/alembic.ini upgrade head
  "$PYTHON_BIN" tools/benchmark_backend.py seed --profile reference
else
  # shellcheck disable=SC2086
  "$PYTHON_BIN" -m unittest -v $TEST_MODULE
fi

cleanup
trap - EXIT INT TERM
