from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CiCdWorkflowTests(unittest.TestCase):
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
        self.assertIn("tools/render_compose_test_config.py", workflow)
        self.assertIn('bash -n "$script"', workflow)
        self.assertIn('docker compose --env-file "$config_path" -f deploy/vds/docker-compose.yml config --quiet', workflow)
        self.assertNotIn("deploy/vds/.env.example", workflow)
        self.assertIn("npm ci --prefix frontend", workflow)
        self.assertIn("npm --prefix frontend run build", workflow)
        self.assertNotIn("VDS_SSH_KEY", workflow)
        self.assertNotIn("secrets.", workflow)

    def test_production_deploy_is_manual_and_uses_github_environment(self):
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
        self.assertIn("TAKSKLAD_DEPLOY_REF", workflow)
        self.assertIn("TAKSKLAD_DEPLOY_ACCEPTANCE", workflow)
        self.assertNotIn("\n  push:", workflow)
        self.assertNotIn("password", workflow.lower())

    def test_vds_deploy_script_keeps_backup_migration_and_verification_gates(self):
        script = (PROJECT_ROOT / "deploy" / "vds" / "deploy_from_git.sh").read_text(encoding="utf-8")

        self.assertIn("git status --short --untracked-files=no", script)
        self.assertIn("tracked worktree changes must be resolved", script)
        self.assertIn("restore_point", script)
        self.assertIn("--exclude '.env'", script)
        self.assertIn("--exclude '.env.*'", script)
        self.assertIn("TAKSKLAD_DEPLOY_REMOTE_URL", script)
        self.assertIn("sync_ref_from_temporary_checkout", script)
        self.assertIn("App dir is not a git checkout", script)
        self.assertIn("git clone --no-checkout", script)
        self.assertIn("rsync -a --delete", script)
        self.assertIn("--exclude 'outputs'", script)
        self.assertIn("--exclude 'backups'", script)
        self.assertIn("--exclude 'node_modules'", script)
        self.assertIn("--exclude 'dist'", script)
        self.assertIn("./deploy/vds/backup_postgres.sh", script)
        self.assertIn("docker compose --env-file \"$ENV_FILE\" -f \"$COMPOSE_FILE\" build backend-api", script)
        self.assertIn("alembic -c alembic.ini upgrade head", script)
        self.assertIn("docker compose --env-file \"$ENV_FILE\" -f \"$COMPOSE_FILE\" up -d --build", script)
        self.assertIn("curl -fsS \"$url\"", script)
        self.assertIn("TAKSKLAD_DEPLOY_URL_RETRY_ATTEMPTS", script)
        self.assertIn("TAKSKLAD_DEPLOY_URL_RETRY_INTERVAL_SECONDS", script)
        self.assertIn("check_public_url \"health\" \"$HEALTH_URL\"", script)
        self.assertIn("check_public_url \"readiness\" \"$READY_URL\"", script)
        self.assertIn("deploy/vds/acceptance_status.sh", script)
        self.assertIn("acceptance_status.sh reported no-go; continuing because acceptance mode is optional", script)
        self.assertIn("[[ \"$ACCEPTANCE_MODE\" == \"required\" ]] && fail \"acceptance_status.sh failed\"", script)
        self.assertIn("grep -Ei 'ERROR|CRITICAL|Traceback|Exception|panic'", script)
        self.assertNotIn("git reset --hard", script)
        self.assertNotIn(".env.example\" ]] ||", script)

    def test_backup_scripts_use_current_stack_path_and_env_overrides(self):
        backup = (PROJECT_ROOT / "deploy" / "vds" / "backup_postgres.sh").read_text(encoding="utf-8")
        restore = (PROJECT_ROOT / "deploy" / "vds" / "restore_postgres.sh").read_text(encoding="utf-8")
        drill = (PROJECT_ROOT / "deploy" / "vds" / "restore_drill.sh").read_text(encoding="utf-8")
        installer = (PROJECT_ROOT / "deploy" / "vds" / "install_backup_timer.sh").read_text(encoding="utf-8")
        unit = (PROJECT_ROOT / "deploy" / "vds" / "systemd" / "taksklad-postgres-backup.service").read_text(
            encoding="utf-8"
        )

        for script in (backup, restore, drill):
            self.assertIn('ENV_FILE="${TAKSKLAD_ENV_FILE:-$SCRIPT_DIR/.env}"', script)
            self.assertIn('COMPOSE_FILE="${TAKSKLAD_COMPOSE_FILE:-$SCRIPT_DIR/docker-compose.yml}"', script)
            self.assertIn('docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE"', script)

        self.assertIn("/opt/stacks/taksklad/app", installer)
        self.assertIn("WorkingDirectory=/opt/stacks/taksklad/app", unit)
        self.assertIn("ExecStart=/opt/stacks/taksklad/app/deploy/vds/backup_postgres.sh", unit)
        self.assertNotIn("WorkingDirectory=/opt/taksklad/app", unit)


if __name__ == "__main__":
    unittest.main()
