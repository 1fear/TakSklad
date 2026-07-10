#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${1:-}" != "--environment" || "${2:-}" != "isolated" ]]; then
  echo "usage: $0 --environment isolated --assert-max-seconds 300 [--evidence PATH]" >&2
  exit 2
fi
shift 2
max_seconds=""
extra=()
while (( $# )); do
  case "$1" in
    --assert-max-seconds) [[ $# -ge 2 ]] || exit 2; max_seconds="$2"; shift 2 ;;
    --evidence) [[ $# -ge 2 ]] || exit 2; extra+=("--evidence" "$2"); shift 2 ;;
    *) echo "unsupported argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$max_seconds" =~ ^[0-9]+([.][0-9]+)?$ ]] || {
  echo "--assert-max-seconds must be a positive number" >&2
  exit 2
}
export PYTHONDONTWRITEBYTECODE=1
PYTHON_BIN=.venv/bin/python
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN=python3
if (( ${#extra[@]} )); then
  exec "$PYTHON_BIN" tools/release_rehearsal_runtime.py rollback --max-seconds "$max_seconds" "${extra[@]}"
fi
exec "$PYTHON_BIN" tools/release_rehearsal_runtime.py rollback --max-seconds "$max_seconds"
