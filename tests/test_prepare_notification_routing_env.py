import unittest
from unittest import mock

from tools.prepare_notification_routing_env import (
    NotificationRoutingConfigError,
    parse_env_assignments,
    prepare_notification_routing,
    render_env_candidate,
)


class PrepareNotificationRoutingEnvTests(unittest.TestCase):
    def runtime(self):
        telegram = {
            "TELEGRAM_ALLOWED_CHAT_IDS": "-1002001,1001",
            "TELEGRAM_ADMIN_CHAT_IDS": "1001",
            "SKLADBOT_DAILY_REPORT_CHAT_IDS": "-1002001",
        }
        smartup = {
            "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID": "-1002001",
            "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID": "1001",
            "SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID": "1001",
            "SMARTUP_AUTO_IMPORT_TIMES": "12:00,15:00,17:50",
            "SMARTUP_AUTO_IMPORT_FINAL_TIME": "17:50",
        }
        return telegram, smartup

    def test_repairs_proven_personal_logistics_route_and_enables_one_date_recovery(self):
        telegram, smartup = self.runtime()
        with mock.patch(
            "tools.prepare_notification_routing_env.secrets.token_hex",
            return_value="a" * 64,
        ):
            prepared = prepare_notification_routing(
                telegram,
                smartup,
                {},
                recovery_export_date="2026-07-16",
            )

        self.assertTrue(prepared.repaired_personal_logistics_route)
        self.assertTrue(prepared.generated_fingerprint_key)
        self.assertEqual(
            prepared.updates["SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID"],
            "-1002001",
        )
        self.assertEqual(
            prepared.updates["TAKSKLAD_AUTOMATION_ALERT_CHAT_ID"],
            "1001",
        )
        self.assertEqual(
            prepared.updates["SMARTUP_AUTO_IMPORT_LOGISTICS_ROUTE_RECOVERY_EXPORT_DATE"],
            "2026-07-16",
        )
        self.assertEqual(prepared.updates["SMARTUP_AUTO_IMPORT_ENABLED"], "true")
        self.assertEqual(prepared.updates["SKLADBOT_CREATE_REQUESTS_MODE"], "enabled")
        self.assertNotIn("-1002001", str(prepared.safe_summary()))
        self.assertNotIn("1001", str(prepared.safe_summary()))
        self.assertNotIn("a" * 64, str(prepared.safe_summary()))

    def test_preserves_stable_key_and_does_not_recover_already_correct_route(self):
        telegram, smartup = self.runtime()
        smartup["SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID"] = "-1002001"
        persisted = {"SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY": "k" * 64}

        prepared = prepare_notification_routing(
            telegram,
            smartup,
            persisted,
            recovery_export_date="2026-07-16",
        )

        self.assertFalse(prepared.repaired_personal_logistics_route)
        self.assertFalse(prepared.generated_fingerprint_key)
        self.assertEqual(
            prepared.updates["SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY"],
            "k" * 64,
        )
        self.assertEqual(
            prepared.updates["SMARTUP_AUTO_IMPORT_LOGISTICS_ROUTE_RECOVERY_EXPORT_DATE"],
            "",
        )

    def test_preserves_exact_recovery_marker_across_runtime_rollback(self):
        telegram, smartup = self.runtime()
        smartup["SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID"] = "-1002001"
        smartup["SMARTUP_AUTO_IMPORT_LOGISTICS_ROUTE_RECOVERY_EXPORT_DATE"] = "2026-07-16"
        persisted = {
            "SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY": "k" * 64,
            "SMARTUP_AUTO_IMPORT_LOGISTICS_ROUTE_RECOVERY_EXPORT_DATE": "2026-07-16",
        }

        prepared = prepare_notification_routing(
            telegram,
            smartup,
            persisted,
            recovery_export_date="2026-07-16",
        )

        self.assertFalse(prepared.repaired_personal_logistics_route)
        self.assertEqual(
            prepared.updates["SMARTUP_AUTO_IMPORT_LOGISTICS_ROUTE_RECOVERY_EXPORT_DATE"],
            "2026-07-16",
        )

    def test_blocks_different_existing_recovery_marker(self):
        telegram, smartup = self.runtime()
        smartup["SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID"] = "-1002001"
        persisted = {
            "SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY": "k" * 64,
            "SMARTUP_AUTO_IMPORT_LOGISTICS_ROUTE_RECOVERY_EXPORT_DATE": "2026-07-15",
        }

        with self.assertRaises(NotificationRoutingConfigError):
            prepare_notification_routing(
                telegram,
                smartup,
                persisted,
                recovery_export_date="2026-07-16",
            )

    def test_blocks_ambiguous_group_admin_and_unknown_logistics_routes(self):
        telegram, smartup = self.runtime()
        cases = []
        two_groups = dict(telegram)
        two_groups["TELEGRAM_ALLOWED_CHAT_IDS"] = "-1002001,-1002002,1001"
        cases.append((two_groups, smartup))
        two_admins = dict(telegram)
        two_admins["TELEGRAM_ADMIN_CHAT_IDS"] = "1001,1002"
        two_admins["TELEGRAM_ALLOWED_CHAT_IDS"] = "-1002001,1001,1002"
        cases.append((two_admins, smartup))
        unknown_logistics = dict(smartup)
        unknown_logistics["SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID"] = "1009"
        cases.append((telegram, unknown_logistics))

        for telegram_case, smartup_case in cases:
            with self.subTest(telegram=telegram_case, smartup=smartup_case):
                with self.assertRaises(NotificationRoutingConfigError):
                    prepare_notification_routing(
                        telegram_case,
                        smartup_case,
                        {},
                        recovery_export_date="2026-07-16",
                    )

    def test_blocks_schedule_reset_and_alert_drift(self):
        telegram, smartup = self.runtime()
        bad_schedule = dict(smartup)
        bad_schedule["SMARTUP_AUTO_IMPORT_TIMES"] = "12:00,17:50"
        bad_alert = dict(smartup)
        bad_alert["SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID"] = "1009"

        for smartup_case in (bad_schedule, bad_alert):
            with self.subTest(smartup=smartup_case):
                with self.assertRaises(NotificationRoutingConfigError):
                    prepare_notification_routing(
                        telegram,
                        smartup_case,
                        {},
                        recovery_export_date="2026-07-16",
                    )

    def test_env_render_replaces_duplicates_without_exposing_or_losing_other_values(self):
        source = "# keep\nSECRET_TOKEN=opaque\nROUTE=old\nROUTE=stale\n"
        rendered = render_env_candidate(source, {"ROUTE": "new", "ADDED": "value"})

        self.assertEqual(rendered.count("ROUTE="), 1)
        self.assertIn("SECRET_TOKEN=opaque", rendered)
        self.assertIn("ROUTE=new", rendered)
        self.assertIn("ADDED=value", rendered)
        self.assertEqual(parse_env_assignments(rendered)["ROUTE"], "new")


if __name__ == "__main__":
    unittest.main()
