#!/usr/bin/env python3
"""Run a credentialed read-only returns canary without exposing its token."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from taksklad.returns_auth_canary import (  # noqa: E402
    PRODUCTION_BACKEND_ORIGIN,
    ReturnsAuthCanaryError,
    read_credential_from_stdin,
    run_returns_auth_canary,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    token = parser.add_mutually_exclusive_group(required=True)
    token.add_argument("--acceptance-token-stdin", action="store_true")
    token.add_argument("--desktop-token-stdin", action="store_true")
    parser.add_argument("--identifier", required=True)
    parser.add_argument("--allow-missing-endpoint", action="store_true")
    parser.add_argument("--require-missing-endpoint", action="store_true")
    parser.add_argument("--timeout", type=int, default=8)
    return parser.parse_args(argv)


def main(argv=None, *, input_stream=None, output_stream=None, error_stream=None, opener=None):
    args = parse_args(argv)
    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    error_stream = sys.stderr if error_stream is None else error_stream
    if args.require_missing_endpoint and not args.allow_missing_endpoint:
        print(
            "RETURNS_AUTH_CANARY_BLOCKED reason=invalid_bootstrap_mode",
            file=error_stream,
        )
        return 2
    try:
        token = read_credential_from_stdin(input_stream)
        kwargs = {"timeout": args.timeout}
        if opener is not None:
            kwargs["opener"] = opener
        kind = "desktop" if args.desktop_token_stdin else "acceptance"
        result = run_returns_auth_canary(
            PRODUCTION_BACKEND_ORIGIN,
            token,
            require_scoped=True,
            canary_kind=kind,
            identifier=args.identifier,
            allow_missing_endpoint=args.allow_missing_endpoint,
            **kwargs,
        )
        if args.require_missing_endpoint and result.status != 404:
            raise ReturnsAuthCanaryError("legacy_endpoint_must_be_absent")
    except ReturnsAuthCanaryError as exc:
        print(f"RETURNS_AUTH_CANARY_BLOCKED reason={exc}", file=error_stream)
        return 1
    except Exception:
        print("RETURNS_AUTH_CANARY_BLOCKED reason=unexpected_failure", file=error_stream)
        return 1

    if result.status == 404:
        print(
            "RETURNS_AUTH_CANARY_SKIPPED reason=endpoint_absent bootstrap_rollback=1",
            file=output_stream,
        )
    else:
        print(
            "RETURNS_AUTH_CANARY_OK "
            f"status={result.status} kind={result.canary_kind} "
            "credentialed=1 read_only=1 data_free=1 origin=pinned",
            file=output_stream,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
