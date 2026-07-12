#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${TAKSKLAD_PYTHON_BIN:-}" ]]; then
  PYTHON_BIN="$TAKSKLAD_PYTHON_BIN"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
else
  PYTHON_BIN=python3
fi
exec "$PYTHON_BIN" "$ROOT_DIR/tools/alert_smoke.py" "$@"
