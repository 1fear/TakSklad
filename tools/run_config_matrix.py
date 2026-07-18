#!/usr/bin/env python3
"""Run fail-closed configuration cases using synthetic process environments only."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_CHILD = (
    "from backend.app.main import validate_startup_configuration; "
    "validate_startup_configuration()"
)
TELEGRAM_CHILD = (
    "import os; "
    "from backend.app.telegram_worker import parse_chat_ids, validate_telegram_worker_config; "
    "validate_telegram_worker_config("
    "os.environ.get('TELEGRAM_BOT_TOKEN'), "
    "parse_chat_ids(os.environ.get('TELEGRAM_ALLOWED_CHAT_IDS')), "
    "parse_chat_ids(os.environ.get('TELEGRAM_ADMIN_CHAT_IDS')), "
    "parse_chat_ids(os.environ.get('SKLADBOT_DAILY_REPORT_CHAT_IDS')), "
    "parse_chat_ids(os.environ.get('TAKSKLAD_DAILY_RECONCILIATION_CHAT_IDS')), "
    "environment=os.environ.get('TAKSKLAD_ENV'), "
    "daily_report_enabled=os.environ.get('SKLADBOT_DAILY_REPORT_ENABLED', '').casefold() in {'1','true','yes','on'}, "
    "skladbot_api_tokens=tuple(value.strip() for value in os.environ.get('SKLADBOT_API_TOKENS', '').replace(';', ',').split(',') if value.strip()), "
    "daily_report_environ=os.environ, "
    "automation_alert_chat_id=os.environ.get('TAKSKLAD_AUTOMATION_ALERT_CHAT_ID'))"
)


@dataclass(frozen=True)
class MatrixCase:
    name: str
    child: str
    values: dict[str, str]
    expected_exit: int


def base_environment() -> dict[str, str]:
    allowed_host_names = ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP")
    result = {name: os.environ[name] for name in allowed_host_names if name in os.environ}
    result.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(PROJECT_ROOT),
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
    })
    return result


def matrix_cases() -> tuple[MatrixCase, ...]:
    api_token = "synthetic-" + "api-token"
    session_secret = "independent-" + "synthetic-session-secret"
    bot_token = "synthetic-" + "bot-token"
    return (
        MatrixCase("production_missing_auth", BACKEND_CHILD, {"TAKSKLAD_ENV": "production"}, 1),
        MatrixCase(
            "production_missing_session",
            BACKEND_CHILD,
            {"TAKSKLAD_ENV": "production", "TAKSKLAD_API_TOKEN": api_token},
            1,
        ),
        MatrixCase(
            "production_shared_session",
            BACKEND_CHILD,
            {
                "TAKSKLAD_ENV": "production",
                "TAKSKLAD_API_TOKEN": api_token,
                "TAKSKLAD_WEB_SESSION_SECRET": api_token,
            },
            1,
        ),
        MatrixCase(
            "production_weak_session",
            BACKEND_CHILD,
            {
                "TAKSKLAD_ENV": "production",
                "TAKSKLAD_API_TOKEN": api_token,
                "TAKSKLAD_WEB_SESSION_SECRET": "synthetic-weak",
            },
            1,
        ),
        MatrixCase(
            "production_valid",
            BACKEND_CHILD,
            {
                "TAKSKLAD_ENV": "production",
                "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
                "TAKSKLAD_WEB_SESSION_SECRET": session_secret,
                "TAKSKLAD_TRUSTED_PROXY_CIDRS": "172.18.0.0/16",
            },
            0,
        ),
        MatrixCase(
            "production_missing_proxy",
            BACKEND_CHILD,
            {
                "TAKSKLAD_ENV": "production",
                "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
                "TAKSKLAD_WEB_SESSION_SECRET": session_secret,
            },
            1,
        ),
        MatrixCase(
            "production_broad_proxy",
            BACKEND_CHILD,
            {
                "TAKSKLAD_ENV": "production",
                "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
                "TAKSKLAD_WEB_SESSION_SECRET": session_secret,
                "TAKSKLAD_TRUSTED_PROXY_CIDRS": "172.16.0.0/12",
            },
            1,
        ),
        MatrixCase(
            "production_extra_proxy",
            BACKEND_CHILD,
            {
                "TAKSKLAD_ENV": "production",
                "TAKSKLAD_IDENTITY_AUTH_ENABLED": "true",
                "TAKSKLAD_WEB_SESSION_SECRET": session_secret,
                "TAKSKLAD_TRUSTED_PROXY_CIDRS": "172.18.0.0/16,10.0.0.0/8",
            },
            1,
        ),
        MatrixCase("local_missing_opt_in", BACKEND_CHILD, {"TAKSKLAD_ENV": "local"}, 1),
        MatrixCase(
            "local_explicit_opt_in",
            BACKEND_CHILD,
            {"TAKSKLAD_ENV": "local", "TAKSKLAD_INSECURE_LOCAL_ANONYMOUS": "true"},
            0,
        ),
        MatrixCase(
            "telegram_missing_allowlist",
            TELEGRAM_CHILD,
            {"TELEGRAM_BOT_TOKEN": bot_token},
            1,
        ),
        MatrixCase(
            "telegram_admin_outside_allowed",
            TELEGRAM_CHILD,
            {
                "TELEGRAM_BOT_TOKEN": bot_token,
                "TELEGRAM_ALLOWED_CHAT_IDS": "1001",
                "TELEGRAM_ADMIN_CHAT_IDS": "2002",
            },
            1,
        ),
        MatrixCase(
            "telegram_valid",
            TELEGRAM_CHILD,
            {
                "TELEGRAM_BOT_TOKEN": bot_token,
                "TELEGRAM_ALLOWED_CHAT_IDS": "1001,2002",
                "TELEGRAM_ADMIN_CHAT_IDS": "2002",
                "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": "2002",
                "SKLADBOT_DAILY_REPORT_CHAT_IDS": "1001",
                "TAKSKLAD_DAILY_RECONCILIATION_CHAT_IDS": "2002",
            },
            0,
        ),
        MatrixCase(
            "telegram_production_missing_personal_alert",
            TELEGRAM_CHILD,
            {
                "TAKSKLAD_ENV": "production",
                "TAKSKLAD_TIMEZONE": "Asia/Tashkent",
                "TELEGRAM_BOT_TOKEN": bot_token,
                "TELEGRAM_ALLOWED_CHAT_IDS": "1001",
                "TELEGRAM_ADMIN_CHAT_IDS": "1001",
                "SKLADBOT_DAILY_REPORT_ENABLED": "true",
                "SKLADBOT_DAILY_REPORT_CHAT_IDS": "1001",
                "SKLADBOT_API_TOKENS": "synthetic-skladbot-token",
            },
            1,
        ),
        MatrixCase(
            "telegram_production_valid",
            TELEGRAM_CHILD,
            {
                "TAKSKLAD_ENV": "production",
                "TAKSKLAD_TIMEZONE": "Asia/Tashkent",
                "TELEGRAM_BOT_TOKEN": bot_token,
                "TELEGRAM_ALLOWED_CHAT_IDS": "1001",
                "TELEGRAM_ADMIN_CHAT_IDS": "1001",
                "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": "1001",
                "SKLADBOT_DAILY_REPORT_ENABLED": "true",
                "SKLADBOT_DAILY_REPORT_CHAT_IDS": "1001",
                "SKLADBOT_API_TOKENS": "synthetic-skladbot-token",
            },
            0,
        ),
    )


def run_case(case: MatrixCase) -> tuple[bool, int, bool]:
    environment = base_environment()
    environment.update(case.values)
    completed = subprocess.run(
        [sys.executable, "-c", case.child],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = completed.stdout + completed.stderr
    leaked = any(value and value in combined for value in case.values.values())
    actual_exit = 0 if completed.returncode == 0 else 1
    return actual_exit == case.expected_exit and not leaked, actual_exit, leaked


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dummy-only", action="store_true")
    arguments = parser.parse_args(argv)
    if not arguments.dummy_only:
        parser.error("--dummy-only is required")

    passed = 0
    failed = 0
    leaked_values = 0
    for case in matrix_cases():
        ok, actual_exit, leaked = run_case(case)
        passed += int(ok)
        failed += int(not ok)
        leaked_values += int(leaked)
        sys.stdout.write(
            f"config_matrix case={case.name} expected={case.expected_exit} "
            f"actual={actual_exit} status={'pass' if ok else 'fail'}\n"
        )
    sys.stdout.write(
        f"config_matrix_summary passed={passed} failed={failed} "
        f"leaked_values={leaked_values} inherited_sensitive=0\n"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
