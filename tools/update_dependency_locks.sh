#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

readonly REQUIRED_UV_VERSION="0.11.23"
readonly UV_BIN="${UV_BIN:-uv}"

if ! command -v "$UV_BIN" >/dev/null 2>&1; then
  echo "DEPENDENCY_LOCK_ERROR code=UV_NOT_AVAILABLE expected=$REQUIRED_UV_VERSION" >&2
  exit 2
fi

actual_version="$($UV_BIN --version | awk '{print $2}')"
if [[ "$actual_version" != "$REQUIRED_UV_VERSION" ]]; then
  echo "DEPENDENCY_LOCK_ERROR code=UV_VERSION_MISMATCH expected=$REQUIRED_UV_VERSION actual=$actual_version" >&2
  exit 2
fi

mkdir -p requirements

compile_lock() {
  local input_path="$1"
  local output_path="$2"
  "$UV_BIN" pip compile "$input_path" \
    --quiet \
    --constraints requirements/constraints.txt \
    --universal \
    --python-version 3.12 \
    --generate-hashes \
    --no-emit-package pip \
    --output-file "$output_path" \
    --custom-compile-command './tools/update_dependency_locks.sh'
}

compile_lock requirements.txt requirements/desktop.lock
compile_lock backend/requirements.txt backend/requirements.lock

"$UV_BIN" pip compile security/requirements.in \
  --quiet \
  --universal \
  --python-version 3.12 \
  --generate-hashes \
  --output-file security/requirements.lock \
  --custom-compile-command './tools/update_dependency_locks.sh'

for lock_path in requirements/desktop.lock backend/requirements.lock security/requirements.lock; do
  lock_hash="$(shasum -a 256 "$lock_path" | awk '{print $1}')"
  lock_count="$(grep -Ec '^[A-Za-z0-9_.-]+(\[[^]]+\])?==' "$lock_path")"
  echo "DEPENDENCY_LOCK_UPDATED path=$lock_path packages=$lock_count sha256=$lock_hash"
done
