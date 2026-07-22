from datetime import date
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from tools.verify_daily_report_recovery_preflight import (
    COVERAGE_ERROR_PREFIX,
    PRE_TELEGRAM_STAGES,
    RecoveryPreflightError,
    _event_is_proven_pre_telegram_failure,
    _event_is_proven_safe_manual_recovery,
    verify_preflight,
)


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy" / "vds" / "deploy_from_git.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-server-production.yml"
DATES = (date(2026, 7, 20), date(2026, 7, 21))


def ready_payload():
    return {
        "http_status": 503,
        "payload": {
            "ready": False,
            "database": {"status": "ok"},
            "migrations": {"status": "ok"},
            "queue": {
                "hot_path_stale_processing_count": 0,
                "hot_path_blocking_count": 3,
                "hot_path_error_count": 3,
            },
            "imports": {"recent_error_count": 0},
            "workers": {
                "status": "ok",
                "required_count": 3,
                "missing_count": 0,
                "unhealthy_count": 0,
            },
            "daily_report": {
                "status": "unhealthy",
                "due_date": "2026-07-21",
                "missing_count": 1,
            },
            "desktop_pairing": {
                "status": "ok",
                "overdue_unacked_count": 0,
                "stale_cleanup_count": 0,
                "sweeper_heartbeat_stale": False,
            },
            "policy": {"mandatory_status": "unhealthy"},
        },
    }


def database_payload():
    return {
        "status": "inspect_ok",
        "report_dates": ["2026-07-20", "2026-07-21"],
        "configured_chat_count": 1,
        "schedule_2200_tashkent": True,
        "blocker_count": 3,
        "blockers_by_date": {"2026-07-20": 1, "2026-07-21": 2},
        "active_count": 0,
        "success_count": 0,
        "registry_count": 0,
        "ambiguous_count": 0,
        "unrelated_blocker_count": 0,
        "target_daily_failure_count": 3,
        "safe_target_blocker_count": 3,
        "values_redacted": True,
    }


def dry_runs():
    return [
        {
            "status": "ready",
            "report_date": value,
            "requests_count": 1,
            "order_kiz_count": 2,
            "day_kiz_count": 1,
            "xlsx_bytes": 4096,
        }
        for value in ("2026-07-20", "2026-07-21")
    ]


class DailyReportRecoveryPreflightTests(unittest.TestCase):
    def test_every_allowlisted_pre_telegram_stage_is_accepted(self):
        for stage in PRE_TELEGRAM_STAGES:
            with self.subTest(stage=stage):
                event = SimpleNamespace(
                    status="failed",
                    last_error=f"{COVERAGE_ERROR_PREFIX}: coverage_status=partial",
                    payload={
                        "stage": stage,
                        "result_status": "blocked_partial",
                        "success": False,
                    },
                )
                self.assertTrue(_event_is_proven_pre_telegram_failure(event))

    def test_pre_telegram_proof_rejects_unsafe_or_incomplete_failure(self):
        base_payload = {
            "stage": "report generation finished",
            "result_status": "blocked_partial",
            "success": False,
        }
        mutations = (
            {"stage": "telegram sendMessage started"},
            {"stage": "telegram sendMessage success"},
            {"stage": "telegram sendDocument started"},
            {"result_status": "failed"},
            {"success": True},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                payload = {**base_payload, **mutation}
                event = SimpleNamespace(
                    status="failed",
                    last_error=f"{COVERAGE_ERROR_PREFIX}: coverage_status=partial",
                    payload=payload,
                )
                self.assertFalse(_event_is_proven_pre_telegram_failure(event))

        wrong_error = SimpleNamespace(
            status="failed",
            last_error="telegram_send_failed",
            payload=base_payload,
        )
        self.assertFalse(_event_is_proven_pre_telegram_failure(wrong_error))

    def test_safe_manual_recovery_accepts_coverage_failure_reclassified_after_retry_window(self):
        event = SimpleNamespace(
            status="failed",
            last_error=f"{COVERAGE_ERROR_PREFIX}: coverage_status=partial",
            payload={
                "stage": "manual_recovery_required",
                "result_status": "manual_recovery_required",
                "manual_recovery_required": True,
                "manual_recovery_reason": "automatic_retry_not_safe_or_exhausted",
                "same_day_existing_event_status": "failed",
                "success": False,
            },
        )
        self.assertTrue(_event_is_proven_safe_manual_recovery(event))

    def test_safe_manual_recovery_rejects_completed_or_telegram_touched_shapes(self):
        base_payload = {
            "stage": "manual_recovery_required",
            "result_status": "manual_recovery_required",
            "manual_recovery_required": True,
            "manual_recovery_reason": "automatic_retry_not_safe_or_exhausted",
            "same_day_existing_event_status": "failed",
            "success": False,
        }
        mutations = (
            {"same_day_existing_event_status": "completed"},
            {"manual_recovery_reason": "skipped_same_day_existing_completed_event"},
            {"success": True},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                payload = {**base_payload, **mutation}
                event = SimpleNamespace(
                    status="failed",
                    last_error=f"{COVERAGE_ERROR_PREFIX}: coverage_status=partial",
                    payload=payload,
                )
                self.assertFalse(_event_is_proven_safe_manual_recovery(event))

        telegram_failure = SimpleNamespace(
            status="failed",
            last_error="telegram_send_failed",
            payload=base_payload,
        )
        self.assertFalse(_event_is_proven_safe_manual_recovery(telegram_failure))

    def verify(self, *, ready=None, database=None, reports=None):
        return verify_preflight(
            report_dates=DATES,
            ready=ready or ready_payload(),
            database=database or database_payload(),
            dry_runs=reports or dry_runs(),
            today=date(2026, 7, 22),
        )

    def test_exact_incident_shape_passes_with_only_redacted_counts(self):
        result = self.verify()
        self.assertEqual(
            result,
            {
                "status": "ready",
                "dates_count": 2,
                "dry_run_count": 2,
                "blocker_count": 3,
                "values_redacted": True,
            },
        )
        encoded = json.dumps(result)
        self.assertNotIn("chat", encoded.casefold())
        self.assertNotIn("kiz", encoded.casefold())

    def test_unrelated_blocker_blocks_recovery(self):
        database = database_payload()
        database["unrelated_blocker_count"] = 1
        with self.assertRaises(RecoveryPreflightError):
            self.verify(database=database)

    def test_wrong_due_date_blocks_recovery(self):
        ready = ready_payload()
        ready["payload"]["daily_report"]["due_date"] = "2026-07-20"
        with self.assertRaises(RecoveryPreflightError):
            self.verify(ready=ready)

    def test_hot_path_error_count_must_equal_exact_blocker_count(self):
        ready = ready_payload()
        ready["payload"]["queue"]["hot_path_error_count"] = 2
        with self.assertRaises(RecoveryPreflightError):
            self.verify(ready=ready)

    def test_any_success_registry_active_or_ambiguous_state_blocks(self):
        for field in (
            "success_count",
            "registry_count",
            "active_count",
            "ambiguous_count",
            "unrelated_blocker_count",
        ):
            with self.subTest(field=field):
                database = database_payload()
                database[field] = 1
                with self.assertRaises(RecoveryPreflightError):
                    self.verify(database=database)

    def test_target_and_safe_blocker_counts_must_match_exact_runtime_blockers(self):
        for field in ("target_daily_failure_count", "safe_target_blocker_count"):
            with self.subTest(field=field):
                database = database_payload()
                database[field] = 2
                with self.assertRaises(RecoveryPreflightError):
                    self.verify(database=database)

    def test_both_candidate_dry_runs_must_be_send_ready_and_redacted(self):
        for mutation in ("blocked", "extra_field", "wrong_date", "zero_workbook"):
            with self.subTest(mutation=mutation):
                reports = dry_runs()
                if mutation == "blocked":
                    reports[0]["status"] = "blocked"
                elif mutation == "extra_field":
                    reports[0]["chat_id"] = "forbidden"
                elif mutation == "wrong_date":
                    reports[1]["report_date"] = "2026-07-20"
                else:
                    reports[1]["xlsx_bytes"] = 0
                with self.assertRaises(RecoveryPreflightError):
                    self.verify(reports=reports)


class DailyReportRecoveryDeployContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_normal_deploy_is_default_and_recovery_requires_exact_gate(self):
        self.assertIn('DAILY_REPORT_RECOVERY_ENABLED=0', self.script)
        self.assertIn('SEND_EXACTLY_TWO_DAILY_REPORT_CATCHUPS', self.script)
        self.assertIn('[[ "${#DAILY_REPORT_RECOVERY_DATES[@]}" -eq 2 ]]', self.script)
        self.assertIn('DAILY_REPORT_RECOVERY_ENABLED=0', self.workflow)
        self.assertIn('EXACT_DAILY_REPORT_RECOVERY_APPROVAL_REQUIRED', self.workflow)

    def test_workflow_runs_candidate_dry_runs_and_current_backend_inspection_before_mutation(self):
        first_dry_run = self.workflow.index('--report-date "\\$DAILY_REPORT_RECOVERY_DATE_1" --dry-run')
        second_dry_run = self.workflow.index('--report-date "\\$DAILY_REPORT_RECOVERY_DATE_2" --dry-run')
        current_inspect = self.workflow.index(
            '< tools/verify_daily_report_recovery_preflight.py', second_dry_run
        )
        deploy = self.workflow.index('./deploy/vds/deploy_from_git.sh --artifact-manifest')
        self.assertLess(first_dry_run, second_dry_run)
        self.assertLess(second_dry_run, current_inspect)
        self.assertLess(current_inspect, deploy)
        self.assertIn('tools/verify_daily_report_recovery_preflight.py verify', self.workflow)

    def test_failed_deploy_cannot_publish_stale_success_evidence(self):
        cleanup = self.workflow.index(
            'rm -f /tmp/taksklad-server-deploy-evidence.json'
        )
        deploy = self.workflow.index(
            './deploy/vds/deploy_from_git.sh --artifact-manifest'
        )
        self.assertLess(cleanup, deploy)
        self.assertIn('cat "\\$recovery_database" >&2', self.workflow)

    def test_recovery_stops_worker_and_rollback_never_restarts_old_scheduler(self):
        send_function = self.script.split('run_one_daily_report_catchup() {', 1)[1].split('\n}\n', 1)[0]
        replay_function = self.script.split('verify_one_daily_report_catchup_replay() {', 1)[1].split('\n}\n', 1)[0]
        rollback = self.script.split('rollback_runtime() {', 1)[1].split('\n}\n', 1)[0]
        self.assertIn('ensure_telegram_worker_stopped', send_function)
        self.assertIn('ensure_telegram_worker_stopped', replay_function)
        self.assertIn('compose stop -t 45 telegram-worker', rollback)
        self.assertIn('automatic retry=0', rollback)
        self.assertNotIn('check_public_url readiness', rollback.split('if [[ "$DAILY_REPORT_RECOVERY_ENABLED" == "1" ]]', 1)[1].split('else', 1)[0])

    def _run_recovery_harness(self, fail_date=""):
        function = self.script.split('run_daily_report_recovery() {', 1)[1].split('\n}\n', 1)[0]
        with tempfile.TemporaryDirectory() as temp_dir:
            harness = f'''#!/usr/bin/env bash
set -u
DAILY_REPORT_RECOVERY_ENABLED=1
DAILY_REPORT_RECOVERY_DATES=(2026-07-20 2026-07-21)
FAIL_DATE={fail_date!r}
CALLS=()
ensure_telegram_worker_stopped() {{ CALLS+=(stopped); return 0; }}
run_one_daily_report_catchup() {{ CALLS+=("send:$1"); [[ "$1" != "$FAIL_DATE" ]]; }}
verify_one_daily_report_catchup_replay() {{ CALLS+=("replay:$1"); return 0; }}
run_daily_report_recovery() {{{function}
}}
set +e
run_daily_report_recovery
status=$?
set -e
printf 'status=%s calls=%s\n' "$status" "${{CALLS[*]}}"
'''
            return subprocess.run(
                ["bash", "-c", harness],
                cwd=temp_dir,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_exact_send_order_then_exactly_one_noop_replay_each(self):
        completed = self._run_recovery_harness()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(
            "status=0 calls=stopped send:2026-07-20 send:2026-07-21 "
            "replay:2026-07-20 replay:2026-07-21 stopped",
            completed.stdout,
        )
        self.assertEqual(completed.stdout.count("send:"), 2)
        self.assertNotIn("2026-07-22", completed.stdout)

    def test_first_failure_stops_before_second_send_and_any_replay(self):
        completed = self._run_recovery_harness("2026-07-20")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("status=1 calls=stopped send:2026-07-20", completed.stdout)
        self.assertNotIn("send:2026-07-21", completed.stdout)
        self.assertNotIn("replay:", completed.stdout)

    def test_second_failure_stops_before_any_replay(self):
        completed = self._run_recovery_harness("2026-07-21")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(
            "calls=stopped send:2026-07-20 send:2026-07-21",
            completed.stdout,
        )
        self.assertNotIn("replay:", completed.stdout)


if __name__ == "__main__":
    unittest.main()
