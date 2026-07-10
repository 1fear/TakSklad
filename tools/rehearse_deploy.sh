#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${1:-}" != "--environment" || "${2:-}" != "isolated" ]]; then
  echo "usage: $0 --environment isolated --assert-readiness --assert-migration-budget [--evidence PATH]" >&2
  exit 2
fi
shift 2
assert_readiness=0
assert_migration=0
extra=()
while (( $# )); do
  case "$1" in
    --assert-readiness) assert_readiness=1; shift ;;
    --assert-migration-budget) assert_migration=1; shift ;;
    --evidence) [[ $# -ge 2 ]] || exit 2; extra+=("--evidence" "$2"); shift 2 ;;
    *) echo "unsupported argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$assert_readiness" == 1 && "$assert_migration" == 1 ]] || {
  echo "both --assert-readiness and --assert-migration-budget are required" >&2
  exit 2
}
export PYTHONDONTWRITEBYTECODE=1
PYTHON_BIN=.venv/bin/python
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN=python3
exec "$PYTHON_BIN" tools/release_rehearsal_runtime.py deploy "${extra[@]}"
