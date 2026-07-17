import json
import subprocess
import sys
import unittest
from pathlib import Path

from backend.app.daily_report_config import validate_daily_report_schedule_config
from backend.app.settings import load_settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = PROJECT_ROOT / "tools" / "validate_daily_report_config.py"


def complete_production_environment():
    return {
        "TAKSKLAD_ENV": "production",
        "TAKSKLAD_TIMEZONE": "Asia/Tashkent",
        "TELEGRAM_BOT_TOKEN": "synthetic-telegram-token",
        "TELEGRAM_ALLOWED_CHAT_IDS": "-1002001,-1002002,1001",
        "TELEGRAM_ADMIN_CHAT_IDS": "1001",
        "SKLADBOT_DAILY_REPORT_ENABLED": "true",
        "SKLADBOT_DAILY_REPORT_CHAT_IDS": "-1002001",
        "SKLADBOT_API_TOKEN": "synthetic-skladbot-token",
        "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": "1001",
        "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID": "-1002001",
        "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID": "-1002002",
        "SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID": "",
        "SMARTUP_AUTO_IMPORT_TIMES": "12:00,15:00,17:50",
        "SMARTUP_AUTO_IMPORT_FINAL_TIME": "17:50",
        "SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME": "17:50",
        "SKLADBOT_DAILY_REPORT_HOUR": "22",
        "SKLADBOT_DAILY_REPORT_MINUTE": "0",
    }


class DailyReportConfigTests(unittest.TestCase):
    def run_validator(self, environment):
        payload = {
            "services": {
                "telegram-worker": {
                    "environment": environment,
                }
            }
        }
        return subprocess.run(
            [sys.executable, str(VALIDATOR)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
            cwd=PROJECT_ROOT,
        )

    def test_accepts_complete_production_daily_configuration(self):
        completed = self.run_validator(complete_production_environment())

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("DAILY_REPORT_CONFIG_OK", completed.stdout)
        self.assertNotIn("synthetic", completed.stdout + completed.stderr)

    def test_rejects_missing_production_daily_configuration_without_leaking_values(self):
        completed = self.run_validator({
            **complete_production_environment(),
            "TELEGRAM_BOT_TOKEN": "synthetic-secret-token",
            "SKLADBOT_DAILY_REPORT_ENABLED": "false",
            "SKLADBOT_DAILY_REPORT_CHAT_IDS": "",
            "SKLADBOT_API_TOKEN": "",
        })

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("SKLADBOT_DAILY_REPORT_ENABLED", completed.stderr)
        self.assertIn("SKLADBOT_DAILY_REPORT_CHAT_IDS", completed.stderr)
        self.assertIn("SKLADBOT_API_TOKEN(S)", completed.stderr)
        self.assertNotIn("synthetic-secret-token", completed.stdout + completed.stderr)

    def test_rejects_non_production_rendered_environment(self):
        completed = self.run_validator({
            **complete_production_environment(),
            "TAKSKLAD_ENV": "test",
        })

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("TAKSKLAD_ENV", completed.stderr)

    def test_rejects_invalid_schedule_fields_with_shared_strict_ranges(self):
        base = complete_production_environment()
        invalid_values = {
            "TAKSKLAD_TIMEZONE": "Invalid/Timezone",
            "SKLADBOT_DAILY_REPORT_HOUR": "24",
            "SKLADBOT_DAILY_REPORT_MINUTE": "60",
            "SKLADBOT_DAILY_REPORT_RETRY_MINUTES": "0",
            "SKLADBOT_DAILY_REPORT_MAX_ATTEMPTS": "11",
            "SKLADBOT_DAILY_REPORT_GRACE_MINUTES": "1441",
            "SKLADBOT_DAILY_REPORT_LOOKBACK_DAYS": "32",
        }

        for setting_name, invalid_value in invalid_values.items():
            with self.subTest(setting_name=setting_name):
                completed = self.run_validator({
                    **base,
                    setting_name: invalid_value,
                })
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(setting_name, completed.stderr)

    def test_backend_and_worker_schedule_parsers_share_exact_values(self):
        environ = {
            "TAKSKLAD_ENV": "test",
            "TAKSKLAD_TIMEZONE": "Asia/Tashkent",
            "SKLADBOT_DAILY_REPORT_HOUR": "21",
            "SKLADBOT_DAILY_REPORT_MINUTE": "47",
            "SKLADBOT_DAILY_REPORT_RETRY_MINUTES": "17",
            "SKLADBOT_DAILY_REPORT_MAX_ATTEMPTS": "4",
            "SKLADBOT_DAILY_REPORT_GRACE_MINUTES": "29",
            "SKLADBOT_DAILY_REPORT_LOOKBACK_DAYS": "9",
        }

        schedule = validate_daily_report_schedule_config(environ)
        settings = load_settings(environ)

        self.assertEqual(settings.timezone, schedule.timezone_name)
        self.assertEqual(settings.skladbot_daily_report_hour, schedule.hour)
        self.assertEqual(settings.skladbot_daily_report_minute, schedule.minute)
        self.assertEqual(settings.skladbot_daily_report_retry_minutes, schedule.retry_minutes)
        self.assertEqual(settings.skladbot_daily_report_max_attempts, schedule.max_attempts)
        self.assertEqual(settings.skladbot_daily_report_grace_minutes, schedule.grace_minutes)
        self.assertEqual(settings.skladbot_daily_report_lookback_days, schedule.lookback_days)


if __name__ == "__main__":
    unittest.main()
