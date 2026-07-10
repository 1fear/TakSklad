#!/usr/bin/env python3
"""Validate the compile-time Windows release signer allowlist via Python AST."""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path


CONSTANT_NAME = "TRUSTED_WINDOWS_SIGNER_CERT_SHA256"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def load_allowlist(path: Path) -> frozenset[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assignments: list[ast.AST] = []
    permitted_store_nodes: list[ast.Name] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            matching_targets = [
                target
                for target in node.targets
                if isinstance(target, ast.Name) and target.id == CONSTANT_NAME
            ]
            if matching_targets:
                assignments.append(node.value)
                permitted_store_nodes.extend(matching_targets)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == CONSTANT_NAME
            and node.value is not None
        ):
            assignments.append(node.value)
            permitted_store_nodes.append(node.target)
    all_store_nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == CONSTANT_NAME
        and isinstance(node.ctx, ast.Store)
    ]
    if len(assignments) != 1:
        raise ValueError(f"{CONSTANT_NAME} must have exactly one top-level assignment")
    if len(all_store_nodes) != 1 or all_store_nodes != permitted_store_nodes:
        raise ValueError(f"{CONSTANT_NAME} cannot be reassigned")

    value = assignments[0]
    if not (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "frozenset"
        and not value.keywords
        and len(value.args) <= 1
    ):
        raise ValueError(f"{CONSTANT_NAME} must be a literal frozenset")
    if not value.args:
        return frozenset()
    literal = value.args[0]
    if not isinstance(literal, (ast.Set, ast.List, ast.Tuple)):
        raise ValueError(f"{CONSTANT_NAME} must contain only literal SHA256 strings")
    entries: list[str] = []
    for element in literal.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            raise ValueError(f"{CONSTANT_NAME} must contain only literal SHA256 strings")
        fingerprint = element.value.strip().lower()
        if not SHA256_RE.fullmatch(fingerprint):
            raise ValueError(f"{CONSTANT_NAME} contains an invalid SHA256 fingerprint")
        entries.append(fingerprint)
    if len(entries) != len(set(entries)):
        raise ValueError(f"{CONSTANT_NAME} contains duplicate fingerprints")
    return frozenset(entries)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("src/taksklad/update_service.py"),
    )
    parser.add_argument("--require", default="")
    args = parser.parse_args()
    try:
        allowlist = load_allowlist(args.source)
        required = args.require.strip().lower()
        if required and not SHA256_RE.fullmatch(required):
            raise ValueError("required signer fingerprint is not lowercase SHA256")
        if required and required not in allowlist:
            raise ValueError("WINDOWS_CODESIGN_IDENTITY_NOT_PINNED")
    except (OSError, SyntaxError, ValueError) as exc:
        print(f"WINDOWS_SIGNER_ALLOWLIST_ERROR: {exc}")
        return 1
    print(
        "WINDOWS_SIGNER_ALLOWLIST_OK "
        f"entries={len(allowlist)} required={int(bool(required))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
