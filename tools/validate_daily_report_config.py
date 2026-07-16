#!/usr/bin/env python3
"""Validate rendered production daily-report config without printing values."""

from __future__ import annotations

import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.daily_report_config import (
    DailyReportConfigurationError,
    validate_production_daily_report_config,
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        environment = payload["services"]["telegram-worker"]["environment"]
        if not isinstance(environment, dict):
            raise KeyError("services.telegram-worker.environment")
        validate_production_daily_report_config(environment)
    except (json.JSONDecodeError, KeyError, TypeError):
        sys.stderr.write("DAILY_REPORT_CONFIG_INVALID fields=rendered_compose\n")
        return 1
    except DailyReportConfigurationError as exc:
        sys.stderr.write(
            "DAILY_REPORT_CONFIG_INVALID fields=" + ",".join(exc.setting_names) + "\n"
        )
        return 1
    sys.stdout.write("DAILY_REPORT_CONFIG_OK values_redacted=1\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
