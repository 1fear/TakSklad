#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${1:-}" == "--local" && $# -eq 1 ]]; then
  MODE=local
  REQUESTED_SHA=""
elif [[ "${1:-}" == "--sha" && $# -eq 2 ]]; then
  MODE=production
  REQUESTED_SHA="$2"
elif [[ $# -eq 0 ]]; then
  MODE=production
  REQUESTED_SHA=""
else
  echo "usage: $0 [--local | --sha <40-lowercase-hex>]" >&2
  exit 2
fi

export PYTHONDONTWRITEBYTECODE=1
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  [[ -x .venv/bin/python ]] && PYTHON_BIN=.venv/bin/python || PYTHON_BIN=python3
fi

if [[ "$MODE" == "local" ]]; then
  exec "$PYTHON_BIN" tools/release_artifacts.py verify --manifest test-artifacts/release.json --local
fi

[[ -n "$REQUESTED_SHA" ]] || {
  echo "production verification requires --sha" >&2
  exit 2
}
MANIFEST="${TAKSKLAD_RELEASE_MANIFEST:-release.json}"
ARTIFACT_DIR="${TAKSKLAD_RELEASE_ARTIFACT_DIR:-.release-state/production-release}"
REPOSITORY="${TAKSKLAD_RELEASE_REPOSITORY:-1fear/TakSklad}"
SIGNER_WORKFLOW="github.com/$REPOSITORY/.github/workflows/build-windows-release.yml"

"$PYTHON_BIN" tools/release_artifacts.py verify \
  --manifest "$MANIFEST" --sha "$REQUESTED_SHA"

command -v gh >/dev/null || {
  echo "GitHub CLI is required for production attestation verification" >&2
  exit 1
}

eval "$("$PYTHON_BIN" - "$MANIFEST" "$ARTIFACT_DIR" <<'PY'
import hashlib
import json
from pathlib import Path
import shlex
import sys

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
root = Path(sys.argv[2])
for role in ("backend", "frontend"):
    print(f"{role.upper()}_REFERENCE={shlex.quote(manifest['images'][role]['reference'])}")
windows = manifest["windows"]
for key, hash_key, variable in (
    ("artifact", "artifact_sha256", "WINDOWS_EXE"),
    ("artifact_onedir", "artifact_sha256_onedir", "WINDOWS_ZIP"),
    ("manifest", "manifest_sha256", "WINDOWS_MANIFEST"),
):
    path = root / windows[key]
    if not path.is_file():
        raise SystemExit(f"production Windows subject is missing: {windows[key]}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != windows[hash_key]:
        raise SystemExit(f"production Windows subject hash mismatch: {windows[key]}")
    print(f"{variable}={shlex.quote(str(path))}")
PY
)"

gh attestation verify "$MANIFEST" \
  --repo "$REPOSITORY" --signer-workflow "$SIGNER_WORKFLOW" --source-digest "$REQUESTED_SHA"
gh attestation verify "oci://$BACKEND_REFERENCE" \
  --repo "$REPOSITORY" --signer-workflow "$SIGNER_WORKFLOW" --source-digest "$REQUESTED_SHA"
gh attestation verify "oci://$FRONTEND_REFERENCE" \
  --repo "$REPOSITORY" --signer-workflow "$SIGNER_WORKFLOW" --source-digest "$REQUESTED_SHA"
for subject in "$WINDOWS_EXE" "$WINDOWS_ZIP" "$WINDOWS_MANIFEST"; do
  gh attestation verify "$subject" \
    --repo "$REPOSITORY" --signer-workflow "$SIGNER_WORKFLOW" --source-digest "$REQUESTED_SHA"
done

printf 'RELEASE_ATTESTATIONS_GITHUB_OK source_sha=%s subjects=6 signer_workflow=%s\n' \
  "$REQUESTED_SHA" "$SIGNER_WORKFLOW"
