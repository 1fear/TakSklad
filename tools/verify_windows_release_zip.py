#!/usr/bin/env python3
"""Validate or safely extract an attested TakSklad Windows ZIP."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from taksklad.windows_release_zip import (  # noqa: E402
    WindowsReleaseZipError,
    extract_windows_release_zip,
    validate_windows_release_zip,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--outer-manifest", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--extract-to", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.extract_to is not None and not args.extract_to.is_absolute():
            raise WindowsReleaseZipError("extract_destination_must_be_absolute")
        outer = json.loads(args.outer_manifest.read_text(encoding="utf-8-sig"))
        if args.extract_to is None:
            validate_windows_release_zip(args.zip, outer, expected_source_sha=args.source_sha)
        else:
            extract_windows_release_zip(
                args.zip,
                args.extract_to,
                outer,
                expected_source_sha=args.source_sha,
            )
    except (OSError, ValueError, json.JSONDecodeError, WindowsReleaseZipError):
        print("WINDOWS_RELEASE_ZIP_BLOCKED reason=identity_or_layout_invalid", file=sys.stderr)
        return 1
    print("WINDOWS_RELEASE_ZIP_OK canonical=1 identity=bound extracted=" + str(args.extract_to is not None).lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
