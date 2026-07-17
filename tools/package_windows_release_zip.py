#!/usr/bin/env python3
"""Create the canonical file-only Windows release ZIP and validate it."""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import stat
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from taksklad.windows_release_zip import validate_windows_release_zip  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--outer-manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    source = args.source.resolve()
    if not source.is_dir() or source.is_symlink() or args.output.exists():
        raise SystemExit("WINDOWS_ZIP_PACKAGE_BLOCKED")
    files = sorted(path for path in source.rglob("*") if path.is_file())
    if not files or any(path.is_symlink() for path in files):
        raise SystemExit("WINDOWS_ZIP_PACKAGE_BLOCKED")
    with zipfile.ZipFile(args.output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = PurePosixPath(path.relative_to(source).as_posix())
            name = f"TakSklad/{relative}"
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    outer = json.loads(args.outer_manifest.read_text(encoding="utf-8-sig"))
    outer.setdefault("app_sha256_onedir", outer.get("app_sha256"))
    outer.setdefault("auth_helper_sha256_onedir", outer.get("auth_helper_sha256"))
    validate_windows_release_zip(args.output, outer, expected_source_sha=outer.get("source_sha"))
    print("WINDOWS_RELEASE_ZIP_OK canonical=1 membership=manifest-bound")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
