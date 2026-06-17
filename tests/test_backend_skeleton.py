import importlib.util
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def load_backend_settings_module():
    module_path = ROOT_DIR / "backend" / "app" / "settings.py"
    spec = importlib.util.spec_from_file_location("backend_settings_for_tests", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BackendSkeletonTests(unittest.TestCase):
    def test_required_backend_files_exist(self):
        required_paths = [
            "backend/Dockerfile",
            "backend/requirements.txt",
            "backend/app/main.py",
            "backend/app/settings.py",
            "backend/app/db.py",
            "backend/app/models.py",
            "backend/app/schemas.py",
            "backend/sql/001_initial_schema.sql",
            "backend/sql/002_kiz_movements.sql",
            "backend/alembic.ini",
            "backend/migrations/env.py",
            "backend/migrations/script.py.mako",
            "backend/migrations/versions/20260616_0001_baseline.py",
            "docs/database-migrations-runbook.md",
            "deploy/vds/docker-compose.yml",
            "deploy/vds/.env.example",
            "deploy/traefik/docker-compose.yml",
            "deploy/traefik/.env.example",
        ]

        for relative_path in required_paths:
            self.assertTrue((ROOT_DIR / relative_path).exists(), relative_path)

    def test_settings_load_from_env_and_mask_database_url(self):
        settings_module = load_backend_settings_module()
        settings = settings_module.load_settings({
            "TAKSKLAD_SERVICE_NAME": "test-service",
            "TAKSKLAD_ENV": "test",
            "DATABASE_URL": "postgresql+psycopg://user:secret@localhost:5432/db",
            "TAKSKLAD_API_TOKEN": "token",
            "TAKSKLAD_CORS_ORIGINS": "https://one.example, https://two.example",
            "TAKSKLAD_TIMEZONE": "Asia/Tashkent",
        })

        self.assertEqual(settings.service_name, "test-service")
        self.assertEqual(settings.environment, "test")
        self.assertTrue(settings.api_auth_enabled)
        self.assertEqual(settings.cors_origins, ("https://one.example", "https://two.example"))
        self.assertEqual(settings.timezone, "Asia/Tashkent")
        self.assertEqual(
            settings_module.mask_secret_url(settings.database_url),
            "postgresql+psycopg://user:***@localhost:5432/db",
        )

    def test_initial_schema_contains_mvp_tables_and_constraints(self):
        schema_sql = (ROOT_DIR / "backend/sql/001_initial_schema.sql").read_text(encoding="utf-8").lower()
        for table_name in [
            "orders",
            "order_items",
            "scan_codes",
            "kiz_codes",
            "kiz_movements",
            "imports",
            "import_files",
            "pending_events",
            "users",
            "audit_log",
        ]:
            self.assertIn(f"create table if not exists {table_name}", schema_sql)

        self.assertIn("constraint uq_kiz_codes_code unique (code)", schema_sql)
        self.assertNotIn("constraint uq_scan_codes_code unique (code)", schema_sql)
        self.assertIn("sha256 varchar(64) not null unique", schema_sql)
        self.assertIn("jsonb", schema_sql)

    def test_alembic_baseline_covers_current_schema_without_secrets(self):
        alembic_ini = (ROOT_DIR / "backend/alembic.ini").read_text(encoding="utf-8")
        env_py = (ROOT_DIR / "backend/migrations/env.py").read_text(encoding="utf-8")
        revision = (ROOT_DIR / "backend/migrations/versions/20260616_0001_baseline.py").read_text(encoding="utf-8")
        runbook = (ROOT_DIR / "docs/database-migrations-runbook.md").read_text(encoding="utf-8")

        self.assertIn("script_location = %(here)s/migrations", alembic_ini)
        self.assertIn("load_settings", env_py)
        self.assertIn("target_metadata = Base.metadata", env_py)
        self.assertNotIn("private_key", alembic_ini.lower())
        for table_name in [
            "orders",
            "order_items",
            "scan_codes",
            "kiz_codes",
            "kiz_movements",
            "pending_events",
            "import_files",
            "audit_log",
        ]:
            self.assertIn(f'"{table_name}"', revision)
        self.assertIn("stamp 20260616_0001", runbook)
        self.assertIn("deploy/vds/apply_schema.sh", runbook)
        self.assertIn("restore a PostgreSQL backup", runbook)

    def test_deploy_runbook_uses_alembic_for_normal_production_upgrades(self):
        runbook = (ROOT_DIR / "docs/deploy-rollback-runbook.md").read_text(encoding="utf-8")
        deploy_section = runbook.split("## 3. Backup", 1)[0]

        self.assertIn("alembic -c alembic.ini upgrade head", deploy_section)
        self.assertIn("docs/database-migrations-runbook.md", deploy_section)
        self.assertIn("curl -fsS https://api.taksklad.uz/ready", deploy_section)
        self.assertNotIn("./deploy/vds/apply_schema.sh", deploy_section)

    def test_compose_declares_core_vds_services_without_public_postgres_port(self):
        compose_text = (ROOT_DIR / "deploy/vds/docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("postgres:", compose_text)
        self.assertIn("backend-api:", compose_text)
        self.assertIn("adminer:", compose_text)
        self.assertIn("traefik.http.routers.taksklad-backend.rule", compose_text)
        self.assertNotIn("5432:5432", compose_text)

    def test_traefik_compose_declares_https_gateway(self):
        compose_text = (ROOT_DIR / "deploy/traefik/docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("image: traefik:v3.6", compose_text)
        self.assertIn('DOCKER_API_VERSION: "1.44"', compose_text)
        self.assertIn("--providers.docker=true", compose_text)
        self.assertIn("--entrypoints.websecure.address=:443", compose_text)
        self.assertIn("--certificatesresolvers.letsencrypt.acme.httpchallenge=true", compose_text)

    def test_env_example_contains_placeholders_not_real_secrets(self):
        env_text = (ROOT_DIR / "deploy/vds/.env.example").read_text(encoding="utf-8")

        self.assertIn("change-me-service-token", env_text)
        self.assertIn("change-me-postgres-password", env_text)
        self.assertNotIn("credentials.json", env_text)
        self.assertNotIn("private_key", env_text)


if __name__ == "__main__":
    unittest.main()
