import ast
import unittest
from pathlib import Path

from tools.google_cutover_repair import build_repair_plan


ROOT = Path(__file__).resolve().parents[1]


class GoogleCutoverRepairWorkflowTests(unittest.TestCase):
    def test_plan_evidence_allowlist_exactly_matches_counts_only_summary(self):
        workflow = (ROOT / ".github/workflows/repair-google-cutover-returns.yml").read_text(
            encoding="utf-8"
        )
        start = workflow.index("          allowed = {")
        end = workflow.index("          }", start) + len("          }")
        allowed = ast.literal_eval(workflow[start:end].split("=", 1)[1].strip())
        summary, _candidates = build_repair_plan([], {}, {})

        self.assertEqual(set(summary), allowed)

    def test_repair_workflow_is_exact_scope_backup_first_and_fail_closed(self):
        workflow = (ROOT / ".github/workflows/repair-google-cutover-returns.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("environment: production", workflow)
        self.assertIn("concurrency:\n  group: taksklad-production", workflow)
        self.assertIn("GOOGLE_CUTOVER_RETURN_REPAIR_APPROVED", workflow)
        self.assertIn("expected_plan_sha:", workflow)
        self.assertIn("GOOGLE_RETURN_REPAIR_EXPECTED_PLAN_SHA_REQUIRED", workflow)
        self.assertIn("GOOGLE_RETURN_REPAIR_EXPECTED_PLAN_SHA_MISMATCH", workflow)
        self.assertIn("test \"$EXPECTED_MISSING_SCANS\" = 7", workflow)
        self.assertIn("test \"$EXPECTED_MISSING_RETURNS\" = 22", workflow)
        self.assertIn("tools/google_cutover_repair.py", workflow)
        self.assertIn("--expected-plan-sha", workflow)
        self.assertIn('plan_rc="\\$?"', workflow)
        self.assertIn("GOOGLE_RETURN_REPAIR_PLAN_COUNTS", workflow)
        for field in (
            "preexisting_anomaly_occurrences",
            "identity_no_strong_id_records",
            "identity_not_found_records",
            "identity_product_quantity_mismatch_records",
            "identity_multiple_records",
            "identity_multiple_unique_scan_owner_records",
            "identity_multiple_unique_row_owner_records",
            "identity_multiple_codes_without_candidate_scan_occurrences",
            "identity_multiple_unique_both_source_ids_records",
            "identity_multiple_unique_source_file_row_records",
            "identity_multiple_single_unique_signal_records",
            "identity_multiple_signal_agreement_records",
            "identity_multiple_signal_conflict_records",
            "identity_order_not_returned_records",
            "target_missing_movement_timestamp_occurrences",
            "target_return_crosses_later_movement_occurrences",
            "target_return_crosses_later_re_outbound_other_item_occurrences",
            "target_return_crosses_later_same_item_movement_occurrences",
            "missing_scan_return_boundary_conflict_occurrences",
            "missing_outbound_occurrences",
            "outbound_owner_mismatch_occurrences",
            "code_owner_conflict_both_items_have_scan_occurrences",
            "code_owner_conflict_neither_item_has_scan_occurrences",
        ):
            self.assertIn(f'"{field}"', workflow)
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
        self.assertLess(
            workflow.index("GOOGLE_RETURN_REPAIR_EXPECTED_PLAN_SHA_MISMATCH"),
            workflow.index("docker stop -t 45"),
        )
        self.assertLess(workflow.index("GOOGLE_RETURN_REPAIR_WRITER_DRAIN_OK"), workflow.index("backup_postgres.sh\" --no-prune"))
        self.assertLess(workflow.index("backup_postgres.sh\" --no-prune"), workflow.index("--apply"))
        self.assertLess(workflow.index("--apply"), workflow.index("GOOGLE_RETURN_REPAIR_FINAL_AUDIT_OK"))
        self.assertLess(workflow.index("repair_committed=1"), workflow.index("run_legacy_script \"\\$run_dir/tools/google_cutover_repair.py\""))
        self.assertLess(workflow.index("GOOGLE_RETURN_REPAIR_SERVER_PUBLIC_RUNTIME_OK"), workflow.rindex("isolation_active=0"))


if __name__ == "__main__":
    unittest.main()
