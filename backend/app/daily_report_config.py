"""Pure production daily-report configuration contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .telegram_routing_contract import production_environment_errors


DAILY_REPORT_INTEGER_SETTINGS = {
    "SKLADBOT_DAILY_REPORT_HOUR": (22, 0, 23),
    "SKLADBOT_DAILY_REPORT_MINUTE": (0, 0, 59),
    "SKLADBOT_DAILY_REPORT_RETRY_MINUTES": (15, 1, 1440),
    "SKLADBOT_DAILY_REPORT_MAX_ATTEMPTS": (3, 1, 10),
    "SKLADBOT_DAILY_REPORT_GRACE_MINUTES": (30, 1, 1440),
    "SKLADBOT_DAILY_REPORT_LOOKBACK_DAYS": (1, 1, 31),
}


class DailyReportConfigurationError(RuntimeError):
    def __init__(self, setting_names):
        self.setting_names = tuple(sorted({str(name) for name in setting_names if str(name)}))
        super().__init__("Invalid daily-report configuration: " + ", ".join(self.setting_names))


@dataclass(frozen=True)
class DailyReportScheduleConfig:
    timezone_name: str
    hour: int
    minute: int
    retry_minutes: int
    max_attempts: int
    grace_minutes: int
    lookback_days: int


def _csv_items(value):
    return {
        part.strip()
        for part in str(value or "").replace(";", ",").split(",")
        if part.strip()
    }


def _bool_flag(value):
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on", "да"}


def _has_skladbot_token(environ):
    raw_pool = str(environ.get("SKLADBOT_API_TOKENS") or "").strip()
    raw_single = str(environ.get("SKLADBOT_API_TOKEN") or "").strip()
    return bool(raw_single or any(part for part in re.split(r"[,;\s]+", raw_pool) if part))


def _chat_ids_are_valid(values):
    return all(value.lstrip("-").isdigit() and int(value) != 0 for value in values)


def validate_daily_report_schedule_config(environ):
    errors = []
    timezone_name = str(environ.get("TAKSKLAD_TIMEZONE") or "Asia/Tashkent").strip()
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append("TAKSKLAD_TIMEZONE")

    values = {}
    for setting_name, (default, minimum, maximum) in DAILY_REPORT_INTEGER_SETTINGS.items():
        raw_value = str(environ.get(setting_name) or "").strip()
        try:
            value = default if not raw_value else int(raw_value)
        except ValueError:
            errors.append(setting_name)
            continue
        if value < minimum or value > maximum:
            errors.append(setting_name)
            continue
        values[setting_name] = value

    if errors:
        raise DailyReportConfigurationError(errors)
    return DailyReportScheduleConfig(
        timezone_name=timezone_name,
        hour=values["SKLADBOT_DAILY_REPORT_HOUR"],
        minute=values["SKLADBOT_DAILY_REPORT_MINUTE"],
        retry_minutes=values["SKLADBOT_DAILY_REPORT_RETRY_MINUTES"],
        max_attempts=values["SKLADBOT_DAILY_REPORT_MAX_ATTEMPTS"],
        grace_minutes=values["SKLADBOT_DAILY_REPORT_GRACE_MINUTES"],
        lookback_days=values["SKLADBOT_DAILY_REPORT_LOOKBACK_DAYS"],
    )


def validate_production_daily_report_config(environ):
    errors = []
    environment = str(environ.get("TAKSKLAD_ENV") or "").strip().casefold()
    telegram_token = str(environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    allowed_chat_ids = _csv_items(environ.get("TELEGRAM_ALLOWED_CHAT_IDS"))
    scheduled_chat_ids = _csv_items(environ.get("SKLADBOT_DAILY_REPORT_CHAT_IDS"))

    if environment != "production":
        errors.append("TAKSKLAD_ENV")
    if not telegram_token:
        errors.append("TELEGRAM_BOT_TOKEN")
    if not allowed_chat_ids or not _chat_ids_are_valid(allowed_chat_ids):
        errors.append("TELEGRAM_ALLOWED_CHAT_IDS")
    if not _bool_flag(environ.get("SKLADBOT_DAILY_REPORT_ENABLED")):
        errors.append("SKLADBOT_DAILY_REPORT_ENABLED")
    if not scheduled_chat_ids or not _chat_ids_are_valid(scheduled_chat_ids):
        errors.append("SKLADBOT_DAILY_REPORT_CHAT_IDS")
    elif not scheduled_chat_ids.issubset(allowed_chat_ids):
        errors.append("SKLADBOT_DAILY_REPORT_CHAT_IDS")
    if not _has_skladbot_token(environ):
        errors.append("SKLADBOT_API_TOKEN(S)")
    errors.extend(production_environment_errors(environ))

    schedule_config = None
    try:
        schedule_config = validate_daily_report_schedule_config(environ)
    except DailyReportConfigurationError as exc:
        errors.extend(exc.setting_names)

    if errors:
        raise DailyReportConfigurationError(errors)
    return schedule_config
