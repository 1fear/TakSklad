import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from backend.app.telegram_output_contract import (
    runtime_output_artifacts,
    runtime_output_policy_hashes,
    transfer_kiz_export_caption,
)

from backend.app.telegram_routing_contract import (
    ROUTING_IDENTITY_ANCHOR_ENV,
    TelegramMessageKind,
    TelegramRoutingContractError,
    _validate_manifest,
    canonical_route_identity_sha256,
    load_telegram_routing_contract,
    production_environment_errors,
    validate_route_values,
)
from tools.verify_telegram_routing_contract import main as verify_main


CLIENT_ID = "-1002001"
LOGISTICS_ID = "-1002002"
ADMIN_ID = "1001"
IDENTITY_ANCHOR = canonical_route_identity_sha256(CLIENT_ID, LOGISTICS_ID, ADMIN_ID)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TelegramRoutingContractTests(unittest.TestCase):
    def setUp(self):
        load_telegram_routing_contract.cache_clear()
        self.contract = load_telegram_routing_contract()

    def environment(self):
        return {
            "TAKSKLAD_ENV": "production",
            "TAKSKLAD_TIMEZONE": "Asia/Tashkent",
            "TELEGRAM_BOT_TOKEN": "synthetic-token",
            "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID": CLIENT_ID,
            "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID": LOGISTICS_ID,
            "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": ADMIN_ID,
            "SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID": "",
            "SMARTUP_AUTO_IMPORT_TIMES": "12:00,15:00,17:50",
            "SMARTUP_AUTO_IMPORT_FINAL_TIME": "17:50",
            "SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME": "17:50",
            "SKLADBOT_DAILY_REPORT_HOUR": "22",
            "SKLADBOT_DAILY_REPORT_MINUTE": "0",
            "TELEGRAM_ALLOWED_CHAT_IDS": f"{CLIENT_ID},{LOGISTICS_ID},{ADMIN_ID}",
            "TELEGRAM_ADMIN_CHAT_IDS": ADMIN_ID,
            "SKLADBOT_DAILY_REPORT_CHAT_IDS": CLIENT_ID,
            "SKLADBOT_DAILY_REPORT_ENABLED": "true",
            "SKLADBOT_API_TOKENS": "synthetic-skladbot-token",
        }

    def compose(self, environment):
        return {
            "services": {
                "telegram-worker": {"environment": dict(environment)},
                "smartup-auto-import-worker": {
                    "environment": {
                        key: environment[key]
                        for key in (
                            "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID",
                            "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID",
                            "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID",
                            "SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID",
                            "SMARTUP_AUTO_IMPORT_TIMES",
                            "SMARTUP_AUTO_IMPORT_FINAL_TIME",
                            "SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME",
                            "TELEGRAM_ADMIN_CHAT_IDS",
                        )
                    }
                },
            }
        }

    def run_verifier(
        self,
        environment,
        compose=None,
        identity_anchor=IDENTITY_ANCHOR,
        state_mode=0o700,
        file_mode=0o600,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chmod(temp_dir, state_mode)
            env_path = Path(temp_dir) / "candidate.env"
            compose_path = Path(temp_dir) / "compose.json"
            env_path.write_text(
                "".join(f"{key}={value}\n" for key, value in environment.items()),
                encoding="utf-8",
            )
            compose_path.write_text(
                json.dumps(compose or self.compose(environment)),
                encoding="utf-8",
            )
            os.chmod(env_path, file_mode)
            os.chmod(compose_path, file_mode)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=False), redirect_stdout(stdout), redirect_stderr(stderr):
                if identity_anchor is None:
                    os.environ.pop(ROUTING_IDENTITY_ANCHOR_ENV, None)
                else:
                    os.environ[ROUTING_IDENTITY_ANCHOR_ENV] = identity_anchor
                status = verify_main([
                    "--env-path", str(env_path),
                    "--compose-config-json", str(compose_path),
                    "--json",
                ])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_exact_message_kind_routing_matrix(self):
        expected = {
            TelegramMessageKind.SMARTUP_CLIENT_EXPORT: ("client", ("12:00", "15:00", "17:50")),
            TelegramMessageKind.SMARTUP_LOGISTICS_REPORT: ("logistics", ("17:50",)),
            TelegramMessageKind.SKLADBOT_DAILY_REPORT: ("client", ("22:00",)),
            TelegramMessageKind.TRANSFER_KIZ_EXPORT: ("client", ("on_completion",)),
            TelegramMessageKind.ADMIN_ERROR: ("admin", ("on_error",)),
        }
        for kind, (destination, schedules) in expected.items():
            route = self.contract.route_for(kind)
            self.assertEqual(route.destination, destination)
            self.assertEqual(route.schedules, schedules)
            self.assertEqual(route.error_destination, "admin")

    def test_unknown_and_near_miss_kinds_are_blocked(self):
        for kind in (
            "smartup_client",
            "smartup_logistics_report ",
            "transfer_kiz",
            "transfer_kiz_export ",
            "service",
            "",
        ):
            with self.subTest(kind=kind), self.assertRaises(TelegramRoutingContractError):
                self.contract.route_for(kind)
        for kind in ("daily_reconciliation_alert ", "foreign_kind", "admin_error"):
            with self.subTest(kind=kind), self.assertRaises(TelegramRoutingContractError):
                self.contract.route_for_notification_kind(kind)

    def test_pairwise_distinct_target_types_and_missing_values(self):
        self.assertEqual(validate_route_values(CLIENT_ID, LOGISTICS_ID, ADMIN_ID), [])
        cases = (
            (CLIENT_ID, CLIENT_ID, ADMIN_ID),
            (CLIENT_ID, LOGISTICS_ID, LOGISTICS_ID),
            (ADMIN_ID, LOGISTICS_ID, ADMIN_ID),
            (CLIENT_ID, ADMIN_ID, ADMIN_ID),
            (CLIENT_ID, LOGISTICS_ID, ""),
            (CLIENT_ID, LOGISTICS_ID, "invalid"),
        )
        for values in cases:
            with self.subTest(values=values):
                self.assertTrue(validate_route_values(*values))

    def test_production_environment_exact_matrix_and_near_misses(self):
        environment = self.environment()
        self.assertEqual(production_environment_errors(environment), [])
        mutations = (
            ("SMARTUP_AUTO_IMPORT_TIMES", "12:00,15:00,17:49"),
            ("SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME", "17:51"),
            ("SKLADBOT_DAILY_REPORT_HOUR", "21"),
            ("SKLADBOT_DAILY_REPORT_CHAT_IDS", LOGISTICS_ID),
            ("TELEGRAM_ADMIN_CHAT_IDS", f"{ADMIN_ID},1002"),
            ("SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID", CLIENT_ID),
            ("SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID", ADMIN_ID),
        )
        for setting, value in mutations:
            with self.subTest(setting=setting):
                changed = {**environment, setting: value}
                self.assertIn(setting if setting != "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID" else "TELEGRAM_ROUTE_ROLE_COLLISION", production_environment_errors(changed))

    def test_text_policies_are_exact_hashes_of_runtime_builder_outputs(self):
        artifacts = runtime_output_artifacts()
        hashes = runtime_output_policy_hashes()
        self.assertEqual(set(artifacts), {kind.value for kind in TelegramMessageKind})
        self.assertEqual(set(hashes), set(artifacts))
        forbidden = (
            "AUTO" + " Smartup ·",
            "MANUAL" + " /logistics ·",
            "_" + "AUTO.xlsx",
            "_" + "MANUAL.xlsx",
            "_" + "OUT.xlsx",
            "_" + "OUT SPOT.xlsx",
            "_" + "OUT_SPOT.xlsx",
        )
        rendered = json.dumps(artifacts, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("Движения:", artifacts[TelegramMessageKind.SKLADBOT_DAILY_REPORT.value]["message"])
        for kind in TelegramMessageKind:
            route = self.contract.route_for(kind)
            self.assertEqual(hashes[kind.value], route.text_policy_sha256)
        for marker in forbidden:
            self.assertNotIn(marker, rendered)

    def test_transfer_kiz_export_output_is_exact_and_completion_only(self):
        kind = TelegramMessageKind.TRANSFER_KIZ_EXPORT
        self.assertEqual(
            transfer_kiz_export_caption("source.xlsx"),
            "Коды маркировки по файлу: source.xlsx",
        )
        self.assertEqual(
            runtime_output_artifacts()[kind.value],
            {"caption": "Коды маркировки по файлу: transfer_kiz_export.xlsx"},
        )
        self.assertEqual(self.contract.route_for(kind).schedules, ("on_completion",))

    def test_on_completion_is_allowlisted_only_for_transfer_kiz_export(self):
        manifest = json.loads(
            (PROJECT_ROOT / "backend" / "app" / "telegram_routing_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        for kind in (
            TelegramMessageKind.SMARTUP_CLIENT_EXPORT,
            TelegramMessageKind.SMARTUP_LOGISTICS_REPORT,
            TelegramMessageKind.SKLADBOT_DAILY_REPORT,
            TelegramMessageKind.ADMIN_ERROR,
        ):
            with self.subTest(kind=kind.value):
                changed = json.loads(json.dumps(manifest))
                changed["message_kinds"][kind.value]["schedules"] = ["on_completion"]
                with self.assertRaises(TelegramRoutingContractError):
                    _validate_manifest(changed)

        changed = json.loads(json.dumps(manifest))
        changed["message_kinds"][TelegramMessageKind.TRANSFER_KIZ_EXPORT.value]["schedules"] = ["on_error"]
        with self.assertRaises(TelegramRoutingContractError):
            _validate_manifest(changed)

    def test_no_send_verifier_redacts_raw_ids(self):
        status, output, error = self.run_verifier(self.environment())
        self.assertEqual(status, 0, error)
        payload = json.loads(output)
        self.assertTrue(payload["raw_chat_ids_redacted"])
        self.assertTrue(payload["candidate_config_validated"])
        self.assertTrue(payload["protected_identity_anchor_validated"])
        self.assertTrue(payload["runtime_outputs_validated"])
        rendered = output + error
        for raw_value in (CLIENT_ID, LOGISTICS_ID, ADMIN_ID):
            self.assertNotIn(raw_value, rendered)

    def test_no_send_verifier_negative_candidate_matrix_never_false_greens(self):
        base = self.environment()
        mutations = (
            {"SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID": CLIENT_ID},
            {"TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": CLIENT_ID},
            {"SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID": ADMIN_ID},
            {"TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": LOGISTICS_ID},
            {"SMARTUP_AUTO_IMPORT_TIMES": "12:00,15:00,17:49"},
            {"SMARTUP_AUTO_IMPORT_FINAL_TIME": "17:49"},
            {"SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME": "17:49"},
            {"SKLADBOT_DAILY_REPORT_HOUR": "21"},
            {"SKLADBOT_DAILY_REPORT_MINUTE": "1"},
        )
        for mutation in mutations:
            with self.subTest(setting=next(iter(mutation))):
                changed = {**base, **mutation}
                status, output, error = self.run_verifier(changed)
                self.assertNotEqual(status, 0)
                rendered = output + error
                for raw_value in (CLIENT_ID, LOGISTICS_ID, ADMIN_ID):
                    self.assertNotIn(raw_value, rendered)

    def test_protected_identity_anchor_blocks_consistent_swaps_and_wrong_admin(self):
        base = self.environment()
        swapped = {
            **base,
            "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID": LOGISTICS_ID,
            "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID": CLIENT_ID,
            "TELEGRAM_ALLOWED_CHAT_IDS": f"{LOGISTICS_ID},{CLIENT_ID},{ADMIN_ID}",
            "SKLADBOT_DAILY_REPORT_CHAT_IDS": LOGISTICS_ID,
        }
        wrong_admin_id = "1002"
        wrong_admin = {
            **base,
            "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": wrong_admin_id,
            "TELEGRAM_ADMIN_CHAT_IDS": wrong_admin_id,
            "TELEGRAM_ALLOWED_CHAT_IDS": f"{CLIENT_ID},{LOGISTICS_ID},{wrong_admin_id}",
        }
        cases = (
            (swapped, IDENTITY_ANCHOR),
            (wrong_admin, IDENTITY_ANCHOR),
            ({**base, ROUTING_IDENTITY_ANCHOR_ENV: IDENTITY_ANCHOR}, IDENTITY_ANCHOR),
            (base, None),
            (base, "malformed"),
        )
        for environment, anchor in cases:
            with self.subTest(anchor_present=anchor is not None):
                status, output, error = self.run_verifier(
                    environment,
                    self.compose(environment),
                    identity_anchor=anchor,
                )
                self.assertNotEqual(status, 0)
                rendered = output + error
                self.assertNotIn(IDENTITY_ANCHOR, rendered)
                for raw_value in (CLIENT_ID, LOGISTICS_ID, ADMIN_ID, wrong_admin_id):
                    self.assertNotIn(raw_value, rendered)

        status, output, error = self.run_verifier(base, self.compose(base))
        self.assertEqual(status, 0, error)
        self.assertNotIn(IDENTITY_ANCHOR, output + error)

    def test_no_send_verifier_rejects_unprotected_candidate_files(self):
        for state_mode, file_mode in ((0o755, 0o600), (0o700, 0o644)):
            with self.subTest(state_mode=state_mode, file_mode=file_mode):
                status, output, error = self.run_verifier(
                    self.environment(),
                    state_mode=state_mode,
                    file_mode=file_mode,
                )
                self.assertNotEqual(status, 0)
                self.assertNotIn(IDENTITY_ANCHOR, output + error)

    def test_no_send_verifier_blocks_compose_runtime_mismatch(self):
        environment = self.environment()
        compose = self.compose(environment)
        compose["services"]["smartup-auto-import-worker"]["environment"][
            "SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME"
        ] = "17:49"
        status, _, error = self.run_verifier(environment, compose)
        self.assertNotEqual(status, 0)
        self.assertIn("compose.smartup-auto-import-worker", error)

    def test_daily_schedule_has_one_active_truth_at_2200(self):
        route = self.contract.route_for(TelegramMessageKind.SKLADBOT_DAILY_REPORT)
        self.assertEqual(route.schedules, ("22:00",))
        config_source = (PROJECT_ROOT / "src" / "taksklad" / "config.py").read_text(
            encoding="utf-8"
        )
        desktop_source = (PROJECT_ROOT / "src" / "taksklad" / "telegram_service.py").read_text(
            encoding="utf-8"
        )
        docs = "\n".join(
            (PROJECT_ROOT / path).read_text(encoding="utf-8")
            for path in (
                "docs/taksklad-full-functionality.md",
                "docs/project-architecture.md",
            )
        )
        self.assertNotIn("DAILY_REPORT_" + "AUTO_SEND_", config_source + desktop_source)
        self.assertNotIn("23" + ":55", config_source + desktop_source + docs)
        self.assertIn("22:00", docs)
        self.assertIn("desktop", docs.casefold())


if __name__ == "__main__":
    unittest.main()
