import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from backend.app.telegram_routing_contract import (
    ROUTING_IDENTITY_ANCHOR_ENV,
    canonical_route_identity_sha256,
)
from tools.prepare_notification_routing_env import (
    NotificationRoutingConfigError,
    main,
    parse_env_assignments,
    prepare_notification_routing,
    render_env_candidate,
    write_candidate_file,
)


CLIENT_ID = "-1002001"
LOGISTICS_ID = "-1002002"
ADMIN_ID = "1001"
FINGERPRINT = "k" * 64
IDENTITY_ANCHOR = canonical_route_identity_sha256(CLIENT_ID, LOGISTICS_ID, ADMIN_ID)


class PrepareNotificationRoutingEnvTests(unittest.TestCase):
    def runtime(self):
        telegram = {
            "TELEGRAM_ALLOWED_CHAT_IDS": f"{CLIENT_ID},{LOGISTICS_ID},{ADMIN_ID}",
            "TELEGRAM_ADMIN_CHAT_IDS": ADMIN_ID,
            "SKLADBOT_DAILY_REPORT_CHAT_IDS": CLIENT_ID,
            "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID": CLIENT_ID,
            "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID": LOGISTICS_ID,
            "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": ADMIN_ID,
        }
        smartup = {
            "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID": CLIENT_ID,
            "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID": LOGISTICS_ID,
            "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": ADMIN_ID,
            "SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID": "",
            "SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY": FINGERPRINT,
        }
        persisted = {
            "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID": CLIENT_ID,
            "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID": LOGISTICS_ID,
            "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": ADMIN_ID,
            "SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID": "",
            "SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY": FINGERPRINT,
        }
        return telegram, smartup, persisted

    def test_exact_distinct_routes_and_schedules_are_prepared(self):
        telegram, smartup, persisted = self.runtime()
        prepared = prepare_notification_routing(telegram, smartup, persisted, IDENTITY_ANCHOR)

        self.assertEqual(prepared.repaired_route_roles, ())
        self.assertEqual(prepared.slot_count, 3)
        self.assertEqual(prepared.updates["SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID"], CLIENT_ID)
        self.assertEqual(prepared.updates["SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID"], LOGISTICS_ID)
        self.assertEqual(prepared.updates["TAKSKLAD_AUTOMATION_ALERT_CHAT_ID"], ADMIN_ID)
        self.assertEqual(prepared.updates["TELEGRAM_ADMIN_CHAT_IDS"], ADMIN_ID)
        self.assertEqual(prepared.updates["SKLADBOT_DAILY_REPORT_CHAT_IDS"], CLIENT_ID)
        self.assertEqual(prepared.updates["SMARTUP_AUTO_IMPORT_TIMES"], "12:00,15:00,17:50")
        self.assertEqual(prepared.updates["SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME"], "17:50")
        self.assertEqual(prepared.updates["SKLADBOT_DAILY_REPORT_HOUR"], "22")
        self.assertEqual(prepared.updates["SKLADBOT_DAILY_REPORT_MINUTE"], "0")
        summary = str(prepared.safe_summary())
        for raw_value in (CLIENT_ID, LOGISTICS_ID, ADMIN_ID, FINGERPRINT):
            self.assertNotIn(raw_value, summary)

    def test_role_collision_or_wrong_target_type_is_blocked(self):
        telegram, smartup, persisted = self.runtime()
        cases = (
            {**persisted, "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID": CLIENT_ID},
            {**persisted, "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID": ADMIN_ID},
            {**persisted, "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": LOGISTICS_ID},
            {**persisted, "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID": "not-a-chat"},
        )
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(NotificationRoutingConfigError):
                    prepare_notification_routing(telegram, smartup, case, IDENTITY_ANCHOR)

    def test_protected_anchor_blocks_consistent_role_swap_wrong_admin_and_malformed_anchor(self):
        telegram, smartup, persisted = self.runtime()
        swapped_telegram = {
            **telegram,
            "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID": LOGISTICS_ID,
            "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID": CLIENT_ID,
            "SKLADBOT_DAILY_REPORT_CHAT_IDS": LOGISTICS_ID,
        }
        swapped_smartup = {
            **smartup,
            "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID": LOGISTICS_ID,
            "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID": CLIENT_ID,
        }
        swapped_persisted = {
            **persisted,
            "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID": LOGISTICS_ID,
            "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID": CLIENT_ID,
        }
        with self.assertRaises(NotificationRoutingConfigError):
            prepare_notification_routing(
                swapped_telegram,
                swapped_smartup,
                swapped_persisted,
                IDENTITY_ANCHOR,
            )

        wrong_admin = "1002"
        wrong_admin_telegram = {
            **telegram,
            "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": wrong_admin,
            "TELEGRAM_ADMIN_CHAT_IDS": wrong_admin,
            "TELEGRAM_ALLOWED_CHAT_IDS": f"{CLIENT_ID},{LOGISTICS_ID},{wrong_admin}",
        }
        wrong_admin_smartup = {**smartup, "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": wrong_admin}
        wrong_admin_persisted = {**persisted, "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": wrong_admin}
        with self.assertRaises(NotificationRoutingConfigError):
            prepare_notification_routing(
                wrong_admin_telegram,
                wrong_admin_smartup,
                wrong_admin_persisted,
                IDENTITY_ANCHOR,
            )
        for malformed in (None, "", "not-a-sha256", "a" * 63):
            with self.subTest(anchor=malformed), self.assertRaises(NotificationRoutingConfigError):
                prepare_notification_routing(telegram, smartup, persisted, malformed)

    def test_runtime_unknown_or_foreign_route_is_blocked_without_fallback(self):
        telegram, smartup, persisted = self.runtime()
        bad_runtime = dict(telegram)
        bad_runtime["TELEGRAM_ALLOWED_CHAT_IDS"] = f"{CLIENT_ID},{LOGISTICS_ID},1009"
        with self.assertRaises(NotificationRoutingConfigError):
            prepare_notification_routing(bad_runtime, smartup, persisted, IDENTITY_ANCHOR)

        bad_smartup = dict(smartup)
        bad_smartup["SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID"] = "-1002009"
        with self.assertRaises(NotificationRoutingConfigError):
            prepare_notification_routing(telegram, bad_smartup, persisted, IDENTITY_ANCHOR)

    def test_legacy_alert_and_missing_or_weak_fingerprint_are_blocked(self):
        telegram, smartup, persisted = self.runtime()
        for changes in (
            {"SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID": ADMIN_ID},
            {"SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY": "weak"},
        ):
            case = {**persisted, **changes}
            with self.subTest(changes=changes):
                with self.assertRaises(NotificationRoutingConfigError):
                    prepare_notification_routing(telegram, smartup, case, IDENTITY_ANCHOR)
        no_fingerprint_smartup = {**smartup, "SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY": ""}
        no_fingerprint_persisted = {**persisted, "SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY": ""}
        with self.assertRaises(NotificationRoutingConfigError):
            prepare_notification_routing(
                telegram,
                no_fingerprint_smartup,
                no_fingerprint_persisted,
                IDENTITY_ANCHOR,
            )

    def test_updates_are_routing_only_and_do_not_mutate_operational_flags(self):
        telegram, smartup, persisted = self.runtime()
        prepared = prepare_notification_routing(telegram, smartup, persisted, IDENTITY_ANCHOR)
        forbidden = {
            "SMARTUP_AUTO_IMPORT_ENABLED",
            "SMARTUP_AUTO_IMPORT_BACKEND_IMPORT_ENABLED",
            "SMARTUP_AUTO_IMPORT_PROCESS_SKLADBOT_NOW",
            "SMARTUP_AUTO_IMPORT_SAGA_MODE",
            "SKLADBOT_CREATE_REQUESTS_MODE",
            "TAKSKLAD_API_TOKEN_EXPIRES_AT",
        }
        self.assertTrue(forbidden.isdisjoint(prepared.updates))
        self.assertNotIn(ROUTING_IDENTITY_ANCHOR_ENV, prepared.updates)

    def test_routing_update_cannot_mutate_auth_configuration(self):
        with self.assertRaises(NotificationRoutingConfigError):
            render_env_candidate(
                "TAKSKLAD_LEGACY_AUTH_EXPIRES_AT=operator-value\n",
                {"TAKSKLAD_LEGACY_AUTH_EXPIRES_AT": "replacement"},
            )

    def test_auth_lines_are_preserved_byte_for_byte(self):
        source = (
            "# auth stays operator-owned\r\n"
            "  TAKSKLAD_LEGACY_AUTH_MODE = 'enforce'  \r\n"
            'TAKSKLAD_LEGACY_AUTH_EXPIRES_AT="operator-cutoff"\r\n'
            "ROUTE=old\r\n"
        )
        rendered = render_env_candidate(source, {"ROUTE": "new"})
        for auth_line in source.splitlines(keepends=True)[1:3]:
            self.assertIn(auth_line, rendered)

    def test_render_is_byte_preserving_idempotent_and_rejects_duplicate_managed_key(self):
        telegram, smartup, persisted = self.runtime()
        updates = prepare_notification_routing(
            telegram, smartup, persisted, IDENTITY_ANCHOR
        ).updates
        source = (
            "# keep CRLF\r\n"
            "UNRELATED = opaque\r\n"
            "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID=old\r\n"
            "NO_NEWLINE=preserved"
        )
        rendered = render_env_candidate(source, updates)
        self.assertEqual(render_env_candidate(rendered, updates), rendered)
        self.assertIn("# keep CRLF\r\nUNRELATED = opaque\r\n", rendered)
        self.assertIn("NO_NEWLINE=preserved\r\n", rendered)
        self.assertEqual(parse_env_assignments(rendered)["SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID"], CLIENT_ID)

        duplicate = source + "\r\nSMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID=again\r\n"
        with self.assertRaises(NotificationRoutingConfigError):
            render_env_candidate(duplicate, updates)

    def test_cli_preserves_crlf_and_unrelated_duplicate_operational_lines(self):
        telegram, smartup, _ = self.runtime()
        source = (
            "# exact CRLF source\r\n"
            "TAKSKLAD_API_TOKEN_EXPIRES_AT=first-opaque-value\r\n"
            "SMARTUP_AUTO_IMPORT_ENABLED=true\r\n"
            "SMARTUP_AUTO_IMPORT_BACKEND_IMPORT_ENABLED=true\r\n"
            "SMARTUP_AUTO_IMPORT_PROCESS_SKLADBOT_NOW=false\r\n"
            "SMARTUP_AUTO_IMPORT_SAGA_MODE=enforced\r\n"
            "SKLADBOT_CREATE_REQUESTS_MODE=live\r\n"
            "TAKSKLAD_API_TOKEN_EXPIRES_AT=second-opaque-value\r\n"
            f"SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID={CLIENT_ID}\r\n"
            f"SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID={LOGISTICS_ID}\r\n"
            f"TAKSKLAD_AUTOMATION_ALERT_CHAT_ID={ADMIN_ID}\r\n"
            "SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID=\r\n"
            f"SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY={FINGERPRINT}\r\n"
            "TELEGRAM_ALLOWED_CHAT_IDS=1001,-1002002,-1002001\r\n"
            "TELEGRAM_ADMIN_CHAT_IDS=1001\r\n"
            "SKLADBOT_DAILY_REPORT_CHAT_IDS=-1002001\r\n"
            "SKLADBOT_DAILY_REPORT_HOUR=23\r\n"
            "SKLADBOT_DAILY_REPORT_MINUTE=55\r\n"
            "SMARTUP_AUTO_IMPORT_TIMES=12:00,15:00\r\n"
            "SMARTUP_AUTO_IMPORT_FINAL_TIME=17:40\r\n"
            "SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME=17:45\r\n"
            "BACKEND_STATUS_WRITE=leave-byte-identical\r\n"
        ).encode("utf-8")
        expected = (
            "# exact CRLF source\r\n"
            "TAKSKLAD_API_TOKEN_EXPIRES_AT=first-opaque-value\r\n"
            "SMARTUP_AUTO_IMPORT_ENABLED=true\r\n"
            "SMARTUP_AUTO_IMPORT_BACKEND_IMPORT_ENABLED=true\r\n"
            "SMARTUP_AUTO_IMPORT_PROCESS_SKLADBOT_NOW=false\r\n"
            "SMARTUP_AUTO_IMPORT_SAGA_MODE=enforced\r\n"
            "SKLADBOT_CREATE_REQUESTS_MODE=live\r\n"
            "TAKSKLAD_API_TOKEN_EXPIRES_AT=second-opaque-value\r\n"
            f"SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID={CLIENT_ID}\r\n"
            f"SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID={LOGISTICS_ID}\r\n"
            f"TAKSKLAD_AUTOMATION_ALERT_CHAT_ID={ADMIN_ID}\r\n"
            "SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID=\r\n"
            f"SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY={FINGERPRINT}\r\n"
            f"TELEGRAM_ALLOWED_CHAT_IDS={CLIENT_ID},{LOGISTICS_ID},{ADMIN_ID}\r\n"
            f"TELEGRAM_ADMIN_CHAT_IDS={ADMIN_ID}\r\n"
            f"SKLADBOT_DAILY_REPORT_CHAT_IDS={CLIENT_ID}\r\n"
            "SKLADBOT_DAILY_REPORT_HOUR=22\r\n"
            "SKLADBOT_DAILY_REPORT_MINUTE=0\r\n"
            "SMARTUP_AUTO_IMPORT_TIMES=12:00,15:00,17:50\r\n"
            "SMARTUP_AUTO_IMPORT_FINAL_TIME=17:50\r\n"
            "SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME=17:50\r\n"
            "BACKEND_STATUS_WRITE=leave-byte-identical\r\n"
        ).encode("utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "source"
            first_path = Path(temp_dir) / "first"
            second_path = Path(temp_dir) / "second"
            source_path.write_bytes(source)
            first_args = [
                "--env-path", str(source_path),
                "--candidate-path", str(first_path),
                "--telegram-container-id", "telegram",
                "--smartup-container-id", "smartup",
            ]
            with mock.patch.dict(os.environ, {ROUTING_IDENTITY_ANCHOR_ENV: IDENTITY_ANCHOR}), mock.patch(
                "tools.prepare_notification_routing_env.inspect_container_env",
                side_effect=(telegram, smartup),
            ):
                self.assertEqual(main(first_args), 0)
            self.assertEqual(first_path.read_bytes(), expected)

            second_args = [
                "--env-path", str(first_path),
                "--candidate-path", str(second_path),
                "--telegram-container-id", "telegram",
                "--smartup-container-id", "smartup",
            ]
            with mock.patch.dict(os.environ, {ROUTING_IDENTITY_ANCHOR_ENV: IDENTITY_ANCHOR}), mock.patch(
                "tools.prepare_notification_routing_env.inspect_container_env",
                side_effect=(telegram, smartup),
            ):
                self.assertEqual(main(second_args), 0)
            self.assertEqual(second_path.read_bytes(), expected)

    def test_candidate_writer_does_not_overwrite_existing_file_or_follow_symlink(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chmod(temp_dir, 0o700)
            root = Path(temp_dir)
            existing = root / "existing"
            existing.write_bytes(b"foreign-existing")
            with self.assertRaises(NotificationRoutingConfigError):
                write_candidate_file(existing, b"candidate")
            self.assertEqual(existing.read_bytes(), b"foreign-existing")

            target = root / "foreign-target"
            target.write_bytes(b"foreign-target")
            linked = root / "linked-candidate"
            linked.symlink_to(target)
            with self.assertRaises(NotificationRoutingConfigError):
                write_candidate_file(linked, b"candidate")
            self.assertTrue(linked.is_symlink())
            self.assertEqual(target.read_bytes(), b"foreign-target")


if __name__ == "__main__":
    unittest.main()
