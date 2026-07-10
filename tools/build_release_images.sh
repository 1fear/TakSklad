#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${1:-}" != "--local" || "${2:-}" != "--sha" || -z "${3:-}" || $# -ne 3 ]]; then
  echo "usage: $0 --local --sha FULL_COMMIT_SHA" >&2
  exit 2
fi

export PYTHONDONTWRITEBYTECODE=1
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  [[ -x .venv/bin/python ]] && PYTHON_BIN=.venv/bin/python || PYTHON_BIN=python3
fi
exec "$PYTHON_BIN" tools/release_artifacts.py build-local --sha "$3"
