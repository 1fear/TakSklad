#!/usr/bin/env python3
"""Validate the protected deploy-user acceptance credential file without outputting it."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import sys


SCOPED_TOKEN_BYTES_RE = re.compile(rb"tks\.[0-9a-f]{32}\.[A-Za-z0-9_-]{32,}")
MAX_FILE_BYTES = 4097


def validate(path_text: str) -> None:
    path = Path(path_text)
    if not path.is_absolute():
        raise ValueError("path_not_absolute")
    file_stat = path.lstat()
    effective_uid = os.geteuid()
    if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
        raise ValueError("file_not_regular")
    if file_stat.st_uid != effective_uid:
        raise ValueError("file_owner_mismatch")
    if stat.S_IMODE(file_stat.st_mode) not in {0o400, 0o600}:
        raise ValueError("file_mode_unsafe")
    parent_stat = path.parent.lstat()
    if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
        raise ValueError("parent_not_directory")
    if parent_stat.st_uid != effective_uid or stat.S_IMODE(parent_stat.st_mode) & 0o022:
        raise ValueError("parent_owner_or_mode_unsafe")
    if file_stat.st_size < 2 or file_stat.st_size > MAX_FILE_BYTES:
        raise ValueError("file_size_invalid")
    raw = path.read_bytes()
    credential = raw[:-2] if raw.endswith(b"\r\n") else raw[:-1] if raw.endswith(b"\n") else raw
    if not SCOPED_TOKEN_BYTES_RE.fullmatch(credential):
        raise ValueError("credential_format_invalid")


def main(argv=None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) != 1:
        print("AUTH_CANARY_TOKEN_FILE_BLOCKED reason=usage", file=sys.stderr)
        return 2
    try:
        validate(values[0])
    except (OSError, ValueError):
        print("AUTH_CANARY_TOKEN_FILE_BLOCKED reason=unsafe_or_invalid", file=sys.stderr)
        return 1
    print("AUTH_CANARY_TOKEN_FILE_OK owner=current_user mode=protected format=scoped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
