#!/usr/bin/env python3
"""Render a temporary synthetic Compose config without reading repository env files."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PROJECT_ROOT / "deploy" / "vds" / "config-contract.json"
KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def render_config(output: Path, contract_path: Path = CONTRACT_PATH) -> int:
    if output.name == ".env" or output.name.startswith(".env."):
        raise ValueError("synthetic config output must not use a forbidden .env* filename")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    values = dict(contract.get("compose_test_values") or {})
    for key, value in values.items():
        if not KEY_PATTERN.fullmatch(key):
            raise ValueError(f"invalid config key: {key}")
        if "\n" in str(value) or "\r" in str(value):
            raise ValueError(f"multiline config value is forbidden: {key}")

    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix="taksklad-compose-config-", dir=output.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file_obj:
            for key in sorted(values):
                file_obj.write(f"{key}={values[key]}\n")
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temporary_name, output)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return len(values)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        count = render_config(args.output)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"compose-test-config: error: {exc}\n")
        return 1
    sys.stdout.write(f"compose-test-config: written keys={count} path={args.output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
