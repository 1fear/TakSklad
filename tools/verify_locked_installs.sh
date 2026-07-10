#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

cold=0
require_hashes=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cold) cold=1 ;;
    --require-hashes) require_hashes=1 ;;
    *) echo "LOCK_VERIFY_ERROR code=UNKNOWN_ARGUMENT argument=$1" >&2; exit 2 ;;
  esac
  shift
done

if [[ "$cold" -ne 1 || "$require_hashes" -ne 1 ]]; then
  echo "LOCK_VERIFY_ERROR code=STRICT_MODE_REQUIRED expected='--cold --require-hashes'" >&2
  exit 2
fi

if [[ -n "${PYTHON_BIN:-}" ]]; then
  python_bin="$PYTHON_BIN"
elif [[ -x ".venv/bin/python" ]]; then
  python_bin=".venv/bin/python"
else
  python_bin="python3"
fi
if ! command -v "$python_bin" >/dev/null 2>&1; then
  echo "LOCK_VERIFY_ERROR code=PYTHON_NOT_AVAILABLE" >&2
  exit 2
fi

python_version="$($python_bin -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$python_version" != "3.12" ]]; then
  echo "LOCK_VERIFY_ERROR code=PYTHON_VERSION_MISMATCH expected=3.12 actual=$python_version" >&2
  exit 2
fi

tmp_root="$(mktemp -d "${TMPDIR:-/tmp}/taksklad-lock-verify.XXXXXX")"
trap 'rm -rf "$tmp_root"' EXIT

manifest_for() {
  local venv_dir="$1"
  "$venv_dir/bin/python" - <<'PY'
from importlib import metadata

def canonical(value: str) -> str:
    return value.lower().replace("_", "-").replace(".", "-")

rows = {
    f"{canonical(distribution.metadata['Name'])}=={distribution.version}"
    for distribution in metadata.distributions()
    if distribution.metadata.get("Name") and canonical(distribution.metadata["Name"]) != "pip"
}
print("\n".join(sorted(rows)))
PY
}

verify_lock() {
  local name="$1"
  local lock_path="$2"
  local first_venv="$tmp_root/${name}-a"
  local second_venv="$tmp_root/${name}-b"
  local first_manifest="$tmp_root/${name}-a.txt"
  local second_manifest="$tmp_root/${name}-b.txt"

  if [[ ! -f "$lock_path" ]]; then
    echo "LOCK_VERIFY_ERROR code=LOCK_MISSING path=$lock_path" >&2
    return 1
  fi
  if grep -Eq '^[A-Za-z0-9_.-]+(\[[^]]+\])?==[^[:space:]]+([[:space:]]*;[^\\]+)?[[:space:]]*\\$' "$lock_path" && ! grep -q -- '--hash=sha256:' "$lock_path"; then
    echo "LOCK_VERIFY_ERROR code=HASHES_MISSING path=$lock_path" >&2
    return 1
  fi

  for venv_dir in "$first_venv" "$second_venv"; do
    "$python_bin" -m venv "$venv_dir"
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
      PIP_NO_INPUT=1 \
      PYTHONDONTWRITEBYTECODE=1 \
      "$venv_dir/bin/python" -m pip install \
        --require-hashes \
        --only-binary=:all: \
        --quiet \
        -r "$lock_path"
    "$venv_dir/bin/python" -m pip check >/dev/null
  done

  manifest_for "$first_venv" >"$first_manifest"
  manifest_for "$second_venv" >"$second_manifest"
  if ! cmp -s "$first_manifest" "$second_manifest"; then
    echo "LOCK_VERIFY_ERROR code=MANIFEST_MISMATCH lock=$lock_path" >&2
    diff -u "$first_manifest" "$second_manifest" >&2 || true
    return 1
  fi

  local lock_hash manifest_hash package_count
  lock_hash="$(shasum -a 256 "$lock_path" | awk '{print $1}')"
  manifest_hash="$(shasum -a 256 "$first_manifest" | awk '{print $1}')"
  package_count="$(awk 'NF {count += 1} END {print count + 0}' "$first_manifest")"
  echo "LOCK_VERIFY_OK target=$name lock=$lock_path packages=$package_count lock_sha256=$lock_hash manifest_sha256=$manifest_hash replicas=2"
}

verify_lock desktop requirements/desktop.lock
verify_lock backend backend/requirements.lock
echo "LOCK_VERIFY_COMPLETE targets=2 cold=1 require_hashes=1"
