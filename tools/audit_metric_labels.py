#!/usr/bin/env python3
"""Fail-closed audit for bounded observability labels and log fields."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "monitoring/observability/signal-catalog.json"
BACKEND_LOG_SOURCES = tuple(sorted((ROOT / "backend/app").glob("*.py")))
LABEL_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
FORBIDDEN_LOG_TERMS = frozenset({"address", "auth_token", "client_name", "kiz", "phone"})


def load_catalog() -> dict:
    payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("signals"), list):
        raise ValueError("invalid signal catalog schema")
    return payload


def audit_catalog(payload: dict) -> tuple[int, int, list[str]]:
    forbidden = set(payload.get("privacy", {}).get("forbidden_data_classes") or ())
    names: set[str] = set()
    total_series = 0
    errors: list[str] = []
    for signal in payload["signals"]:
        name = str(signal.get("name") or "")
        if not LABEL_RE.fullmatch(name):
            errors.append(f"invalid metric name: {name}")
        if name in names:
            errors.append(f"duplicate metric name: {name}")
        names.add(name)
        labels = signal.get("labels") or {}
        computed_series = 1
        for label_name, values in labels.items():
            if not LABEL_RE.fullmatch(label_name):
                errors.append(f"invalid label name: {name}.{label_name}")
            if label_name in forbidden:
                errors.append(f"forbidden label data class: {name}.{label_name}")
            if not isinstance(values, list) or not values:
                errors.append(f"label domain must be a non-empty list: {name}.{label_name}")
                continue
            if len(values) != len(set(values)) or len(values) > 32:
                errors.append(f"label domain is duplicate or unbounded: {name}.{label_name}")
            computed_series *= len(values)
        maximum = int(signal.get("maximum_series") or 0)
        if maximum != computed_series or maximum > 512:
            errors.append(f"series bound mismatch: {name} declared={maximum} computed={computed_series}")
        total_series += computed_series
    return len(names), total_series, errors


def audit_observability_logs() -> tuple[list[str], int]:
    errors: list[str] = []
    log_calls = 0
    for path in BACKEND_LOG_SOURCES:
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = ""
            if isinstance(node.func, ast.Attribute):
                function_name = node.func.attr
            elif isinstance(node.func, ast.Name):
                function_name = node.func.id
            if function_name not in {"debug", "info", "warning", "error", "exception", "critical"}:
                continue
            log_calls += 1
            text = " ".join(
                str(argument.value).casefold()
                for argument in node.args
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            )
            matches = sorted(term for term in FORBIDDEN_LOG_TERMS if term in text)
            if matches:
                errors.append(f"forbidden log field in {path.name}:{node.lineno}: {','.join(matches)}")
            referenced_names = {
                child.id.casefold()
                for argument in (*node.args, *[keyword.value for keyword in node.keywords])
                for child in ast.walk(argument)
                if isinstance(child, ast.Name)
            }
            referenced_attributes = {
                child.attr.casefold()
                for argument in (*node.args, *[keyword.value for keyword in node.keywords])
                for child in ast.walk(argument)
                if isinstance(child, ast.Attribute)
            }
            dynamic_matches = sorted(
                term for term in FORBIDDEN_LOG_TERMS
                if term in referenced_names or term in referenced_attributes
            )
            if dynamic_matches:
                errors.append(
                    f"forbidden dynamic log value in {path.name}:{node.lineno}: {','.join(dynamic_matches)}"
                )
    return errors, log_calls


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    if not args.strict:
        parser.error("--strict is required")
    try:
        signal_count, maximum_series, errors = audit_catalog(load_catalog())
        log_errors, log_calls = audit_observability_logs()
        errors.extend(log_errors)
    except (OSError, ValueError, json.JSONDecodeError, SyntaxError) as exc:
        sys.stderr.write(f"METRIC_LABEL_AUDIT_FAIL error={exc}\n")
        return 1
    if errors:
        for error in errors:
            sys.stderr.write(f"METRIC_LABEL_AUDIT_ERROR {error}\n")
        sys.stderr.write(f"METRIC_LABEL_AUDIT_FAIL forbidden_count={len(errors)}\n")
        return 1
    print(
        f"METRIC_LABEL_AUDIT_OK signals={signal_count} maximum_series={maximum_series} "
        f"scanned_log_sources={len(BACKEND_LOG_SOURCES)} scanned_log_calls={log_calls} "
        "forbidden_label_count=0 forbidden_log_count=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
