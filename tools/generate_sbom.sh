#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export PYTHONDONTWRITEBYTECODE=1
if [[ -n "${PYTHON_BIN:-}" ]]; then
  python_bin="$PYTHON_BIN"
elif [[ -x ".venv/bin/python" ]]; then
  python_bin=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  python_bin="python3"
else
  python_bin="python"
fi
exec "$python_bin" tools/generate_sbom.py "$@"
