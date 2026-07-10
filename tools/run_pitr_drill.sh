#!/usr/bin/env bash
set -euo pipefail

IMAGE="postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777"
RPO_LIMIT=""
RTO_LIMIT=""
SYNTHETIC=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --synthetic-db) SYNTHETIC=true; shift ;;
    --assert-rpo-minutes) RPO_LIMIT="${2:-}"; shift 2 ;;
    --assert-rto-minutes) RTO_LIMIT="${2:-}"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [[ "$SYNTHETIC" != true || ! "$RPO_LIMIT" =~ ^[0-9]+$ || ! "$RTO_LIMIT" =~ ^[0-9]+$ ]]; then
  echo "Usage: $0 --synthetic-db --assert-rpo-minutes 15 --assert-rto-minutes 30" >&2
  exit 2
fi
command -v docker >/dev/null || { echo "Docker is required for the disposable PostgreSQL PITR drill" >&2; exit 1; }
docker image inspect "$IMAGE" >/dev/null

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
EVIDENCE_DIR="${TAKSKLAD_DR_EVIDENCE_DIR:-$PROJECT_ROOT/test-artifacts/disaster-recovery}"
mkdir -p "$EVIDENCE_DIR"

run_id="pitr-$(date -u +%Y%m%dT%H%M%SZ)-$$-${RANDOM}"
primary="taksklad-${run_id}-primary"
recovery="taksklad-${run_id}-recovery"
data_volume="taksklad-${run_id}-data"
base_volume="taksklad-${run_id}-base"
wal_volume="taksklad-${run_id}-wal"
recovery_volume="taksklad-${run_id}-recovery-data"
resource_names=("$primary" "$recovery" "$data_volume" "$base_volume" "$wal_volume" "$recovery_volume")
cleanup_complete=false

cleanup() {
  if [[ "${TAKSKLAD_DR_DEBUG_KEEP:-false}" == "true" ]]; then
    echo "Debug mode retained disposable resources for inspection: $run_id" >&2
    return
  fi
  docker rm -f "$primary" "$recovery" >/dev/null 2>&1 || true
  docker volume rm -f "$data_volume" "$base_volume" "$wal_volume" "$recovery_volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

for volume in "$data_volume" "$base_volume" "$wal_volume" "$recovery_volume"; do
  docker volume create "$volume" >/dev/null
done
docker run --rm -u root -v "$base_volume:/base" -v "$wal_volume:/wal" "$IMAGE" \
  sh -eu -c 'chown -R postgres:postgres /base /wal; chmod 700 /base /wal'

docker run -d --name "$primary" \
  -e POSTGRES_PASSWORD=synthetic-pitr-only \
  -e POSTGRES_DB=synthetic_pitr \
  -v "$data_volume:/var/lib/postgresql/data" \
  -v "$base_volume:/base" \
  -v "$wal_volume:/wal" \
  "$IMAGE" \
  -c wal_level=replica \
  -c archive_mode=on \
  -c "archive_command=test ! -f /wal/%f && cp %p /wal/%f" \
  -c archive_timeout=1s >/dev/null

for _ in $(seq 1 60); do
  if docker exec "$primary" psql -U postgres -d synthetic_pitr -At -c 'select 1;' >/dev/null 2>&1; then break; fi
  sleep 0.5
done
docker exec "$primary" psql -U postgres -d synthetic_pitr -At -c 'select 1;' >/dev/null
docker exec "$primary" psql -v ON_ERROR_STOP=1 -U postgres -d synthetic_pitr \
  -c "CREATE TABLE recovery_probe(event_id text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT clock_timestamp());" >/dev/null
docker exec "$primary" psql -v ON_ERROR_STOP=1 -U postgres -d synthetic_pitr -c "CHECKPOINT;" >/dev/null
docker exec "$primary" pg_basebackup -U postgres -D /base -Fp -Xs -c fast >/dev/null

before_time="$(docker exec "$primary" psql -At -v ON_ERROR_STOP=1 -U postgres -d synthetic_pitr \
  -c "WITH inserted AS (INSERT INTO recovery_probe(event_id) VALUES ('event-before-target') RETURNING created_at) SELECT created_at FROM inserted;")"
sleep 1
target_time="$(docker exec "$primary" psql -At -v ON_ERROR_STOP=1 -U postgres -d synthetic_pitr \
  -c "SELECT clock_timestamp();")"
sleep 1
docker exec "$primary" psql -v ON_ERROR_STOP=1 -U postgres -d synthetic_pitr \
  -c "INSERT INTO recovery_probe(event_id) VALUES ('event-after-target'); SELECT pg_switch_wal();" >/dev/null
sleep 2
docker stop -t 30 "$primary" >/dev/null

docker run --rm -u root -v "$base_volume:/from:ro" -v "$recovery_volume:/to" "$IMAGE" \
  sh -eu -c 'cp -a /from/. /to/; chown -R postgres:postgres /to; chmod 700 /to'
docker run --rm -u root -v "$recovery_volume:/data" "$IMAGE" sh -eu -c \
  "touch /data/recovery.signal; printf '%s\n' \"restore_command = 'cp /wal/%f %p'\" \"recovery_target_time = '$target_time'\" \"recovery_target_inclusive = 'true'\" \"recovery_target_action = 'promote'\" >> /data/postgresql.auto.conf; chown postgres:postgres /data/recovery.signal /data/postgresql.auto.conf"

recovery_started_epoch="$(date +%s)"
docker run -d --name "$recovery" \
  -v "$recovery_volume:/var/lib/postgresql/data" \
  -v "$wal_volume:/wal:ro" \
  "$IMAGE" >/dev/null
recovery_probe=""
for _ in $(seq 1 120); do
  recovery_probe="$(docker exec "$recovery" psql -U postgres -d synthetic_pitr -At \
    -c "select (to_regclass('public.recovery_probe') is not null)::int || ':' || pg_is_in_recovery()::text;" \
    2>/dev/null || true)"
  if [[ "$recovery_probe" == "1:false" ]]; then break; fi
  sleep 0.5
done
if [[ "$recovery_probe" != "1:false" ]]; then
  docker logs "$recovery" --tail 80 >&2 || true
  echo "Disposable recovery PostgreSQL did not reach and promote the target" >&2
  exit 1
fi

before_count="$(docker exec "$recovery" psql -At -v ON_ERROR_STOP=1 -U postgres -d synthetic_pitr -c "SELECT count(*) FROM recovery_probe WHERE event_id='event-before-target';")"
after_count="$(docker exec "$recovery" psql -At -v ON_ERROR_STOP=1 -U postgres -d synthetic_pitr -c "SELECT count(*) FROM recovery_probe WHERE event_id='event-after-target';")"
recovery_state="$(docker exec "$recovery" psql -At -v ON_ERROR_STOP=1 -U postgres -d synthetic_pitr -c "SELECT pg_is_in_recovery();")"
[[ "$before_count" == "1" ]] || { echo "PITR invariant failed: event before target missing" >&2; exit 1; }
[[ "$after_count" == "0" ]] || { echo "PITR invariant failed: event after target was recovered" >&2; exit 1; }
[[ "$recovery_state" == "f" ]] || { echo "PITR did not promote at selected recovery target" >&2; exit 1; }

rto_seconds="$(( $(date +%s) - recovery_started_epoch ))"
rpo_seconds="$(python3 - "$before_time" "$target_time" <<'PY'
from datetime import datetime
import re
import sys
def parse(value):
    value = value.replace("Z", "+00:00")
    if value.endswith("+00"):
        value += ":00"
    match = re.fullmatch(r"(.*[.])(\d+)([+-]\d{2}:\d{2})", value)
    if match:
        value = match.group(1) + match.group(2).ljust(6, "0")[:6] + match.group(3)
    return datetime.fromisoformat(value)
before = parse(sys.argv[1])
target = parse(sys.argv[2])
print(max(0.0, (target - before).total_seconds()))
PY
)"
python3 - "$rpo_seconds" "$RPO_LIMIT" "$rto_seconds" "$RTO_LIMIT" <<'PY'
import sys
rpo_seconds, rpo_limit, rto_seconds, rto_limit = map(float, sys.argv[1:])
if rpo_seconds > rpo_limit * 60:
    raise SystemExit(f"RPO assertion failed: {rpo_seconds / 60:.3f} > {rpo_limit:.3f} minutes")
if rto_seconds > rto_limit * 60:
    raise SystemExit(f"RTO assertion failed: {rto_seconds:.3f}s > {rto_limit:.3f} minutes")
PY
rpo_minutes="$(python3 - "$rpo_seconds" <<'PY'
import sys
print(round(float(sys.argv[1]) / 60, 3))
PY
)"
wal_files="$(docker run --rm -v "$wal_volume:/wal:ro" "$IMAGE" sh -c "find /wal -type f | wc -l" | tr -d ' ')"

evidence="$EVIDENCE_DIR/pitr-drill.json"
partial="$evidence.partial"
python3 - "$partial" "$run_id" "$target_time" "$before_time" "$rpo_minutes" "$RPO_LIMIT" "$rto_seconds" "$RTO_LIMIT" "$wal_files" <<'PY'
import json, os, sys
path, run_id, target, before, rpo, rpo_limit, rto, rto_limit, wal_files = sys.argv[1:]
payload = {
    "schema": "taksklad-pitr-drill-evidence-v2",
    "drill_id": run_id,
    "drill_mode": "disposable-postgresql-wal-recovery",
    "postgres_image_pinned": True,
    "selected_recovery_target_time": target,
    "last_required_event_time": before,
    "event_before_target_count": 1,
    "event_after_target_count": 0,
    "wal_archive_file_count": int(wal_files),
    "rpo_minutes": float(rpo),
    "rpo_target_minutes": int(rpo_limit),
    "rpo_met": True,
    "rto_seconds": int(rto),
    "rto_target_minutes": int(rto_limit),
    "rto_met": True,
    "actual_postgresql_pitr": True,
    "isolated": True,
    "production_touched": False,
    "customer_content_in_evidence": False,
}
with open(path, "x", encoding="utf-8") as stream:
    json.dump(payload, stream, sort_keys=True, indent=2)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(path, path.removesuffix(".partial"))
PY

cleanup
trap - EXIT INT TERM
for resource in "${resource_names[@]}"; do
  if docker container inspect "$resource" >/dev/null 2>&1 || docker volume inspect "$resource" >/dev/null 2>&1; then
    echo "PITR cleanup failed: disposable resource remains: $resource" >&2
    exit 1
  fi
done
cleanup_complete=true
echo "PITR_DRILL_OK drill_id=$run_id selected_timestamp=$target_time last_required_event=$before_time rpo_minutes=$rpo_minutes rto_seconds=$rto_seconds wal_files=$wal_files before_count=1 after_count=0 actual_postgresql=true cleanup_zero=true production_touched=false evidence=$evidence"
