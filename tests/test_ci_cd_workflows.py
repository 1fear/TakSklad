import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

from tools.github_protection_diff import (
    load_json,
    semantic_diff,
    validate_json_schema,
    validate_manifest,
)
from backend.app.telegram_routing_contract import (
    ROUTING_IDENTITY_ANCHOR_ENV,
    canonical_route_identity_sha256,
)
from tools.materialize_deploy_control import (
    DEPLOY_CONTROL_PATHS,
    MaterializationError,
    materialize,
)
from tools.release_artifacts import validate_manifest_shape


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def deploy_control_paths(workflow):
    if "tools/materialize_deploy_control.py" not in workflow:
        raise AssertionError("workflow does not use the exact-SHA control materializer")
    return list(DEPLOY_CONTROL_PATHS)


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
        self.assertIn("./tools/run_postgres_tests.sh skladbot-nonlease-concurrency", workflow)
        self.assertIn("tools/render_compose_test_config.py", workflow)
        self.assertIn('bash -n "$script"', workflow)
        self.assertIn('docker compose --env-file "$config_path" -f deploy/vds/docker-compose.yml config --quiet', workflow)
        self.assertNotIn("deploy/vds/.env.example", workflow)
        self.assertIn("npm ci --prefix frontend", workflow)
        self.assertIn("npm --prefix frontend run build", workflow)
        self.assertNotIn("VDS_SSH_KEY", workflow)
        self.assertNotIn("secrets.", workflow)

    def test_postgres_gate_wires_skladbot_nonlease_concurrency_into_ci_and_all(self):
        script = (PROJECT_ROOT / "tools" / "run_postgres_tests.sh").read_text(encoding="utf-8")
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        all_mode = script.split("  all)", 1)[1].split("    ;;", 1)[0]

        self.assertIn("skladbot-nonlease-concurrency)", script)
        self.assertIn(
            'TEST_MODULE="tests.test_postgres_skladbot_nonlease_concurrency"',
            script,
        )
        self.assertIn("tests.test_postgres_skladbot_nonlease_concurrency", all_mode)
        self.assertIn(
            "./tools/run_postgres_tests.sh skladbot-nonlease-concurrency",
            workflow,
        )

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
        routing_tool = (
            PROJECT_ROOT / "tools" / "prepare_notification_routing_env.py"
        ).read_text(encoding="utf-8")
        control_paths = set(deploy_control_paths(workflow))

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
        self.assertIn("ServerAliveInterval=30", workflow)
        self.assertIn("ServerAliveCountMax=10", workflow)
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
        self.assertIn("DEPLOY_CONTROL_SHA: ${{ inputs.source_sha }}", workflow)
        self.assertNotIn("DEPLOY_CONTROL_SHA: ${{ github.sha }}", workflow)
        self.assertIn('[[ "$WORKFLOW_CONTROL_SHA" == "$EXPECTED_SOURCE_SHA" ]]', workflow)
        self.assertIn("WORKFLOW_CONTROL_SOURCE_SHA_MISMATCH", workflow)
        self.assertIn("def live_runtime_invariants", workflow)
        self.assertIn("PRODUCTION_APPROVAL: READY_FOR_PRODUCTION_DEPLOY", workflow)
        self.assertIn("TAKSKLAD_PRODUCTION_APPROVAL", workflow)
        self.assertIn("DEPLOY_ACCEPTANCE: required", workflow)
        self.assertIn("PRODUCTION_CONFIG_RECOVERY_OK", workflow)
        self.assertIn("values_redacted=1", workflow)
        self.assertIn("phase27-env-pre-recovery", workflow)
        self.assertIn("tools/prepare_notification_routing_env.py", workflow)
        self.assertIn("tools/verify_telegram_routing_contract.py", workflow)
        self.assertIn("backend/app/telegram_routing_contract.py", control_paths)
        self.assertIn("backend/app/telegram_routing_manifest.json", control_paths)
        self.assertIn("backend/app/telegram_output_contract.py", control_paths)
        self.assertIn("tools/materialize_deploy_control.py", workflow)
        self.assertIn("tools/phase27_routing_candidate_guard.sh", workflow)
        self.assertIn(
            "TELEGRAM_ROUTING_IDENTITY_ANCHOR_SHA256: "
            "${{ secrets.TELEGRAM_ROUTING_IDENTITY_ANCHOR_SHA256 }}",
            workflow,
        )
        self.assertIn("PROTECTED_TELEGRAM_ROUTING_IDENTITY_ANCHOR_REQUIRED", workflow)
        self.assertIn("phase27_candidate_guard_init", workflow)
        self.assertIn("phase27_candidate_guard_create_compose", workflow)
        self.assertIn("phase27_candidate_guard_verify_modes", workflow)
        self.assertIn("phase27_candidate_guard_mark_installed", workflow)
        self.assertIn("phase27_candidate_guard_commit", workflow)
        self.assertLess(
            workflow.index("phase27_candidate_guard_init"),
            workflow.index("python3 tools/prepare_notification_routing_env.py"),
        )
        self.assertGreater(
            workflow.index("phase27_candidate_guard_commit"),
            workflow.index("./tools/live_release_verifier.sh --read-only --same-sha --slo-window"),
        )
        self.assertLess(
            workflow.index("python3 tools/verify_telegram_routing_contract.py"),
            workflow.index('install -m 600 "\\$candidate_env" deploy/vds/.env'),
        )
        self.assertNotIn("--recovery-export-date", workflow)
        self.assertIn("contract=notification-routing-v3", workflow)
        self.assertNotIn("vds-telegram-worker-1", workflow)
        self.assertIn('"SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID": logistics', routing_tool)
        self.assertIn('"TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": admin', routing_tool)
        self.assertIn('"SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID": ""', routing_tool)
        self.assertNotIn('"SMARTUP_AUTO_IMPORT_SAGA_MODE":', routing_tool)
        self.assertNotIn('"SMARTUP_AUTO_IMPORT_PROCESS_SKLADBOT_NOW":', routing_tool)
        self.assertNotIn('"SMARTUP_AUTO_IMPORT_ENABLED":', routing_tool)
        self.assertNotIn('"SKLADBOT_CREATE_REQUESTS_MODE":', routing_tool)
        self.assertNotIn("repaired_personal_logistics_route", routing_tool)
        self.assertNotIn("TAKSKLAD_GOOGLE_TO_BACKEND_SYNC_ENABLED", workflow)
        self.assertIn("export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.", workflow)
        self.assertIn("pull postgres-wal-init postgres", workflow)
        self.assertIn("com.docker.compose.volume=postgres_wal_archive", workflow)
        self.assertIn("chown -R 70:70 /wal-archive", workflow)
        self.assertIn("up -d --no-deps --no-build --pull never --wait --wait-timeout 180 postgres", workflow)
        self.assertIn("select version_num from alembic_version", workflow)
        self.assertIn("alembic -c alembic.ini heads </dev/null", workflow)
        self.assertIn(r'test "\$current_revision" = "\$target_revision"', workflow)
        self.assertIn("phase27_runtime_mutation_started=0", workflow)
        self.assertIn("phase27_record_promoted=0", workflow)
        self.assertIn("phase27_rollback_on_error()", workflow)
        self.assertIn("--rollback-to-current-record", workflow)
        self.assertIn("--promote-current-runtime", workflow)
        self.assertIn("PHASE27_RETRY_RUNTIME_OK", workflow)
        self.assertIn("backend-api frontend telegram-worker skladbot-worker", workflow)
        self.assertIn("for attempt in \\$(seq 1 36)", workflow)
        self.assertIn("backend_health", workflow)
        self.assertIn("frontend_health", workflow)
        self.assertIn("RUNTIME_STATE service=", workflow)
        self.assertIn("Invalid configuration:|configuration invalid:", workflow)
        self.assertIn("HOT_PATH_FAILURE_GROUPS", workflow)
        self.assertIn("unresolved_daily_report_historical", workflow)
        self.assertIn("PHASE27_HISTORICAL_REPORT_RECOVERY_OK", workflow)
        self.assertIn("phase27_historical_daily_report_recovery", workflow)
        self.assertIn("DB_ONLY_RUNTIME_POLICY_OK", workflow)
        self.assertIn("phase27_record_promoted=1", workflow)
        self.assertNotIn("phase27_runtime_preactivated", workflow)
        rollback_trap = workflow.index("trap phase27_rollback_on_error ERR")
        mutation_start = workflow.index("phase27_runtime_mutation_started=1")
        candidate_stop = workflow.index("stop -t 45 backend-api telegram-worker")
        promotion = workflow.index("--promote-current-runtime")
        promotion_recorded = workflow.index("phase27_record_promoted=1")
        self.assertLess(rollback_trap, mutation_start)
        self.assertLess(mutation_start, candidate_stop)
        self.assertLess(candidate_stop, promotion)
        self.assertLess(promotion, promotion_recorded)
        self.assertIn('retired_google_ids="\\$(docker ps -aq', workflow)
        self.assertIn("docker container stop -t 45 \\$retired_google_ids", workflow)
        self.assertIn("docker container rm -f \\$retired_google_ids", workflow)
        self.assertIn("PHASE27_LIVE_SLO_SUMMARY", workflow)
        self.assertIn("PHASE27_DEPLOY_REUSED", workflow)
        self.assertIn("/opt/stacks/taksklad/deployments/current-release.json", workflow)
        self.assertNotIn("google_sync_status", workflow)
        self.assertIn('docker exec -i "\\$backend_id" python - < /tmp/taksklad-phase27-recover.py', workflow)
        self.assertIn("\n          PY\n            chmod 600 /tmp/taksklad-phase27-recover.py", workflow)
        self.assertIn("for attempt in \\$(seq 1 36)", workflow)
        self.assertIn("compose up -d --no-deps --no-build", workflow)
        self.assertIn("backup_postgres.sh --no-prune </dev/null", workflow)
        executable_control_chmod = (
            "chmod 700 deploy/vds/backup_postgres.sh "
            "deploy/vds/acceptance_status.sh"
        )
        readable_invariant_chmod = "chmod 644 tools/check_data_invariants.py"
        self.assertIn(executable_control_chmod, workflow)
        self.assertIn(readable_invariant_chmod, workflow)
        self.assertLess(
            workflow.index(executable_control_chmod),
            workflow.index("./deploy/vds/backup_postgres.sh --no-prune"),
        )
        self.assertLess(
            workflow.index(readable_invariant_chmod),
            workflow.index("python3 tools/collect_phase27_evidence.py"),
        )
        self.assertIn(
            "deploy_from_git.sh --artifact-manifest release.json --acceptance required --wait \\$legacy_canary_arg </dev/null",
            workflow,
        )
        self.assertIn("live_release_verifier.sh --read-only --same-sha --slo-window </dev/null", workflow)
        self.assertIn("tools/credentialed_returns_canary.py", workflow)
        self.assertIn("tools/validate_auth_canary_token_file.py", workflow)
        self.assertIn("--current-auth-canary-only", workflow)
        self.assertNotIn("TAKSKLAD_AUTH_CANARY_TOKEN", workflow)
        self.assertNotIn("--token-env", workflow)
        self.assertNotIn("--token ", workflow)
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

    def test_deploy_control_artifact_imports_and_verifies_from_clean_base(self):
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "deploy-production.yml").read_text(
            encoding="utf-8"
        )
        members = deploy_control_paths(workflow)
        required = {
            "backend/app/daily_report_config.py",
            "backend/app/telegram_output_contract.py",
            "backend/app/telegram_routing_contract.py",
            "backend/app/telegram_routing_manifest.json",
            "tools/prepare_notification_routing_env.py",
            "tools/verify_telegram_routing_contract.py",
        }
        self.assertTrue(required.issubset(set(members)))

        candidate = {
            "TAKSKLAD_ENV": "production",
            "TAKSKLAD_TIMEZONE": "Asia/Tashkent",
            "TELEGRAM_BOT_TOKEN": "synthetic-token",
            "TELEGRAM_ALLOWED_CHAT_IDS": "-1002001,-1002002,1001",
            "TELEGRAM_ADMIN_CHAT_IDS": "1001",
            "SKLADBOT_DAILY_REPORT_ENABLED": "true",
            "SKLADBOT_DAILY_REPORT_CHAT_IDS": "-1002001",
            "SKLADBOT_API_TOKENS": "synthetic-skladbot-token",
            "TAKSKLAD_AUTOMATION_ALERT_CHAT_ID": "1001",
            "SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID": "-1002001",
            "SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID": "-1002002",
            "SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID": "",
            "SMARTUP_AUTO_IMPORT_TIMES": "12:00,15:00,17:50",
            "SMARTUP_AUTO_IMPORT_FINAL_TIME": "17:50",
            "SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME": "17:50",
            "SKLADBOT_DAILY_REPORT_HOUR": "22",
            "SKLADBOT_DAILY_REPORT_MINUTE": "0",
        }
        identity_anchor = canonical_route_identity_sha256(
            candidate["SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID"],
            candidate["SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID"],
            candidate["TAKSKLAD_AUTOMATION_ALERT_CHAT_ID"],
        )
        compose = {
            "services": {
                "telegram-worker": {"environment": dict(candidate)},
                "smartup-auto-import-worker": {
                    "environment": {
                        key: candidate[key]
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
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            archive = temp_root / "control.tar.gz"
            extracted = temp_root / "clean-base"
            extracted.mkdir(mode=0o700)
            with tarfile.open(archive, "w:gz") as bundle:
                for member in members:
                    bundle.add(PROJECT_ROOT / member, arcname=member)
            with tarfile.open(archive, "r:gz") as bundle:
                bundle.extractall(extracted, filter="data")
            env_path = extracted / "candidate.env"
            compose_path = extracted / "candidate-compose.json"
            env_path.write_text(
                "".join(f"{key}={value}\n" for key, value in candidate.items()),
                encoding="utf-8",
            )
            compose_path.write_text(json.dumps(compose), encoding="utf-8")
            os.chmod(env_path, 0o600)
            os.chmod(compose_path, 0o600)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(extracted / "tools" / "verify_telegram_routing_contract.py"),
                    "--env-path", str(env_path),
                    "--compose-config-json", str(compose_path),
                    "--json",
                ],
                cwd=extracted,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, ROUTING_IDENTITY_ANCHOR_ENV: identity_anchor},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertNotIn("ModuleNotFoundError", completed.stdout + completed.stderr)
            for raw_value in ("-1002001", "-1002002", "1001", "synthetic-token"):
                self.assertNotIn(raw_value, completed.stdout + completed.stderr)

            (extracted / "backend" / "app" / "telegram_routing_manifest.json").unlink()
            missing_manifest = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(extracted / "tools" / "verify_telegram_routing_contract.py"),
                    "--env-path", str(env_path),
                    "--compose-config-json", str(compose_path),
                    "--json",
                ],
                cwd=extracted,
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, ROUTING_IDENTITY_ANCHOR_ENV: identity_anchor},
            )
            self.assertNotEqual(missing_manifest.returncode, 0)
            self.assertIn("manifest is unreadable", missing_manifest.stderr)

    def test_deploy_control_materializer_uses_source_sha_for_entire_closure(self):
        materializer_source = (
            PROJECT_ROOT / "tools" / "materialize_deploy_control.py"
        ).read_bytes()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            source_sha = "a" * 40
            github_sha = "b" * 40
            missing_sha = "c" * 40
            source_objects = {}
            github_objects = {}
            for relative in DEPLOY_CONTROL_PATHS:
                payload = (
                    materializer_source
                    if relative == "tools/materialize_deploy_control.py"
                    else f"source-sha:{relative}\n".encode("utf-8")
                )
                source_objects[relative] = payload
                github_objects[relative] = f"github-sha:{relative}\n".encode("utf-8")
            self.assertNotEqual(github_sha, source_sha)
            missing_relative = DEPLOY_CONTROL_PATHS[-1]
            object_sets = {
                source_sha: source_objects,
                github_sha: github_objects,
                missing_sha: {
                    key: value for key, value in source_objects.items() if key != missing_relative
                },
            }
            calls = []

            def fake_git(_repo_root, *args):
                calls.append(args)
                if args[:2] == ("cat-file", "-e"):
                    sha = args[2].removesuffix("^{commit}")
                    if sha not in object_sets:
                        raise MaterializationError("missing commit")
                    return b""
                if args[:2] != ("cat-file", "blob") or ":" not in args[2]:
                    raise MaterializationError("unexpected git call")
                sha, relative = args[2].split(":", 1)
                try:
                    return object_sets[sha][relative]
                except KeyError as exc:
                    raise MaterializationError("missing blob") from exc

            output = root / "materialized"
            with mock.patch("tools.materialize_deploy_control._git", side_effect=fake_git):
                materialize(repo, source_sha, output)
            actual_paths = {
                path.relative_to(output).as_posix()
                for path in output.rglob("*")
                if path.is_file()
            }
            self.assertEqual(actual_paths, set(DEPLOY_CONTROL_PATHS))
            for relative, payload in source_objects.items():
                self.assertEqual((output / relative).read_bytes(), payload)
            requested_specs = {args[2] for args in calls if args[:2] == ("cat-file", "blob")}
            self.assertEqual(
                requested_specs,
                {f"{source_sha}:{relative}" for relative in DEPLOY_CONTROL_PATHS},
            )
            self.assertFalse(any(github_sha in spec for spec in requested_specs))

            missing_output = root / "missing-output"
            with mock.patch("tools.materialize_deploy_control._git", side_effect=fake_git):
                with self.assertRaises(MaterializationError):
                    materialize(repo, missing_sha, missing_output)

    def test_vds_deploy_script_is_artifact_only_and_rolls_back_runtime_without_db_downgrade(self):
        script = (PROJECT_ROOT / "deploy" / "vds" / "deploy_from_git.sh").read_text(encoding="utf-8")

        self.assertIn("--artifact-manifest", script)
        self.assertIn("--acceptance", script)
        self.assertIn("--wait", script)
        self.assertIn("--promote-current-runtime", script)
        self.assertIn("--rollback-to-current-record", script)
        self.assertIn("only one current-runtime control mode may be selected", script)
        self.assertIn("current runtime identity differs from the candidate manifest", script)
        self.assertIn("Current verified runtime promoted to the exact deployment record.", script)
        self.assertIn("Current deployment record runtime restored and verified.", script)
        self.assertIn("READY_FOR_PRODUCTION_DEPLOY", script)
        self.assertIn('print("tools/release_artifacts.py")', script)
        self.assertIn('print("tools/server_release_artifacts.py")', script)
        self.assertIn("verify_release_manifest", script)
        self.assertIn("emit_release_shell", script)
        self.assertIn('docker pull "$TAKSKLAD_BACKEND_IMAGE"', script)
        self.assertIn('docker pull "$TAKSKLAD_FRONTEND_IMAGE"', script)
        self.assertIn("tools/reconcile_output_permissions.sh", script)
        self.assertIn("tools/validate_daily_report_config.py", script)
        self.assertLess(
            script.index("tools/validate_daily_report_config.py"),
            script.index("./deploy/vds/backup_postgres.sh --no-prune"),
        )
        self.assertIn('TAKSKLAD_OUTPUT_PERMISSIONS_IMAGE="$TAKSKLAD_BACKEND_IMAGE"', script)
        self.assertIn("PHASE22_CHANGE_OUTPUT_OWNER", script)
        self.assertIn("compose stop -t 45", script)
        self.assertIn("python -m app.event_lease_recovery", script)
        self.assertIn("in-flight event leases could not be recovered", script)
        self.assertIn("candidate containers failed to activate", script)
        self.assertIn("./deploy/vds/backup_postgres.sh --no-prune", script)
        self.assertIn("alembic -c alembic.ini upgrade head", script)
        self.assertIn("--no-build --pull never", script)
        self.assertEqual(script.count("compose up -d --no-deps --no-build"), 2)
        self.assertIn("tools/validate_deploy_probe.py", script)
        self.assertIn('--expected-sha "$RELEASE_SOURCE_SHA"', script)
        self.assertIn('--expected-digest "$RELEASE_BACKEND_DIGEST"', script)
        self.assertIn("TAKSKLAD_DEPLOY_URL_RETRY_ATTEMPTS", script)
        self.assertIn("TAKSKLAD_DEPLOY_URL_RETRY_INTERVAL_SECONDS", script)
        self.assertIn("deploy/vds/acceptance_status.sh", script)
        self.assertIn('ACCEPTANCE_MODE="${TAKSKLAD_DEPLOY_ACCEPTANCE:-required}"', script)
        self.assertIn('[[ "$ACCEPTANCE_MODE" == "required" ]] || fail', script)
        self.assertIn("acceptance_status.sh --require-go", script)
        self.assertIn("Traceback \\(most recent call last\\):", script)
        self.assertIn("(ERROR|CRITICAL)", script)
        self.assertNotIn("'ERROR|CRITICAL|Traceback|Exception|panic'", script)
        self.assertIn("rollback_runtime", script)
        self.assertIn("verify_telegram_worker_repair_candidate", script)
        self.assertIn("REPAIR_ONE_TELEGRAM_WORKER_ROLLBACK_MISMATCH", script)
        self.assertIn("PREVIOUS_MANIFEST", script)
        self.assertIn("previous runtime migration head does not match the retained database schema", script)
        self.assertIn('"$database_revision" != "$previous_runtime_revision"', script)
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
        control_paths = set(deploy_control_paths(workflow))
        candidate_guard = (
            PROJECT_ROOT / "tools" / "phase27_routing_candidate_guard.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("tools/collect_phase27_evidence.py", workflow)
        self.assertIn(
            'git cat-file blob "$DEPLOY_CONTROL_SHA:tools/materialize_deploy_control.py"',
            workflow,
        )
        self.assertIn("deploy/vds/deploy_from_git.sh", control_paths)
        self.assertIn("deploy/vds/acceptance_status.sh", control_paths)
        self.assertIn("tools/google_cutover_audit.py", control_paths)
        self.assertIn("previous runtime migration head does not match the retained database schema", workflow)
        self.assertIn("structurally complete forced rollout", workflow)
        self.assertIn(
            "rm -f /tmp/taksklad-google-cutover-audit.json",
            workflow,
        )
        self.assertIn("tools/google_cutover_audit.py", workflow)
        self.assertIn("tools/verify_postgres_only_cutover.py", workflow)
        self.assertIn("tools/verify_postgres_only_cutover.py", control_paths)
        self.assertIn("POSTGRES_ONLY_CUTOVER_REUSED", workflow)
        self.assertIn("GOOGLE_TO_POSTGRES_CUTOVER_STATE_UNVERIFIED", workflow)
        self.assertIn("importlib.util.find_spec('app.google_sheets_sync_worker')", workflow)
        self.assertIn("GOOGLE_TO_POSTGRES_CUTOVER_AUDIT_BLOCKED", workflow)
        self.assertIn('payload.get("blockers") != 0', workflow)
        self.assertLess(
            workflow.index("phase27_candidate_guard_init"),
            workflow.index("< tools/google_cutover_audit.py"),
        )
        self.assertIn("./deploy/vds/backup_postgres.sh --no-prune", workflow)
        self.assertIn(
            "./tools/production_preflight.sh --read-only --require-current-backup --require-zero-blockers",
            workflow,
        )
        self.assertLess(
            workflow.index("--promote-current-runtime"),
            workflow.index('if python3 - "\\$deployment_record" release.json <<\'PY\''),
        )
        self.assertIn("--ready-json .release-state/current-ready.json", workflow)
        self.assertIn("http://127.0.0.1:8000/ready", workflow)
        self.assertIn("except HTTPError as e: r=e", workflow)
        self.assertIn("tools/validate_daily_report_config.py", workflow)
        self.assertIn("backend/app/daily_report_config.py", control_paths)
        self.assertIn("phase27-env-candidate", candidate_guard)
        self.assertIn("PRODUCTION_CONFIG_RECOVERY_ROLLED_BACK", candidate_guard)
        candidate_validation = workflow.index(
            'docker compose --env-file "\\$candidate_env"'
        )
        env_install = workflow.index(
            'install -m 600 "\\$candidate_env" deploy/vds/.env'
        )
        self.assertLess(candidate_validation, env_install)
        self.assertGreater(
            workflow.index("phase27_candidate_guard_commit"),
            workflow.index("docker pull \"\\$RELEASE_BACKEND_IMAGE\""),
        )
        self.assertLess(
            workflow.index("tools/validate_daily_report_config.py"),
            workflow.index("pull postgres-wal-init postgres"),
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
