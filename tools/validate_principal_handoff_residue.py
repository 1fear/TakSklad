#!/usr/bin/env python3
"""Block on crash-residue token temp files without reading or printing their content."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys


PREFIX = ".token."


def validate(parent: Path) -> None:
    parent_stat = parent.lstat()
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or stat.S_ISLNK(parent_stat.st_mode)
        or parent_stat.st_uid != os.geteuid()
        or stat.S_IMODE(parent_stat.st_mode) & 0o022
    ):
        raise ValueError("parent_unsafe")
    residue = [item for item in parent.iterdir() if item.name.startswith(PREFIX)]
    for item in residue:
        value = item.lstat()
        if (
            not stat.S_ISREG(value.st_mode)
            or stat.S_ISLNK(value.st_mode)
            or value.st_uid != os.geteuid()
            or stat.S_IMODE(value.st_mode) != 0o600
        ):
            raise ValueError("residue_unsafe")
    if residue:
        raise RuntimeError(str(len(residue)))


def main(argv=None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) != 1:
        print("PRINCIPAL_RESIDUE_BLOCKED reason=usage", file=sys.stderr)
        return 2
    try:
        validate(Path(values[0]))
    except RuntimeError as exc:
        print(f"PRINCIPAL_RESIDUE_BLOCKED reason=crash_residue count={exc}", file=sys.stderr)
        return 3
    except (OSError, ValueError):
        print("PRINCIPAL_RESIDUE_BLOCKED reason=unsafe_parent_or_residue", file=sys.stderr)
        return 1
    print("PRINCIPAL_RESIDUE_OK count=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
