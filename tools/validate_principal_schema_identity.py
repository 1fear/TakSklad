#!/usr/bin/env python3
"""Compare one candidate Alembic head with the exact live database revision."""

from __future__ import annotations

import argparse
import re
import sys


REVISION_RE = re.compile(r"^[A-Za-z0-9_]{8,64}$")


def validate(current_revision: str, rendered_heads: str) -> str:
    if REVISION_RE.fullmatch(current_revision) is None:
        raise ValueError("current_revision_invalid")
    rows = [line.split()[0] for line in rendered_heads.splitlines() if line.strip()]
    if len(rows) != 1 or REVISION_RE.fullmatch(rows[0]) is None:
        raise ValueError("target_heads_invalid")
    if rows[0] != current_revision:
        raise ValueError("schema_identity_mismatch")
    return rows[0]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-revision", required=True)
    args = parser.parse_args(argv)
    try:
        revision = validate(args.current_revision, sys.stdin.read())
    except (OSError, ValueError):
        print("PRINCIPAL_SCHEMA_BLOCKED reason=identity_mismatch", file=sys.stderr)
        return 1
    print(revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
