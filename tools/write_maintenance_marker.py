#!/usr/bin/env python3
"""Atomically publish only bounded backup/restore-drill success timestamps."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile


DEFAULT_MARKER = Path("/run/taksklad-observability/maintenance.json")
KEYS = {
    "backup": "backup_success_at",
    "restore_drill": "restore_drill_success_at",
}


def _timestamp(value: str | None) -> str:
    if value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        parsed = parsed.astimezone(timezone.utc)
    else:
        parsed = datetime.now(timezone.utc)
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def write_marker(path: Path, kind: str, *, at: str | None = None) -> dict[str, str]:
    if kind not in KEYS:
        raise ValueError("unsupported maintenance kind")
    if path.is_symlink():
        raise ValueError("maintenance marker must not be a symlink")
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if path.exists():
        if not path.is_file() or path.stat().st_size > 1024:
            raise ValueError("existing maintenance marker is invalid")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("existing maintenance marker is invalid")
        existing = {key: str(loaded[key]) for key in KEYS.values() if key in loaded}
    existing[KEYS[kind]] = _timestamp(at)
    encoded = (json.dumps(existing, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > 1024:
        raise ValueError("maintenance marker exceeds size limit")
    fd, temporary_name = tempfile.mkstemp(prefix=".maintenance-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        # Timestamp-only payload; backend UID 10001 reads it through a ro bind.
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return existing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=tuple(KEYS))
    parser.add_argument("--path", type=Path, default=DEFAULT_MARKER)
    parser.add_argument("--at")
    args = parser.parse_args()
    payload = write_marker(args.path, args.kind, at=args.at)
    sys.stdout.write(f"MAINTENANCE_MARKER_OK kind={args.kind} fields={len(payload)} path={args.path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
