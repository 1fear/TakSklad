#!/usr/bin/env python3
"""Scan only policy-allowed repository files for the synthetic Phase 11 sentinel."""

from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

from release_tree_policy import forbidden_path_reason


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SENTINEL = b"TAKSKLAD" + b"_SYNTHETIC_" + b"SECRET_SENTINEL_V1"


def allowed_repo_paths():
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("git ls-files failed")
    for raw_path in completed.stdout.split(b"\0"):
        if not raw_path:
            continue
        path = raw_path.decode("utf-8", "surrogateescape")
        if forbidden_path_reason(path):
            continue
        candidate = PROJECT_ROOT / path
        if candidate.is_file() and not candidate.is_symlink():
            yield path, candidate


def sentinel_count_in_file(relative_path, path):
    count = path.read_bytes().count(SENTINEL)
    if path.suffix.lower() != ".zip":
        return count
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                member_path = f"{relative_path}/{info.filename}"
                if info.is_dir() or forbidden_path_reason(member_path):
                    continue
                count += archive.read(info).count(SENTINEL)
    except zipfile.BadZipFile:
        pass
    return count


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--allowed-tree", action="store_true", required=True)
    args = parser.parse_args(argv)
    if not args.allowed_tree:
        return 2
    count = 0
    scanned = 0
    for relative_path, path in allowed_repo_paths():
        scanned += 1
        count += sentinel_count_in_file(relative_path, path)
    sys.stdout.write(f"synthetic_secret_sentinel_count={count} scanned_allowed_files={scanned}\n")
    return 1 if count else 0


if __name__ == "__main__":
    sys.exit(main())
