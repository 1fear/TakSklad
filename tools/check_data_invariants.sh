#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${TAKSKLAD_TEST_PYTHON:-$ROOT_DIR/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

cd "$ROOT_DIR"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT_DIR" "$PYTHON_BIN" tools/check_data_invariants.py "$@"
