#!/usr/bin/env python3
"""Fail-closed release tree and phase-boundary ownership guard.

The default checks are path-only. Hashes are read only for paths already
recorded as allowed in a local ownership manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    from tools.release_tree_policy import forbidden_path_reason, is_runtime_surface
except ModuleNotFoundError:  # Direct `python tools/check_release_tree.py` execution.
    from release_tree_policy import forbidden_path_reason, is_runtime_surface


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = Path(".release-state/owned-tree-manifest.json")


@dataclass(frozen=True)
class Change:
    status: str
    path: str


def run_git(root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {message}")
    return completed.stdout


def status_changes(root: Path) -> list[Change]:
    payload = run_git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--no-renames",
    )
    changes: list[Change] = []
    for raw in payload.split(b"\0"):
        if not raw:
            continue
        text = raw.decode("utf-8", errors="surrogateescape")
        changes.append(Change(status=text[:2], path=text[3:]))
    return sorted(changes, key=lambda item: item.path)


def exclude_prefixes(changes: list[Change], prefixes: list[str]) -> list[Change]:
    """Exclude declared generated evidence from ownership hash comparisons only."""

    normalized = tuple(prefix.replace("\\", "/").lstrip("./").rstrip("/") + "/" for prefix in prefixes)
    return [
        change for change in changes
        if not any(change.path.replace("\\", "/").startswith(prefix) for prefix in normalized)
    ]


def staged_changes(root: Path) -> list[Change]:
    payload = run_git(root, "diff", "--cached", "--name-status", "-z", "--no-renames")
    fields = [field for field in payload.split(b"\0") if field]
    changes: list[Change] = []
    for index in range(0, len(fields), 2):
        status = fields[index].decode("ascii", errors="replace")
        path = fields[index + 1].decode("utf-8", errors="surrogateescape")
        changes.append(Change(status=status, path=path))
    return sorted(changes, key=lambda item: item.path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_head(root: Path) -> str:
    return run_git(root, "rev-parse", "HEAD").decode("ascii").strip()


def current_branch(root: Path) -> str:
    return run_git(root, "branch", "--show-current").decode("utf-8").strip()


def change_record(root: Path, change: Change, *, include_hash: bool) -> dict[str, str]:
    record = {"status": change.status, "path": change.path}
    absolute_path = root / change.path
    if include_hash and absolute_path.is_file() and forbidden_path_reason(change.path) is None:
        record["sha256"] = sha256_file(absolute_path)
    return record


def write_manifest(root: Path, manifest_path: Path, changes: list[Change]) -> None:
    forbidden = [change.path for change in changes if forbidden_path_reason(change.path)]
    if forbidden:
        raise RuntimeError("refusing to manifest forbidden paths: " + ", ".join(forbidden))

    payload = {
        "schema": 1,
        "head": current_head(root),
        "branch": current_branch(root),
        "changes": [change_record(root, change, include_hash=True) for change in changes],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=manifest_path.name, dir=manifest_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
            json.dump(payload, file_obj, ensure_ascii=False, indent=2, sort_keys=True)
            file_obj.write("\n")
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temporary_name, manifest_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def compare_manifest(root: Path, manifest_path: Path, changes: list[Change]) -> list[str]:
    if not manifest_path.is_file():
        return [f"owned manifest is missing: {manifest_path}"]
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    if payload.get("head") != current_head(root):
        problems.append("HEAD drifted from owned manifest")
    if payload.get("branch") != current_branch(root):
        problems.append("branch drifted from owned manifest")
    actual = [change_record(root, change, include_hash=True) for change in changes]
    if payload.get("changes") != actual:
        problems.append("allowed path/status/hash set drifted from owned manifest")
    return problems


def strict_problems(changes: list[Change], *, staged: bool) -> list[str]:
    problems: list[str] = []
    for change in changes:
        reason = forbidden_path_reason(change.path)
        if reason:
            problems.append(f"{change.path}: {reason}")
        if not staged and change.status == "??" and is_runtime_surface(change.path):
            problems.append(f"{change.path}: untracked runtime/source path")
    return problems


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--path-only", action="store_true")
    parser.add_argument("--write-owned-manifest", action="store_true")
    parser.add_argument("--compare-owned-manifest", action="store_true")
    parser.add_argument("--exclude-prefix", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    manifest_path = args.manifest
    if not manifest_path.is_absolute():
        manifest_path = root / manifest_path
    manifest_path = manifest_path.resolve()

    try:
        changes = staged_changes(root) if args.staged else status_changes(root)
        problems = strict_problems(changes, staged=args.staged) if args.strict else []
        owned_changes = exclude_prefixes(changes, args.exclude_prefix)
        if args.write_owned_manifest:
            if problems:
                raise RuntimeError("strict release-tree checks failed before manifest write")
            write_manifest(root, manifest_path, owned_changes)
        if args.compare_owned_manifest:
            problems.extend(compare_manifest(root, manifest_path, owned_changes))
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"release-tree: error: {exc}\n")
        return 2

    if problems:
        sys.stderr.write("release-tree: blocked\n")
        for problem in problems:
            sys.stderr.write(f"- {problem}\n")
        return 1

    mode = "staged" if args.staged else "working-tree"
    detail = "path-only" if args.path_only else "path/hash"
    sys.stdout.write(f"release-tree: ok mode={mode} detail={detail} changes={len(changes)}\n")
    try:
        manifest_label = manifest_path.relative_to(root)
    except ValueError:
        manifest_label = manifest_path
    if args.write_owned_manifest:
        sys.stdout.write(f"owned-manifest: written {manifest_label}\n")
    if args.compare_owned_manifest:
        sys.stdout.write(f"owned-manifest: match {manifest_label}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
