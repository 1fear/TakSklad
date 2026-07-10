#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${1:-}" == "--local" && $# -eq 1 ]]; then
  MODE=(--local)
elif [[ $# -eq 0 ]]; then
  MODE=()
else
  echo "usage: $0 [--local]" >&2
  exit 2
fi

export PYTHONDONTWRITEBYTECODE=1
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  [[ -x .venv/bin/python ]] && PYTHON_BIN=.venv/bin/python || PYTHON_BIN=python3
fi
exec "$PYTHON_BIN" tools/release_artifacts.py verify --manifest test-artifacts/release.json "${MODE[@]}"
