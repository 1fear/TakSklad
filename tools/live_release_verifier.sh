#!/usr/bin/env bash
set -Eeuo pipefail

export PYTHONDONTWRITEBYTECODE=1
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  [[ -x .venv/bin/python ]] && PYTHON_BIN=.venv/bin/python || PYTHON_BIN=python3
fi

exec "$PYTHON_BIN" tools/production_release_checks.py live "$@"
