import ast
import unittest
from pathlib import Path

from tests.test_google_cutover_active_kiz_repair import OBSERVED_AT, make_targets


ROOT = Path(__file__).resolve().parents[1]


class GoogleCutoverActiveKizWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (
            ROOT / ".github/workflows/repair-google-cutover-active-kiz.yml"
        ).read_text(encoding="utf-8")

    def test_plan_allowlist_exactly_matches_counts_only_summary(self):
        marker = self.workflow.index('"identity_hash_mismatch_records"')
        start = self.workflow.rfind("          allowed = {", 0, marker)
        end = self.workflow.index("          }", marker) + len("          }")
        allowed = ast.literal_eval(self.workflow[start:end].split("=", 1)[1].strip())
        diagnostics = {
            "active_records_total": 6,
            "active_missing_item_records": 0,
            "identity_no_strong_id_records": 0,
            "identity_not_unique_records": 0,
            "identity_mapping_mismatch_records": 0,
            "identity_product_quantity_mismatch_records": 0,
            "identity_hash_mismatch_records": 0,
        }
        from tools.google_cutover_active_kiz_repair import build_plan
        summary, _ = build_plan(
            make_targets(), {}, {}, set(), diagnostics,
            observed_at=OBSERVED_AT,
            snapshot_sha="a" * 64,
        )
        self.assertEqual(set(summary), allowed)

    def test_workflow_is_exact_scope_backup_first_and_fail_closed(self):
        workflow = self.workflow
        self.assertIn("environment: production", workflow)
        self.assertIn("ref: main", workflow)
        self.assertIn('test "$DISPATCH_REF" = refs/heads/main', workflow)
        self.assertIn("concurrency:\n  group: taksklad-production", workflow)
        self.assertIn("GOOGLE_CUTOVER_ACTIVE_KIZ_REPAIR_APPROVED", workflow)
        self.assertIn("ACTIVE_KIZ_INITIAL_AUDIT_SCOPE_CHANGED", workflow)
        self.assertIn('"active_codes_missing_backend": 6', workflow)
        self.assertIn("ACTIVE_KIZ_EXPECTED_PLAN_SHA_REQUIRED", workflow)
        self.assertIn("ACTIVE_KIZ_EXPECTED_PLAN_SHA_MISMATCH", workflow)
        self.assertIn("active_missing_unique_codes", workflow)
        self.assertIn("active_missing_unique_item_codes", workflow)
        self.assertIn("docker run --rm -i", workflow)
        self.assertIn("service_names=(backend-api frontend telegram-worker", workflow)
        self.assertIn("ACTIVE_KIZ_WRITER_DRAIN_OK stopped=6", workflow)
        self.assertIn("ACTIVE_KIZ_DRAINED_SNAPSHOT_CHANGED", workflow)
        self.assertIn("ACTIVE_KIZ_DATABASE_QUIESCENT_OK", workflow)
        self.assertIn("ACTIVE_KIZ_POST_BACKUP_QUIESCENCE_OK", workflow)
        self.assertIn("ACTIVE_KIZ_INDEPENDENT_VERIFICATION_OK", workflow)
        self.assertIn('backup_postgres.sh" --no-prune', workflow)
        self.assertIn("ACTIVE_KIZ_POST_WRITE_FAILURE_LEFT_IN_MAINTENANCE", workflow)
        self.assertIn("ACTIVE_KIZ_FINAL_AUDIT_OK blockers=0", workflow)
        self.assertIn('docker start "\\$backend_id"', workflow)
        self.assertIn("other_legacy_writers_stopped=5", workflow)
        self.assertIn("taksklad-active-kiz-repair-evidence", workflow)
        self.assertLess(workflow.index("google_cutover_audit.py"), workflow.index("--plan"))
        self.assertLess(workflow.index("ACTIVE_KIZ_EXPECTED_PLAN_SHA_MISMATCH"), workflow.index("docker stop -t 45"))
        self.assertLess(workflow.index("ACTIVE_KIZ_WRITER_DRAIN_OK"), workflow.index("ACTIVE_KIZ_DATABASE_QUIESCENT_OK"))
        self.assertLess(workflow.index("ACTIVE_KIZ_DATABASE_QUIESCENT_OK"), workflow.index('backup_postgres.sh" --no-prune'))
        self.assertLess(workflow.index('backup_postgres.sh" --no-prune'), workflow.index("--apply"))
        self.assertLess(workflow.index('backup_postgres.sh" --no-prune'), workflow.index("ACTIVE_KIZ_POST_BACKUP_QUIESCENCE_OK"))
        self.assertLess(workflow.index("ACTIVE_KIZ_POST_BACKUP_QUIESCENCE_OK"), workflow.index("--apply"))
        self.assertGreaterEqual(workflow.count("--filter label=com.docker.compose.service=\\$service"), 3)
        self.assertLess(workflow.index("--apply"), workflow.index("--verify"))
        self.assertLess(workflow.index("--verify"), workflow.index("ACTIVE_KIZ_FINAL_AUDIT_OK"))


if __name__ == "__main__":
    unittest.main()
