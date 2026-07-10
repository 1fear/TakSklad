#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${1:-}" != "--dummy-config" || "${2:-}" != "--permission-tests" || $# -ne 2 ]]; then
  echo "usage: $0 --dummy-config --permission-tests" >&2
  exit 2
fi

export COMPOSE_DISABLE_ENV_FILE=1
export PYTHONDONTWRITEBYTECODE=1
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x .venv/bin/python ]]; then
    PYTHON_BIN=.venv/bin/python
  else
    PYTHON_BIN=python3
  fi
fi
exec "$PYTHON_BIN" tools/container_runtime_harness.py smoke
