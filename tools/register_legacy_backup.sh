#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
INPUT=""
OUTPUT_ROOT="${TAKSKLAD_BACKUP_TEST_DIR:-$APP_DIR/test-artifacts/phase24/legacy-registered}"
SIMULATE_FAILURE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --input) INPUT="${2:-}"; shift ;;
    --output-root) OUTPUT_ROOT="${2:-}"; shift ;;
    --simulate-failure) SIMULATE_FAILURE=true ;;
    *) echo "Usage: $0 --input OLD.sql.gz [--output-root DIR] [--simulate-failure]" >&2; exit 2 ;;
  esac
  shift
done
[[ -n "$INPUT" && -f "$INPUT" ]] || { echo "Legacy .sql.gz input is required" >&2; exit 2; }
[[ "$INPUT" == *.sql.gz ]] || { echo "Legacy input must end in .sql.gz" >&2; exit 2; }
gzip -t "$INPUT"
gzip -dc "$INPUT" | awk '
  NR == 1 && $0 == "-- PostgreSQL database dump" {header = 1}
  $0 == "-- PostgreSQL database dump complete" {complete = 1}
  END {exit !(header && complete)}
'

digest="$(shasum -a 256 "$INPUT" | awk '{print $1}')"
backup_id="taksklad-postgres-legacy-${digest:0:16}"
completed_root="$OUTPUT_ROOT/completed"
bundle="$completed_root/$backup_id"
staging="$OUTPUT_ROOT/.staging-$backup_id-$$"
cleanup() { rm -rf "$staging"; }
trap cleanup EXIT
mkdir -p "$completed_root"
chmod 700 "$OUTPUT_ROOT" "$completed_root"
[[ ! -e "$bundle" && ! -e "$staging" ]] || { echo "Legacy backup already registered: $backup_id" >&2; exit 1; }
mkdir -m 700 "$staging"

archive_name="$backup_id.sql.gz"
list_name="$backup_id.list"
checksum_name="$backup_id.sha256"
manifest_name="$backup_id.manifest.json"
cp "$INPUT" "$staging/$archive_name"
gzip -dc "$INPUT" | awk '
  /^CREATE TABLE / {name=$3; gsub(/\(/, "", name); print "TABLE " name}
  /^COPY / {name=$2; print "TABLE DATA " name}
' | sort -u >"$staging/$list_name"
if [[ ! -s "$staging/$list_name" ]]; then
  printf '%s\n' 'LEGACY SQL validated; object inventory unavailable' >"$staging/$list_name"
fi
printf '%s  %s\n' "$digest" "$archive_name" >"$staging/$checksum_name"
chmod 600 "$staging"/*
if [[ "$SIMULATE_FAILURE" == true ]]; then
  echo "Synthetic failure requested before legacy bundle publication" >&2
  exit 86
fi
list_sha="$(shasum -a 256 "$staging/$list_name" | awk '{print $1}')"
bytes="$(wc -c <"$staging/$archive_name" | tr -d ' ')"
entries="$(awk 'NF {n += 1} END {print n + 0}' "$staging/$list_name")"
python3 - "$staging/$manifest_name" "$backup_id" "$archive_name" "$digest" "$bytes" \
  "$checksum_name" "$list_name" "$list_sha" "$entries" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

path, backup_id, archive, digest, size, checksum, inventory, inventory_sha, entries = sys.argv[1:]
payload = {
    "schema_version": 2,
    "backup_id": backup_id,
    "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "archive": {
        "filename": archive,
        "format": "postgresql-plain-sql-gzip-legacy-transition",
        "sha256": digest,
        "bytes": int(size),
        "validated": True,
        "validation": ["gzip-integrity", "pg-dump-header", "pg-dump-complete-marker"],
        "checksum_sidecar": checksum,
        "list": {"filename": inventory, "sha256": inventory_sha, "entries": int(entries), "validated": True},
    },
    "source": "legacy-postgresql-plain-sql",
    "postgres_image": "postgres:16-alpine@sha256:57c72fd2a128e416c7fcc499958864df5301e940bca0a56f58fddf30ffc07777",
    "actual_postgresql": False,
    "transition_registered": True,
    "isolated_restore_validation_required": True,
    "contains_customer_content": True,
    "sanitized_manifest": True,
    "atomic_bundle": True,
}
with open(path, "x", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
PY
chmod 600 "$staging/$manifest_name"
mv "$staging" "$bundle"
staging=""
trap - EXIT
printf 'LEGACY_BACKUP_REGISTERED backup_id=%s sha256=%s bundle=%s manifest=%s restore_validation_required=true\n' \
  "$backup_id" "$digest" "$bundle" "$bundle/$manifest_name"
