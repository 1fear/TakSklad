import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class GoogleCutoverRepairWorkflowTests(unittest.TestCase):
    def test_repair_workflow_is_exact_scope_backup_first_and_fail_closed(self):
        workflow = (ROOT / ".github/workflows/repair-google-cutover-returns.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("environment: production", workflow)
        self.assertIn("concurrency:\n  group: taksklad-production", workflow)
        self.assertIn("GOOGLE_CUTOVER_RETURN_REPAIR_APPROVED", workflow)
        self.assertIn("test \"$EXPECTED_MISSING_SCANS\" = 7", workflow)
        self.assertIn("test \"$EXPECTED_MISSING_RETURNS\" = 22", workflow)
        self.assertIn("tools/google_cutover_repair.py", workflow)
        self.assertIn("--expected-plan-sha", workflow)
        self.assertIn('plan_rc="\\$?"', workflow)
        self.assertIn("GOOGLE_RETURN_REPAIR_PLAN_COUNTS", workflow)
        self.assertIn("evidence = {key: payload[key] for key in sorted(allowed)", workflow)
        self.assertIn("os.O_EXCL, 0o600", workflow)
        self.assertNotIn('install -m 600 "\\$run_dir/repair-plan.json" /tmp/taksklad-google-return-repair-plan.json', workflow)
        self.assertLess(workflow.index("if set(payload) - allowed"), workflow.index("os.O_EXCL, 0o600"))
        self.assertLess(workflow.index("os.O_EXCL, 0o600"), workflow.index("GOOGLE_RETURN_REPAIR_PLAN_BLOCKED"))
        self.assertIn('test "\\${#backend_ids[@]}" -eq 1', workflow)
        self.assertIn("service_names=(backend-api frontend telegram-worker", workflow)
        self.assertIn("run_legacy_script", workflow)
        self.assertIn("GOOGLE_RETURN_REPAIR_WRITER_DRAIN_OK", workflow)
        self.assertIn("backup_postgres.sh\" --no-prune", workflow)
        self.assertIn("GOOGLE_RETURN_REPAIR_FINAL_AUDIT_OK", workflow)
        self.assertIn("payload.get(\"blockers\") != 0", workflow)
        self.assertIn("restore_legacy_runtime", workflow)
        self.assertIn("stop_legacy_runtime", workflow)
        self.assertIn("trap cleanup_legacy_env EXIT", workflow)
        self.assertIn("GOOGLE_RETURN_REPAIR_POST_COMMIT_FAILURE_LEFT_IN_MAINTENANCE", workflow)
        self.assertIn("GOOGLE_RETURN_REPAIR_PUBLIC_RUNTIME_OK", workflow)
        self.assertIn("taksklad-google-return-repair-evidence", workflow)
        self.assertLess(workflow.index("--plan"), workflow.index("docker stop -t 45"))
        self.assertLess(workflow.index("GOOGLE_RETURN_REPAIR_WRITER_DRAIN_OK"), workflow.index("backup_postgres.sh\" --no-prune"))
        self.assertLess(workflow.index("backup_postgres.sh\" --no-prune"), workflow.index("--apply"))
        self.assertLess(workflow.index("--apply"), workflow.index("GOOGLE_RETURN_REPAIR_FINAL_AUDIT_OK"))
        self.assertLess(workflow.index("repair_committed=1"), workflow.index("run_legacy_script \"\\$run_dir/tools/google_cutover_repair.py\""))
        self.assertLess(workflow.index("GOOGLE_RETURN_REPAIR_SERVER_PUBLIC_RUNTIME_OK"), workflow.rindex("isolation_active=0"))


if __name__ == "__main__":
    unittest.main()
