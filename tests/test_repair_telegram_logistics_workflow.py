import re
import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "repair-telegram-logistics-orders.yml"


class RepairTelegramLogisticsWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.document = yaml.safe_load(cls.workflow)
        cls.remote_step = next(
            step["run"]
            for step in cls.document["jobs"]["repair"]["steps"]
            if step.get("name") == "Plan, isolate, backup, apply, verify and restore"
        )
        cls.collect_step = next(
            step["run"]
            for step in cls.document["jobs"]["repair"]["steps"]
            if step.get("name") == "Collect counts-only sanitized evidence"
        )
        cls.cleanup_step = next(
            step["run"]
            for step in cls.document["jobs"]["repair"]["steps"]
            if step.get("name") == "Remove remote sanitized evidence"
        )

    def assert_order(self, *needles):
        positions = []
        cursor = -1
        for needle in needles:
            cursor = self.remote_step.find(needle, cursor + 1)
            self.assertNotEqual(cursor, -1, needle)
            positions.append(cursor)
        self.assertEqual(positions, sorted(positions), needles)

    def test_yaml_and_embedded_shell_python_are_syntactically_valid(self):
        yaml.compose(self.workflow)
        for step in self.document["jobs"]["repair"]["steps"]:
            script = step.get("run")
            if not script:
                continue
            completed = subprocess.run(
                ["bash", "-n"],
                input=script,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

        for step in self.document["jobs"]["repair"]["steps"]:
            script = step.get("run") or ""
            if "<<'REMOTE'\n" not in script:
                continue
            remote = script.split("<<'REMOTE'\n", 1)[1].rsplit("\nREMOTE", 1)[0]
            completed = subprocess.run(
                ["bash", "-n"],
                input=remote,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
        remote = self.remote_step.split("<<'REMOTE'\n", 1)[1].rsplit("\nREMOTE", 1)[0]
        snippets = re.findall(r"<<'PY'\n(.*?)\nPY", remote, flags=re.DOTALL)
        self.assertGreaterEqual(len(snippets), 4)
        for snippet in snippets:
            compile(snippet, "<workflow-python>", "exec")
        generated = re.search(r"source = r'''(.*?)'''", snippets[0], flags=re.DOTALL)
        self.assertIsNotNone(generated)
        compile(generated.group(1), "<generated-sanitizer>", "exec")

    def test_exact_main_candidate_and_production_gate(self):
        self.assertIn("permissions:\n  contents: read", self.workflow)
        self.assertIn("concurrency:\n  group: taksklad-production", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)
        self.assertIn("environment: production", self.workflow)
        self.assertIn("timeout-minutes: 45", self.workflow)
        self.assertIn("source_sha:", self.workflow)
        self.assertIn("expected_plan_sha:", self.workflow)
        self.assertIn("ref: ${{ inputs.source_sha }}", self.workflow)
        self.assertIn("fetch-depth: 0", self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)
        self.assertIn("test \"$DISPATCH_REF\" = refs/heads/main", self.workflow)
        self.assertIn("test \"$DISPATCH_SHA\" = \"$SOURCE_SHA\"", self.workflow)
        self.assertIn(
            "test \"$(git rev-parse refs/remotes/origin/main)\" = \"$SOURCE_SHA\"",
            self.workflow,
        )
        self.assertIn("test \"$REPAIR_APPROVAL\" = REPAIR-62-LOGISTICS-2026-07-23", self.workflow)
        self.assertIn('[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]', self.workflow)
        self.assertIn('[[ "$EXPECTED_PLAN_SHA" =~ ^[0-9a-f]{64}$ ]]', self.workflow)
        self.assertIn(
            "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
            self.workflow,
        )

    def test_plan_is_read_only_and_validated_before_exact_writer_stop(self):
        self.assertIn(
            "writer_services=(backend-api telegram-worker skladbot-worker smartup-auto-import-worker)",
            self.remote_step,
        )
        writer_line = next(
            line for line in self.remote_step.splitlines() if line.startswith("writer_services=(")
        )
        self.assertNotIn("frontend", writer_line)
        self.assertNotIn("google-sheets-sync-worker", writer_line)
        self.assertIn('test "${#writer_ids[@]}" -eq 4', self.remote_step)
        self.assertIn('timeout 60s docker stop -t 45 "${writer_ids[@]}"', self.remote_step)
        self.assertIn('payload.get("target_count") != 62', self.remote_step)
        self.assertIn('payload.get("target_items") != 230', self.remote_step)
        self.assertIn('payload.get("source_files") != 2', self.remote_step)
        self.assertIn('payload.get("scan_count") != 560', self.remote_step)
        self.assertIn('payload.get("unique_kiz_count") != 560', self.remote_step)
        self.assertIn('payload.get("safe_to_repair") is not True', self.remote_step)
        self.assertIn('payload.get("conflicts") != 0', self.remote_step)
        self.assertIn('test "$plan_sha" = "$expected_plan_sha"', self.remote_step)
        self.assert_order(
            "writer_services=(backend-api telegram-worker skladbot-worker smartup-auto-import-worker)",
            'repair_env_container_id="${writer_by_service[smartup-auto-import-worker]}"',
            'test "$repair_env_image" = "$backend_image"',
            'docker inspect --format \'{{json .Config.Env}}\' "$repair_env_container_id"',
            'initial_plan_raw="$run_dir/initial-plan.raw.json"',
            "TELEGRAM_LOGISTICS_INITIAL_PLAN_OK",
            "trap repair_exit EXIT",
            "TELEGRAM_LOGISTICS_RUNTIME_BEFORE_CAPTURED",
            "isolation_active=1",
            "stop_exact_writers\n",
            "TELEGRAM_LOGISTICS_WRITERS_STOPPED",
            'drained_plan_raw="$run_dir/drained-plan.raw.json"',
        )

    def test_backup_and_hash_bound_preimage_precede_apply(self):
        self.assertIn("backup_postgres.sh\" --no-prune", self.remote_step)
        self.assertIn("tools/postgres_quiescence_check.py", self.workflow)
        self.assertIn("TELEGRAM_LOGISTICS_DATABASE_NOT_QUIESCENT", self.remote_step)
        self.assertIn('payload.get("other_active_client_transactions") != 0', self.remote_step)
        self.assertIn("TAKSKLAD_BACKUP_RESULT_FILE=\"$backup_raw\"", self.remote_step)
        self.assertIn("TELEGRAM_LOGISTICS_BACKUP_NOT_FRESH", self.remote_step)
        self.assertIn('test ! -e "$preimage_file"', self.remote_step)
        self.assertIn("--preimage-out /repair-state/preimage.json", self.remote_step)
        self.assertIn('test "$(stat -c \'%a\' "$preimage_file")" = 600', self.remote_step)
        self.assertIn('test "$preimage_sha" = "$planned_preimage_sha"', self.remote_step)
        self.assert_order(
            "TELEGRAM_LOGISTICS_WRITERS_STOPPED",
            'drained_plan_raw="$run_dir/drained-plan.raw.json"',
            "TELEGRAM_LOGISTICS_QUIESCENCE_BEFORE_BACKUP_OK",
            'backup_operation_id="$(cat /proc/sys/kernel/random/uuid)"',
            "TELEGRAM_LOGISTICS_BACKUP_OK",
            "TELEGRAM_LOGISTICS_QUIESCENCE_AFTER_BACKUP_OK",
            'test ! -e "$preimage_file"',
            "postcommit_guard=1",
            'run_repair "$run_dir/apply.raw.json"',
            "--apply",
            "--preimage-out /repair-state/preimage.json",
            "TELEGRAM_LOGISTICS_APPLY_OK",
        )

    def test_backend_only_then_two_no_send_verifications_then_restore(self):
        self.assertEqual(self.remote_step.count("--verify --expected-plan-sha"), 2)
        self.assertEqual(self.remote_step.count("--no-send"), 2)
        self.assertIn('payload.get("report_rows") != 230', self.remote_step)
        self.assertIn("-e TAKSKLAD_EXTERNAL_SENDS_DISABLED=1", self.remote_step)
        self.assertIn("sleep 30", self.remote_step)
        self.assertIn("TELEGRAM_LOGISTICS_BACKEND_ONLY_HEALTHY other_writers_stopped=3", self.remote_step)
        self.assert_order(
            "TELEGRAM_LOGISTICS_APPLY_OK",
            'timeout 60s docker start "$backend_id"',
            "TELEGRAM_LOGISTICS_BACKEND_ONLY_HEALTHY",
            'run_repair "$run_dir/verify-1.raw.json"',
            "TELEGRAM_LOGISTICS_VERIFY_1_OK",
            "sleep 30",
            'run_repair "$run_dir/verify-2.raw.json"',
            "TELEGRAM_LOGISTICS_VERIFY_2_OK",
            '"${writer_by_service[telegram-worker]}"',
            'check_public_runtime "${evidence_prefix}-runtime.json"',
            "TELEGRAM_LOGISTICS_RUNTIME_RESTORED",
            "isolation_active=0",
        )
        self.assertIn("https://api.taksklad.uz/health", self.remote_step)
        self.assertIn("https://api.taksklad.uz/version", self.remote_step)
        self.assertNotIn("https://api.taksklad.uz/ready", self.remote_step)
        self.assertIn('test "$health" != healthy', self.remote_step)
        self.assertIn("TELEGRAM_LOGISTICS_PUBLIC_RUNTIME_CHANGED", self.remote_step)
        self.assertIn('check_public_runtime "${evidence_prefix}-runtime.json"', self.remote_step)
        self.assertIn('"${evidence_prefix}-runtime-before.json"', self.remote_step)

    def test_any_postcommit_failure_uses_hash_bound_rollback_before_restore(self):
        self.assertIn("trap repair_exit EXIT", self.remote_step)
        self.assertIn("TELEGRAM_LOGISTICS_APPLY_FAILED_ROLLBACK_REQUIRED", self.remote_step)
        self.assertIn("--rollback", self.remote_step)
        self.assertIn('--approval "$repair_approval"', self.remote_step)
        self.assertIn('--expected-plan-sha "$plan_sha"', self.remote_step)
        self.assertIn("--preimage-file /repair-state/preimage.json", self.remote_step)
        self.assertIn('--expected-preimage-sha "$preimage_sha"', self.remote_step)
        self.assertIn("TELEGRAM_LOGISTICS_ROLLBACK_OK", self.remote_step)
        self.assertIn("TELEGRAM_LOGISTICS_ROLLBACK_FAILED_LEFT_IN_MAINTENANCE", self.remote_step)
        self.assertIn(
            'check_public_runtime "${evidence_prefix}-rollback-runtime.json"',
            self.remote_step,
        )
        rollback_block = self.remote_step[
            self.remote_step.index('run_repair "$run_dir/rollback.raw.json"') :
            self.remote_step.index("trap repair_exit EXIT")
        ]
        self.assertLess(rollback_block.index("--rollback"), rollback_block.index("restore_previous_runtime"))
        self.assertRegex(rollback_block, r"TELEGRAM_LOGISTICS_ROLLBACK_OK[\s\S]*?exit 1")
        self.assert_order(
            "trap repair_exit EXIT",
            "postcommit_guard=1",
            'run_repair "$run_dir/apply.raw.json"',
        )

    def test_evidence_is_exact_counts_only_and_excludes_sensitive_state(self):
        for field in (
            '"target_count"',
            '"safe_to_repair"',
            '"conflicts"',
            '"plan_sha256"',
            '"preimage_sha256"',
            '"mutations_applied"',
            '"verified_count"',
            '"problem_rows"',
            '"rollback_count"',
        ):
            self.assertIn(field, self.remote_step)
        self.assertIn("set(payload) != schemas[mode]", self.remote_step)
        self.assertIn("os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600", self.remote_step)
        self.assertIn("values_redacted=1", self.remote_step)
        self.assertNotIn("preimage", self.collect_step)
        self.assertNotIn("raw", self.collect_step)
        self.assertNotIn("stderr", self.collect_step)
        self.assertNotIn("backup.stdout", self.collect_step)
        self.assertIn(
            "for name in plan runtime-before drained-plan quiescence-before-backup backup quiescence-after-backup apply verify-1 verify-2 rollback runtime rollback-runtime",
            self.collect_step,
        )
        self.assertIn("test -f \"$evidence_dir/plan.json\"", self.collect_step)
        self.assertIn("if: always()", self.workflow)
        self.assertIn("if-no-files-found: error", self.workflow)
        self.assertIn("retention-days: 14", self.workflow)
        self.assertIn(
            "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
            self.workflow,
        )

    def test_ssh_and_logs_fail_closed(self):
        self.assertIn("VDS_SSH_KNOWN_HOSTS", self.workflow)
        self.assertIn("chmod 600 ~/.ssh/taksklad_telegram_logistics_repair_key", self.workflow)
        self.assertIn("chmod 600 ~/.ssh/known_hosts", self.workflow)
        self.assertNotIn("StrictHostKeyChecking=no", self.workflow)
        self.assertNotIn("set -x", self.workflow)
        self.assertNotRegex(self.workflow, r"\b(?:cat|tee)\s+\"?\$[^\n]*(?:raw|preimage|env_file)")

    def test_remote_evidence_is_removed_by_exact_validated_names_after_collection(self):
        self.assertIn('[[ "$GITHUB_RUN_ID" =~ ^[0-9]+$ ]]', self.cleanup_step)
        self.assertIn('[[ "$GITHUB_RUN_ATTEMPT" =~ ^[0-9]+$ ]]', self.cleanup_step)
        self.assertIn("TELEGRAM_LOGISTICS_EVIDENCE_PREFIX_INVALID", self.cleanup_step)
        self.assertIn(
            "/tmp/taksklad-telegram-logistics-repair-[0-9]*-[0-9]*)",
            self.cleanup_step,
        )
        for name in (
            "plan", "runtime-before", "drained-plan", "quiescence-before-backup",
            "backup", "quiescence-after-backup", "apply", "verify-1", "verify-2",
            "rollback", "runtime", "rollback-runtime",
        ):
            self.assertIn(name, self.cleanup_step)
        self.assertIn('rm -f -- "${prefix}-${name}.json"', self.cleanup_step)
        self.assertNotIn('rm -f -- "${prefix}"*', self.cleanup_step)
        collect = self.workflow.index("- name: Collect counts-only sanitized evidence")
        cleanup = self.workflow.index("- name: Remove remote sanitized evidence")
        upload = self.workflow.index("- name: Upload counts-only sanitized evidence")
        self.assertLess(collect, cleanup)
        self.assertLess(cleanup, upload)
        cleanup_step = next(
            step
            for step in self.document["jobs"]["repair"]["steps"]
            if step.get("name") == "Remove remote sanitized evidence"
        )
        self.assertEqual(cleanup_step.get("if"), "always()")


if __name__ == "__main__":
    unittest.main()
