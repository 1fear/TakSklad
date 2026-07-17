#!/usr/bin/env python3
"""Revalidate the actual fresh PostgreSQL archive before a principal mutation."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid


DEFAULT_ROOT = Path("/opt/taksklad/backups/postgres/completed")
HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def safe_node(path: Path, *, directory: bool, uid: int, modes: set[int]):
    value = path.lstat()
    expected = stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
    if not expected or stat.S_ISLNK(value.st_mode) or value.st_uid != uid or stat.S_IMODE(value.st_mode) not in modes:
        raise ValueError("unsafe_node")
    return value


def canonical_existing(path: Path) -> Path:
    if not path.is_absolute() or path != Path(os.path.abspath(path)):
        raise ValueError("path_not_canonical")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        value = current.lstat()
        if stat.S_ISLNK(value.st_mode):
            raise ValueError("path_symlink_forbidden")
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise ValueError("path_identity_mismatch")
    return resolved


def inside(child: Path, parent: Path) -> bool:
    return child != parent and child.is_relative_to(parent)


def validate(
    root: Path,
    result_file: Path,
    operation_id: str,
    expected_migration_head: str,
    *,
    now: datetime,
    max_age_seconds: int = 900,
    expected_archive: Path | None = None,
) -> dict[str, Path]:
    uid = os.geteuid()
    root = canonical_existing(root)
    result_file = canonical_existing(result_file)
    if expected_archive is not None:
        expected_archive = canonical_existing(expected_archive)
    safe_node(root, directory=True, uid=uid, modes={0o700, 0o750})
    safe_node(result_file, directory=False, uid=uid, modes={0o400, 0o600})
    result = json.loads(result_file.read_text(encoding="utf-8"))
    if result.get("schema") != 1 or result.get("operation_id") != operation_id:
        raise ValueError("result_binding_invalid")
    manifest_path = canonical_existing(Path(str(result.get("manifest_path") or "")))
    archive_from_result = canonical_existing(Path(str(result.get("archive_path") or "")))
    if (
        not inside(manifest_path, root)
        or not inside(archive_from_result, root)
        or manifest_path.parent != archive_from_result.parent
    ):
        raise ValueError("result_path_invalid")
    bundle = canonical_existing(manifest_path.parent)
    safe_node(bundle, directory=True, uid=uid, modes={0o700})
    manifests = list(bundle.glob("*.manifest.json"))
    if len(manifests) != 1 or manifests[0] != manifest_path:
        raise ValueError("manifest_set_invalid")
    safe_node(manifest_path, directory=False, uid=uid, modes={0o400, 0o600})
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive_meta = manifest.get("archive") or {}
    created = datetime.fromisoformat(str(manifest.get("created_at_utc") or "").replace("Z", "+00:00"))
    if created.tzinfo is None or not 0 <= (now - created.astimezone(timezone.utc)).total_seconds() <= max_age_seconds:
        raise ValueError("backup_stale")
    if (
        manifest.get("schema_version") != 2
        or manifest.get("atomic_bundle") is not True
        or manifest.get("actual_postgresql") is not True
        or manifest.get("source") != "postgresql"
        or manifest.get("migration_head") != expected_migration_head
        or archive_meta.get("format") != "postgresql-custom"
        or archive_meta.get("validated") is not True
        or not HEX_RE.fullmatch(str(archive_meta.get("sha256") or ""))
    ):
        raise ValueError("backup_metadata_invalid")
    archive_name = str(archive_meta.get("filename") or "")
    if not archive_name or Path(archive_name).name != archive_name:
        raise ValueError("archive_name_invalid")
    archive = canonical_existing(bundle / archive_name)
    if expected_archive is not None and archive != expected_archive:
        raise ValueError("expected_archive_mismatch")
    if (
        archive != archive_from_result
        or result.get("archive_sha256") != archive_meta.get("sha256")
        or result.get("archive_bytes") != archive_meta.get("bytes")
        or result.get("migration_head") != manifest.get("migration_head")
        or result.get("created_at_utc") != manifest.get("created_at_utc")
    ):
        raise ValueError("result_archive_binding_invalid")
    archive_stat = safe_node(archive, directory=False, uid=uid, modes={0o400, 0o600})
    if archive_stat.st_size != int(archive_meta.get("bytes") or -1):
        raise ValueError("archive_size_mismatch")
    if not hmac.compare_digest(digest(archive), archive_meta["sha256"]):
        raise ValueError("archive_digest_mismatch")
    with archive.open("rb") as handle:
        if handle.read(5) != b"PGDMP":
            raise ValueError("archive_header_invalid")
    list_meta = archive_meta.get("list") or {}
    list_name = str(list_meta.get("filename") or "")
    if Path(list_name).name != list_name or list_meta.get("validated") is not True:
        raise ValueError("restore_list_invalid")
    restore_list = canonical_existing(bundle / list_name)
    safe_node(restore_list, directory=False, uid=uid, modes={0o400, 0o600})
    if not hmac.compare_digest(digest(restore_list), str(list_meta.get("sha256") or "")):
        raise ValueError("restore_list_digest_mismatch")
    return {
        "root": root,
        "result_file": result_file,
        "manifest": manifest_path,
        "archive": archive,
        "restore_list": restore_list,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-migration-head", required=True)
    parser.add_argument("--result-file", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--expected-archive", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[A-Za-z0-9_]{8,64}", args.expected_migration_head):
            raise ValueError("migration_head_invalid")
        if str(uuid.UUID(args.operation_id)) != args.operation_id:
            raise ValueError("operation_id_invalid")
        validate(
            args.root,
            args.result_file,
            args.operation_id,
            args.expected_migration_head,
            now=datetime.now(timezone.utc),
            expected_archive=args.expected_archive,
        )
    except (OSError, ValueError, json.JSONDecodeError):
        print("PRINCIPAL_BACKUP_BLOCKED reason=actual_backup_unverified", file=sys.stderr)
        return 1
    print("PRINCIPAL_BACKUP_OK actual_archive=verified freshness=short migration_identity=matched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
