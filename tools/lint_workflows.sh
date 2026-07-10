#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON_BIN="python3"
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
fi

cd "$ROOT_DIR"

PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" - <<'PY'
from __future__ import annotations

from pathlib import Path
import re
import sys

try:
    import yaml
    from yaml.nodes import MappingNode, ScalarNode, SequenceNode
except ImportError as exc:
    raise SystemExit(f"WORKFLOW_LINT_ERROR: PyYAML is required: {exc}")


ROOT = Path.cwd()
WORKFLOW_DIR = ROOT / ".github" / "workflows"
errors: list[str] = []


def check_duplicate_keys(node: object, path: Path, location: str = "root") -> None:
    if isinstance(node, MappingNode):
        seen: dict[str, int] = {}
        for key_node, value_node in node.value:
            key = key_node.value if isinstance(key_node, ScalarNode) else repr(key_node.value)
            line = key_node.start_mark.line + 1
            if key in seen:
                errors.append(
                    f"{path.relative_to(ROOT)}:{line}: duplicate key {key!r} "
                    f"(first declared on line {seen[key]})"
                )
            else:
                seen[key] = line
            check_duplicate_keys(value_node, path, f"{location}.{key}")
    elif isinstance(node, SequenceNode):
        for index, item in enumerate(node.value):
            check_duplicate_keys(item, path, f"{location}[{index}]")


paths = sorted((*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")))
if not paths:
    errors.append("no GitHub Actions workflows found")

for path in paths:
    source = path.read_text(encoding="utf-8")
    try:
        node = yaml.compose(source, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        errors.append(f"{path.relative_to(ROOT)}: invalid YAML: {exc}")
        continue
    if not isinstance(node, MappingNode):
        errors.append(f"{path.relative_to(ROOT)}: workflow root must be a mapping")
        continue
    check_duplicate_keys(node, path)
    top_level = {
        key.value
        for key, _value in node.value
        if isinstance(key, ScalarNode)
    }
    for required in ("name", "on", "jobs"):
        if required not in top_level:
            errors.append(f"{path.relative_to(ROOT)}: missing top-level {required!r}")
    for line_no, line in enumerate(source.splitlines(), start=1):
        match = re.search(r"^\s*uses:\s*([^\s#]+)", line)
        if not match or match.group(1).startswith("./"):
            continue
        reference = match.group(1)
        if "@" not in reference or not re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference):
            errors.append(
                f"{path.relative_to(ROOT)}:{line_no}: action must use a full lowercase commit SHA"
            )

release = (WORKFLOW_DIR / "build-windows-release.yml").read_text(encoding="utf-8")
deploy = (WORKFLOW_DIR / "deploy-production.yml").read_text(encoding="utf-8")
ci = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")

release_contract = {
    "workflow identity": "name: Build Immutable Release",
    "Windows build job": "build-windows:",
    "container build job": "build-container-subjects:",
    "manifest job": "release-manifest:",
    "backend digest output": "backend_digest: ${{ steps.backend.outputs.digest }}",
    "frontend digest output": "frontend_digest: ${{ steps.frontend.outputs.digest }}",
    "backend digest consumer": "BACKEND_DIGEST: ${{ needs.build-container-subjects.outputs.backend_digest }}",
    "frontend digest consumer": "FRONTEND_DIGEST: ${{ needs.build-container-subjects.outputs.frontend_digest }}",
    "backend registry attestation": "subject-digest: ${{ steps.backend.outputs.digest }}",
    "frontend registry attestation": "subject-digest: ${{ steps.frontend.outputs.digest }}",
}
for label, needle in release_contract.items():
    if needle not in release:
        errors.append(f"build-windows-release.yml: missing {label}")
if release.count("uses: docker/build-push-action@") != 2:
    errors.append("build-windows-release.yml: release must build exactly backend and frontend once")
if release.count("id: backend\n") != 1 or release.count("id: frontend\n") != 1:
    errors.append("build-windows-release.yml: backend/frontend build identities must be unique")

deploy_contract = {
    "artifact run input": "artifact_run_id:",
    "exact source input": "source_sha:",
    "manifest hash input": "manifest_sha256:",
    "successful producer verification": 'metadata.get("conclusion") != "success"',
    "manifest attestation verification": 'gh attestation verify "$manifest_path"',
    "artifact-only invocation": "/tmp/deploy_from_git.sh --artifact-manifest /tmp/release.json",
    "protected production environment": "environment: production",
}
for label, needle in deploy_contract.items():
    if needle not in deploy:
        errors.append(f"deploy-production.yml: missing {label}")
for forbidden in ("inputs.ref", "inputs.branch", "inputs.tag", "TAKSKLAD_DEPLOY_REF"):
    if forbidden in deploy:
        errors.append(f"deploy-production.yml: arbitrary release selector is forbidden: {forbidden}")
if "name: Release gate" not in ci:
    errors.append("ci.yml: protected branch status context 'Release gate' is missing")

if errors:
    for error in errors:
        print(f"WORKFLOW_LINT_ERROR: {error}", file=sys.stderr)
    raise SystemExit(1)

print(
    "WORKFLOW_LINT_OK "
    f"files={len(paths)} duplicate_keys=0 immutable_actions=1 "
    "release_builds=2 digest_consumers=2 arbitrary_refs=0"
)
PY
