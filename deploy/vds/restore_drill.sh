#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PYTHON_BIN="${TAKSKLAD_PYTHON_BIN:-$APP_DIR/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

if [[ "$*" == "--isolated --synthetic-db --assert-invariants" ]]; then
  exec "$PYTHON_BIN" "$APP_DIR/tools/dr_recovery.py" restore-drill
fi

if [[ $# -eq 1 && -f "$1" ]]; then
  exec "$PYTHON_BIN" "$APP_DIR/tools/dr_recovery.py" restore-drill --manifest "$1"
fi

echo "Usage: $0 --isolated --synthetic-db --assert-invariants" >&2
echo "   or: $0 /path/to/verified-backup.manifest.json" >&2
exit 2
