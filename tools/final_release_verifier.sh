#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ "${1:-}" != "--environment" || "${2:-}" != "isolated" || "${3:-}" != "--repeat" || "${4:-}" != "3" || "${5:-}" != "--same-artifact" || $# -ne 5 ]]; then
  echo "usage: $0 --environment isolated --repeat 3 --same-artifact" >&2
  exit 2
fi

export PYTHONDONTWRITEBYTECODE=1
export TAKSKLAD_NO_PRODUCTION=1
export TAKSKLAD_EXTERNAL_SENDS_DISABLED=1
exec "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/tools/final_release_verifier.py" "$@"
