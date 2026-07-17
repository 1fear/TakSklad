"""Shared entrypoint dispatch for source, windowed GUI, and console auth builds."""

from __future__ import annotations

import os
import sys

INSTALL_FLAG = "--install-backend-token-stdin"
CANARY_FLAG = "--auth-canary"
SMOKE_IMPORT_FLAG = "--smoke-import"
SMOKE_GUI_FLAG = "--smoke-gui"
HELP_FLAGS = {"-h", "--help"}


def _write(stream, message: str) -> None:
    if stream is not None:
        print(message, file=stream)


def _single_command(argv: list[str], allowed: set[str], error_stream) -> str | None:
    if not argv:
        return None
    if len(argv) != 1 or argv[0] not in allowed:
        _write(error_stream, "TAKSKLAD_CLI_BLOCKED reason=unsupported_or_conflicting_command")
        return "__invalid__"
    return argv[0]


def dispatch_source_cli(
    argv=None,
    *,
    input_stream=None,
    output_stream=None,
    error_stream=None,
    opener=None,
) -> int:
    """Developer/source entrypoint; production operators use TakSkladAuth.exe."""
    argv = list(sys.argv[1:] if argv is None else argv)
    output_stream = sys.stdout if output_stream is None else output_stream
    error_stream = sys.stderr if error_stream is None else error_stream
    command = _single_command(
        argv,
        {SMOKE_IMPORT_FLAG, SMOKE_GUI_FLAG, *HELP_FLAGS},
        error_stream,
    )
    if command == "__invalid__":
        return 2
    if command in HELP_FLAGS:
        _write(output_stream, "TakSklad source CLI: --smoke-import | --smoke-gui")
        return 0
    if command == SMOKE_IMPORT_FLAG:
        _write(output_stream, "TakSklad import OK")
        return 0
    from .main import run_app, run_gui_smoke

    if command == SMOKE_GUI_FLAG:
        return int(run_gui_smoke() or 0)
    return int(run_app() or 0)


def dispatch_gui_entrypoint(argv=None, *, output_stream=None, error_stream=None) -> int:
    """Windowed production entrypoint. It never handles secret-bearing commands."""
    argv = list(sys.argv[1:] if argv is None else argv)
    output_stream = sys.stdout if output_stream is None else output_stream
    error_stream = sys.stderr if error_stream is None else error_stream
    command = _single_command(
        argv,
        {SMOKE_IMPORT_FLAG, SMOKE_GUI_FLAG, *HELP_FLAGS},
        error_stream,
    )
    if command == "__invalid__":
        return 2
    if command in HELP_FLAGS:
        _write(output_stream, "TakSklad.exe: desktop application")
        return 0
    if command == SMOKE_IMPORT_FLAG:
        _write(output_stream, "TakSklad import OK")
        return 0
    from .main import run_app, run_gui_smoke

    if command == SMOKE_GUI_FLAG:
        return int(run_gui_smoke() or 0)
    return int(run_app() or 0)


def dispatch_auth_helper_cli(
    argv=None,
    *,
    input_stream=None,
    output_stream=None,
    error_stream=None,
    opener=None,
    frozen=None,
) -> int:
    """Console-only frozen helper using the immutable production backend origin."""
    argv = list(sys.argv[1:] if argv is None else argv)
    output_stream = sys.stdout if output_stream is None else output_stream
    error_stream = sys.stderr if error_stream is None else error_stream
    command = _single_command(argv, {INSTALL_FLAG, CANARY_FLAG, *HELP_FLAGS}, error_stream)
    if command == "__invalid__":
        return 2
    if command is None or command in HELP_FLAGS:
        _write(output_stream, "TakSkladAuth.exe: --install-backend-token-stdin | --auth-canary")
        return 0
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    if not is_frozen:
        _write(error_stream, "TAKSKLAD_AUTH_HELPER_BLOCKED reason=signed_frozen_helper_required")
        return 2

    from .returns_auth_canary import PRODUCTION_BACKEND_ORIGIN
    from .config import TAKSKLAD_BACKEND_TIMEOUT_SECONDS
    from .desktop_auth import (
        install_scoped_backend_token_from_stdin,
        run_desktop_returns_auth_canary,
    )

    if command == INSTALL_FLAG:
        principal_identifier = os.environ.get("TAKSKLAD_DESKTOP_PRINCIPAL_IDENTIFIER", "")
        return install_scoped_backend_token_from_stdin(
            PRODUCTION_BACKEND_ORIGIN,
            expected_identifier=principal_identifier,
            timeout=TAKSKLAD_BACKEND_TIMEOUT_SECONDS,
            input_stream=input_stream,
            output_stream=output_stream,
            error_stream=error_stream,
            opener=opener,
        )
    return run_desktop_returns_auth_canary(
        PRODUCTION_BACKEND_ORIGIN,
        timeout=TAKSKLAD_BACKEND_TIMEOUT_SECONDS,
        output_stream=output_stream,
        error_stream=error_stream,
        opener=opener,
    )
