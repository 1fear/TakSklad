import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_ROOT = PROJECT_ROOT / ".github" / "workflows"


class ServerReleaseWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = (WORKFLOW_ROOT / "build-server-release.yml").read_text(encoding="utf-8")
        cls.deploy = (WORKFLOW_ROOT / "deploy-server-production.yml").read_text(
            encoding="utf-8"
        )

    def test_build_is_exact_main_sha_after_successful_ci_release_gate(self):
        required = (
            "name: Build Immutable Server Release",
            "source_sha:",
            "ci_run_id:",
            '[[ "$WORKFLOW_REF" == "refs/heads/main" ]]',
            '[[ "$WORKFLOW_SHA" == "$EXPECTED_SOURCE_SHA" ]]',
            "git ls-remote origin refs/heads/main",
            'metadata.get("workflowName") != "CI"',
            'metadata.get("event") != "push"',
            'metadata.get("headBranch") != "main"',
            'job.get("name") == "Release gate"',
            'release_gates[0].get("conclusion") != "success"',
        )
        for needle in required:
            with self.subTest(needle=needle):
                self.assertIn(needle, self.build)

    def test_build_publishes_exactly_two_immutable_oci_subjects(self):
        self.assertEqual(self.build.count("uses: docker/build-push-action@"), 2)
        self.assertEqual(self.build.count("id: backend\n"), 1)
        self.assertEqual(self.build.count("id: frontend\n"), 1)
        self.assertIn(
            "taksklad-backend:sha-${{ steps.source.outputs.sha }}", self.build
        )
        self.assertIn(
            "taksklad-frontend:sha-${{ steps.source.outputs.sha }}", self.build
        )
        self.assertEqual(self.build.count("push-to-registry: true"), 2)
        self.assertIn("IMMUTABLE_IMAGE_TAG_ALREADY_EXISTS", self.build)

    def test_build_manifest_is_server_only_and_freezes_desktop_compatibility(self):
        required = (
            '"schema_version": 1',
            '"release_kind": "server"',
            '"server_release_id": f"server-{source_sha}"',
            '"capabilities": ["returns_auth_canary_v2_exact_identifier"]',
            '"desktop_api_contract": 1',
            '"min_desktop_version": "2.0.49"',
            '"migration_policy": "no_change"',
            '"destructive_migrations_allowed": False',
            '"alembic_downgrade_allowed": False',
            "tools/server_release_artifacts.py verify",
            "--manifest dist/server-release.json --sha \"$SOURCE_SHA\"",
            "subject-path: dist/server-release.json",
            "name: taksklad-server-release-manifest",
        )
        for needle in required:
            with self.subTest(needle=needle):
                self.assertIn(needle, self.build)
        for forbidden in (
            "TakSklad.exe",
            "TakSkladAuth.exe",
            "version.json",
            "build-windows",
            "release_tag",
            "contents: write",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.build)

    def test_deploy_verifies_producer_manifest_ci_and_registry_identity(self):
        required = (
            "name: Deploy Server Production",
            "artifact_run_id:",
            "source_sha:",
            "manifest_sha256:",
            "environment: production",
            "group: taksklad-production",
            'metadata.get("workflowName") != "Build Immutable Server Release"',
            "--name taksklad-server-release-manifest",
            'gh attestation verify "$manifest_path"',
            ".github/workflows/build-server-release.yml",
            "tools/server_release_artifacts.py verify",
            'gh attestation verify "oci://$reference"',
            'job.get("name") == "Release gate"',
        )
        for needle in required:
            with self.subTest(needle=needle):
                self.assertIn(needle, self.deploy)

    def test_deploy_can_promote_attested_main_ancestor_after_main_moves(self):
        self.assertIn('[[ "$WORKFLOW_CONTROL_REF" == "refs/heads/main" ]]', self.deploy)
        self.assertIn('git merge-base --is-ancestor "$EXPECTED_SOURCE_SHA" origin/main', self.deploy)
        self.assertNotIn("WORKFLOW_CONTROL_SOURCE_SHA_MISMATCH", self.deploy)

    def test_deploy_blocks_database_drift_before_runtime_mutation(self):
        required = (
            "Verify no database migration change from current production release",
            "git merge-base --is-ancestor",
            "git diff --quiet",
            "-- backend/migrations",
            "SERVER_RELEASE_DATABASE_MIGRATION_DIFF_FORBIDDEN",
            "PRODUCTION_ALEMBIC_HEAD_DIFFERS_FROM_NO_CHANGE_RELEASE",
            "CANDIDATE_ALEMBIC_HEAD_DIFFERS_FROM_NO_CHANGE_RELEASE",
            'test "\\$RELEASE_DATABASE_MIGRATION_POLICY" = no_change',
        )
        for needle in required:
            with self.subTest(needle=needle):
                self.assertIn(needle, self.deploy)

    def test_deploy_reuses_backup_rollback_acceptance_and_live_gates(self):
        required = (
            "taksklad-server-deploy-control.tar.gz",
            "tools/materialize_deploy_control.py",
            "backup_postgres.sh --no-prune",
            "rollback_after_candidate_failure",
            "run_acceptance",
            "check_public_url readiness",
            "verify_telegram_routing_contract.py",
            "validate_daily_report_config.py",
            "--artifact-manifest server-release.json",
            "--acceptance required --wait",
            "tools/collect_phase27_evidence.py preflight",
            "--output .release-state/server-production-preflight.json",
            "--evidence .release-state/server-production-preflight.json",
            "--read-only --require-current-backup --require-zero-blockers",
            "tools/collect_phase27_evidence.py live",
            "--output .release-state/server-live-release-verification.json",
            "--evidence .release-state/server-live-release-verification.json",
            "--read-only --same-sha --slo-window",
            "PRODUCTION_APPROVAL: READY_FOR_PRODUCTION_DEPLOY",
            'export TAKSKLAD_SERVER_RELEASE_ID="\\$RELEASE_SERVER_RELEASE_ID"',
            'export TAKSKLAD_DESKTOP_API_CONTRACT="\\$RELEASE_DESKTOP_API_CONTRACT"',
            '"server_release_id", "desktop_api_contract"',
        )
        for needle in required:
            with self.subTest(needle=needle):
                self.assertIn(needle, self.deploy)

    def test_deploy_cannot_mutate_desktop_update_channel(self):
        for forbidden in (
            "TakSklad.exe",
            "TakSkladAuth.exe",
            "version.json",
            "build-windows-release.yml",
            "inputs.ref",
            "inputs.branch",
            "inputs.tag",
            "TAKSKLAD_DEPLOY_REF",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.deploy)

    def test_exact_sha_control_plane_contains_server_manifest_verifier(self):
        materializer = (PROJECT_ROOT / "tools" / "materialize_deploy_control.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"tools/server_release_artifacts.py"', materializer)

    def test_remote_server_candidate_does_not_require_git_checkout(self):
        deploy_script = (PROJECT_ROOT / "deploy/vds/deploy_from_git.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("verify_candidate_release_manifest", deploy_script)
        self.assertIn('if [[ "$verifier" == "tools/server_release_artifacts.py" ]]', deploy_script)
        self.assertIn('--manifest "$manifest_path"', deploy_script)


if __name__ == "__main__":
    unittest.main()
