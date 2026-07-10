#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PYTHON_BIN="${TAKSKLAD_PYTHON_BIN:-$APP_DIR/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

maintenance_marker="${TAKSKLAD_MAINTENANCE_MARKER:-/run/taksklad-observability/maintenance.json}"
if [[ "$*" == "--isolated --synthetic-db --assert-invariants" ]]; then
  if [[ -z "${TAKSKLAD_MAINTENANCE_MARKER:-}" ]]; then
    maintenance_marker="$APP_DIR/test-artifacts/disaster-recovery/maintenance.json"
  fi
  "$PYTHON_BIN" "$APP_DIR/tools/dr_recovery.py" restore-drill
  exec "$PYTHON_BIN" "$APP_DIR/tools/write_maintenance_marker.py" restore_drill --path "$maintenance_marker"
fi

if [[ $# -eq 1 && -f "$1" ]]; then
  "$PYTHON_BIN" "$APP_DIR/tools/dr_recovery.py" restore-drill --manifest "$1"
  exec "$PYTHON_BIN" "$APP_DIR/tools/write_maintenance_marker.py" restore_drill --path "$maintenance_marker"
fi

echo "Usage: $0 --isolated --synthetic-db --assert-invariants" >&2
echo "   or: $0 /path/to/verified-backup.manifest.json" >&2
exit 2
