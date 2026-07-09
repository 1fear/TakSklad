#!/usr/bin/env python3
"""Run the full local unittest surface without reading repository .env files."""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_MODULES = {}


def discover_safe_test_modules(root: Path = PROJECT_ROOT) -> list[str]:
    modules = []
    for path in sorted((root / "tests").glob("test_*.py")):
        module = f"tests.{path.stem}"
        if module not in EXCLUDED_MODULES:
            modules.append(module)
    return modules


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--verbosity", type=int, default=1)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    modules = discover_safe_test_modules()
    if args.list:
        sys.stdout.write("\n".join(modules) + "\n")
        return 0

    sys.stdout.write(f"safe-tests: modules={len(modules)} excluded={len(EXCLUDED_MODULES)}\n")
    for module, reason in sorted(EXCLUDED_MODULES.items()):
        sys.stdout.write(f"safe-tests: excluded {module} ({reason})\n")
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    result = unittest.TextTestRunner(verbosity=args.verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.path.insert(0, str(PROJECT_ROOT))
    raise SystemExit(main())
