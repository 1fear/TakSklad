import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

from tools.github_protection_diff import (
    load_json,
    semantic_diff,
    validate_json_schema,
    validate_manifest,
)
from tools.release_artifacts import validate_manifest_shape


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CiCdWorkflowTests(unittest.TestCase):
    def test_historical_production_manifest_remains_valid_for_runtime_rollback(self):
        source_sha = "a" * 40
        digest = f"sha256:{'b' * 64}"
        manifest = {
            "schema_version": 1,
            "authority": "github-sigstore",
            "deployable": True,
            "source_sha": source_sha,
            "acceptance_required": True,
            "images": {
                role: {
                    "name": f"ghcr.io/1fear/taksklad-{role}",
                    "tag": f"sha-{source_sha}",
                    "digest": digest,
                }
                for role in ("backend", "frontend")
            },
            "windows": {
                "version": "1.9.0",
                "artifact_sha256": "c" * 64,
                "dependency_lock_sha256": "d" * 64,
            },
            "database_rollback": {
                "strategy": "retain-current-schema",
                "alembic_downgrade_allowed": False,
            },
            "attestation": {
                "github_identity_verified": True,
                "registry_attestation_verified": True,
            },
        }

        validate_manifest_shape(manifest, local=False)

    def test_deploy_probe_rejects_transport_success_with_invalid_readiness_body(self):
        validator = PROJECT_ROOT / "tools" / "validate_deploy_probe.py"
        invalid = subprocess.run(
            [sys.executable, str(validator), "readiness"],
            input='{"ready": true, "status": "ok"}',
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        valid = subprocess.run(
            [sys.executable, str(validator), "readiness"],
            input='{"ready":true,"status":"degraded","database":{"status":"ok"},"migrations":{"status":"ok","expected_head":"head","current_revision":"head"},"policy":{"mandatory_status":"ok"}}',
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(invalid.returncode, 1)
        self.assertIn("readiness database contract failed", invalid.stderr)
        self.assertEqual(valid.returncode, 0)

        wrong_identity = subprocess.run(
            [
                sys.executable,
                str(validator),
                "health",
                "--expected-sha",
                "a" * 40,
                "--expected-digest",
                f"sha256:{'b' * 64}",
            ],
            input=json.dumps(
                {
                    "status": "ok",
                    "commit_sha": "c" * 40,
                    "image_digest": f"sha256:{'b' * 64}",
                }
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(wrong_identity.returncode, 1)
        self.assertIn("runtime commit SHA differs from verified manifest", wrong_identity.stderr)

    def test_ci_runs_checks_without_production_secrets(self):
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

        self.assertIn("pull_request:", workflow)
        self.assertIn("push:", workflow)
        self.assertIn("branches:", workflow)
        self.assertIn("sudo apt-get install -y python3-tk", workflow)
        self.assertIn("python -m unittest discover -s tests", workflow)
        self.assertIn("python -m compileall -q backend/app backend/migrations tools tests", workflow)
        self.assertIn("python -m alembic -c backend/alembic.ini heads", workflow)
        self.assertIn("./tools/run_postgres_tests.sh migrations", workflow)
        self.assertIn("./tools/run_postgres_tests.sh smoke", workflow)
        self.assertIn("./tools/run_postgres_tests.sh readiness", workflow)
        self.assertIn("tools/render_compose_test_config.py", workflow)
        self.assertIn('bash -n "$script"', workflow)
        self.assertIn('docker compose --env-file "$config_path" -f deploy/vds/docker-compose.yml config --quiet', workflow)
        self.assertNotIn("deploy/vds/.env.example", workflow)
        self.assertIn("npm ci --prefix frontend", workflow)
        self.assertIn("npm --prefix frontend run build", workflow)
        self.assertNotIn("VDS_SSH_KEY", workflow)
        self.assertNotIn("secrets.", workflow)

    def test_release_workflow_builds_each_image_once_and_consumes_exact_digests(self):
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "build-windows-release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("name: Build Immutable Release", workflow)
        self.assertIn("build-container-subjects:", workflow)
        self.assertIn("release-manifest:", workflow)
        self.assertIn("needs: build-windows", workflow)
        self.assertIn("- build-container-subjects", workflow)
        self.assertEqual(workflow.count("uses: docker/build-push-action@"), 2)
        self.assertEqual(workflow.count("id: backend\n"), 1)
        self.assertEqual(workflow.count("id: frontend\n"), 1)
        self.assertIn("backend_digest: ${{ steps.backend.outputs.digest }}", workflow)
        self.assertIn("frontend_digest: ${{ steps.frontend.outputs.digest }}", workflow)
        self.assertIn(
            "BACKEND_DIGEST: ${{ needs.build-container-subjects.outputs.backend_digest }}",
            workflow,
        )
        self.assertIn(
            "FRONTEND_DIGEST: ${{ needs.build-container-subjects.outputs.frontend_digest }}",
            workflow,
        )
        self.assertIn("subject-digest: ${{ steps.backend.outputs.digest }}", workflow)
        self.assertIn("subject-digest: ${{ steps.frontend.outputs.digest }}", workflow)
        self.assertIn('"source_sha": source_sha', workflow)
        self.assertIn('"digest": backend_digest', workflow)
        self.assertIn('"digest": frontend_digest', workflow)
        self.assertIn('"build_on_target": False', workflow)
        self.assertIn('"alembic_downgrade_allowed": False', workflow)
        self.assertGreaterEqual(workflow.count("fetch-depth: 0"), 2)

    def test_production_deploy_is_manual_and_accepts_only_verified_artifact_identity(self):
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "deploy-production.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("environment: production", workflow)
        self.assertIn("concurrency:", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("permissions:", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("VDS_HOST", workflow)
        self.assertIn("VDS_USER", workflow)
        self.assertIn("VDS_SSH_KEY", workflow)
        self.assertIn("VDS_SSH_KNOWN_HOSTS", workflow)
        self.assertIn("deploy/vds/deploy_from_git.sh", workflow)
        self.assertIn("artifact_run_id:", workflow)
        self.assertIn("source_sha:", workflow)
        self.assertIn("manifest_sha256:", workflow)
        self.assertIn("IMMUTABLE_ARTIFACT_RUN_ID_REQUIRED", workflow)
        self.assertIn("IMMUTABLE_SOURCE_SHA_REQUIRED", workflow)
        self.assertIn("IMMUTABLE_RELEASE_MANIFEST_SHA256_REQUIRED", workflow)
        self.assertIn('metadata.get("workflowName") != "Build Immutable Release"', workflow)
        self.assertIn('metadata.get("conclusion") != "success"', workflow)
        self.assertIn("gh attestation verify \"$manifest_path\"", workflow)
        self.assertIn('gh attestation verify "oci://$reference"', workflow)
        self.assertIn('--signer-workflow "$signer_workflow"', workflow)
        self.assertIn('--source-digest "$EXPECTED_SOURCE_SHA"', workflow)
        self.assertIn("RELEASE_MANIFEST_SOURCE_SHA_MISMATCH", workflow)
        self.assertIn("RELEASE_WORKFLOW_SOURCE_SHA_MISMATCH", workflow)
        self.assertIn("SOURCE_BUILD_DEPLOYMENT_FORBIDDEN", workflow)
        self.assertIn("ref: ${{ inputs.source_sha }}", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("taksklad-deploy-control.tar.gz", workflow)
        self.assertIn("PRODUCTION_APPROVAL: READY_FOR_PRODUCTION_DEPLOY", workflow)
        self.assertIn("TAKSKLAD_PRODUCTION_APPROVAL", workflow)
        self.assertIn("DEPLOY_ACCEPTANCE: required", workflow)
        self.assertNotIn("TAKSKLAD_DEPLOY_REF", workflow)
        self.assertNotIn("inputs.ref", workflow)
        self.assertNotIn("inputs.branch", workflow)
        self.assertNotIn("inputs.tag", workflow)
        self.assertNotIn("workflow_run:", workflow)
        self.assertNotIn("default: optional", workflow)
        self.assertNotIn("- optional", workflow)
        self.assertNotIn("- skip", workflow)
        self.assertNotIn("\n  push:", workflow)
        self.assertNotIn("VDS_PASSWORD", workflow)
        self.assertNotIn("secrets.VDS_PASSWORD", workflow)

    def test_vds_deploy_script_is_artifact_only_and_rolls_back_runtime_without_db_downgrade(self):
        script = (PROJECT_ROOT / "deploy" / "vds" / "deploy_from_git.sh").read_text(encoding="utf-8")

        self.assertIn("--artifact-manifest", script)
        self.assertIn("--acceptance", script)
        self.assertIn("--wait", script)
        self.assertIn("READY_FOR_PRODUCTION_DEPLOY", script)
        self.assertIn("tools/release_artifacts.py verify", script)
        self.assertIn("tools/release_artifacts.py emit-shell", script)
        self.assertIn('docker pull "$TAKSKLAD_BACKEND_IMAGE"', script)
        self.assertIn('docker pull "$TAKSKLAD_FRONTEND_IMAGE"', script)
        self.assertIn("./deploy/vds/backup_postgres.sh --no-prune", script)
        self.assertIn("alembic -c alembic.ini upgrade head", script)
        self.assertIn("--no-build --pull never", script)
        self.assertIn("tools/validate_deploy_probe.py", script)
        self.assertIn('--expected-sha "$RELEASE_SOURCE_SHA"', script)
        self.assertIn('--expected-digest "$RELEASE_BACKEND_DIGEST"', script)
        self.assertIn("TAKSKLAD_DEPLOY_URL_RETRY_ATTEMPTS", script)
        self.assertIn("TAKSKLAD_DEPLOY_URL_RETRY_INTERVAL_SECONDS", script)
        self.assertIn("deploy/vds/acceptance_status.sh", script)
        self.assertIn('ACCEPTANCE_MODE="${TAKSKLAD_DEPLOY_ACCEPTANCE:-required}"', script)
        self.assertIn('[[ "$ACCEPTANCE_MODE" == "required" ]] || fail', script)
        self.assertIn("acceptance_status.sh --require-go", script)
        self.assertIn("rollback_runtime", script)
        self.assertIn("PREVIOUS_MANIFEST", script)
        self.assertIn("database schema retained, alembic downgrade=0", script)
        self.assertIn("--acceptance required --wait", script)
        self.assertIn('install -m 600 "$ARTIFACT_MANIFEST" "$temporary_record"', script)
        self.assertNotIn("continuing because acceptance mode is optional", script)
        self.assertNotIn("optional|required|skip", script)
        self.assertNotIn("docker compose build", script)
        self.assertNotIn("compose build", script)
        self.assertNotIn("up -d --build", script)
        self.assertNotIn("git clone", script)
        self.assertNotIn("git fetch", script)
        self.assertNotIn("git checkout", script)
        self.assertNotIn("rsync", script)
        self.assertNotIn("alembic -c alembic.ini downgrade", script)
        self.assertNotIn("compose run --rm --no-deps backend-api alembic downgrade", script)
        self.assertNotIn("git reset --hard", script)

    def test_production_workflow_runs_phase27_preflight_deploy_and_live_gates(self):
        workflow = (PROJECT_ROOT / ".github/workflows/deploy-production.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("tools/collect_phase27_evidence.py", workflow)
        self.assertIn("./deploy/vds/backup_postgres.sh --no-prune", workflow)
        self.assertIn(
            "./tools/production_preflight.sh --read-only --require-current-backup --require-zero-blockers",
            workflow,
        )
        self.assertIn(
            "./deploy/vds/deploy_from_git.sh --artifact-manifest release.json --acceptance required --wait",
            workflow,
        )
        self.assertIn(
            "./tools/live_release_verifier.sh --read-only --same-sha --slo-window",
            workflow,
        )
        self.assertIn("taksklad-phase27-production-evidence", workflow)
        self.assertIn("docker logout ghcr.io", workflow)

    def test_desired_github_protection_is_fail_closed_and_diff_is_read_only(self):
        manifest = json.loads(
            (PROJECT_ROOT / "supply-chain" / "github-protection.json").read_text(encoding="utf-8")
        )
        validated = validate_manifest(manifest)
        self.assertIs(validated["mutation_allowed"], False)
        schema = load_json(PROJECT_ROOT / "supply-chain" / "github-protection.schema.json")
        validate_json_schema(manifest, schema)
        schema_unsafe = copy.deepcopy(manifest)
        schema_unsafe["branch_rulesets"][0]["conditions"]["ref_name"]["unexpected"] = True
        with self.assertRaisesRegex(RuntimeError, "schema unknown fields"):
            validate_json_schema(schema_unsafe, schema)

        result = semantic_diff(validated, {"ruleset": None, "environment": None}, source="test")
        self.assertIs(result["read_only"], True)
        self.assertEqual(result["mutation_count"], 0)
        self.assertGreater(result["pending_count"], 0)
        pending_paths = {item["path"] for item in result["settings"] if item["status"] == "pending"}
        self.assertIn("branch_ruleset.exists", pending_paths)
        self.assertIn("environment.can_admins_bypass", pending_paths)
        self.assertIn("environment.required_reviewers", pending_paths)

        unsafe = copy.deepcopy(manifest)
        unsafe["environments"][0]["can_admins_bypass"] = True
        with self.assertRaisesRegex(RuntimeError, "administrator bypass"):
            validate_manifest(unsafe)

    def test_backup_scripts_use_current_stack_path_and_env_overrides(self):
        backup = (PROJECT_ROOT / "deploy" / "vds" / "backup_postgres.sh").read_text(encoding="utf-8")
        restore = (PROJECT_ROOT / "deploy" / "vds" / "restore_postgres.sh").read_text(encoding="utf-8")
        drill = (PROJECT_ROOT / "deploy" / "vds" / "restore_drill.sh").read_text(encoding="utf-8")
        installer = (PROJECT_ROOT / "deploy" / "vds" / "install_backup_timer.sh").read_text(encoding="utf-8")
        unit = (PROJECT_ROOT / "deploy" / "vds" / "systemd" / "taksklad-postgres-backup.service").read_text(
            encoding="utf-8"
        )

        for script in (backup, restore):
            self.assertIn('ENV_FILE="${TAKSKLAD_ENV_FILE:-$SCRIPT_DIR/.env}"', script)
            self.assertIn('COMPOSE_FILE="${TAKSKLAD_COMPOSE_FILE:-$SCRIPT_DIR/docker-compose.yml}"', script)
            self.assertIn('docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE"', script)

        self.assertIn("tools/dr_recovery.py", drill)
        self.assertIn("--isolated --synthetic-db --assert-invariants", drill)
        self.assertIn("--manifest", drill)
        self.assertNotIn("TAKSKLAD_ENV_FILE", drill)
        self.assertNotIn("docker compose", drill)

        self.assertIn("/opt/stacks/taksklad/app", installer)
        self.assertIn("WorkingDirectory=/opt/stacks/taksklad/app", unit)
        self.assertIn("ExecStart=/opt/stacks/taksklad/app/deploy/vds/backup_postgres.sh", unit)
        self.assertNotIn("WorkingDirectory=/opt/taksklad/app", unit)


if __name__ == "__main__":
    unittest.main()
