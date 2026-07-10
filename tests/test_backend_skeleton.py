import json
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
            "backend/app/access_policy.py",
            "backend/app/audit_identity.py",
            "backend/app/csrf.py",
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
            "backend/migrations/versions/20260623_0003_client_points.py",
            "backend/migrations/versions/20260623_0004_user_password_hash.py",
            "backend/migrations/versions/20260626_0005_logistics_calendar.py",
            "backend/migrations/versions/20260701_0006_representative_contacts.py",
            "backend/migrations/versions/20260701_0007_pending_event_indexes.py",
            "backend/migrations/versions/20260710_0008_pending_event_leases.py",
            "backend/migrations/versions/20260710_0009_import_identity.py",
            "backend/migrations/versions/20260710_0010_data_invariants.py",
            "backend/migrations/versions/20260710_0011_atomic_outbox.py",
            "backend/migrations/versions/20260710_0012_auth_identities.py",
            "backend/migrations/versions/20260710_0013_authenticated_audit_subject.py",
            "docs/database-migrations-runbook.md",
            "deploy/vds/docker-compose.yml",
            "deploy/vds/config-contract.json",
            "deploy/traefik/docker-compose.yml",
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
            "client_points",
            "logistics_calendar_days",
            "representative_contacts",
            "users",
            "audit_log",
        ]:
            self.assertIn(f"create table if not exists {table_name}", schema_sql)

        self.assertIn("constraint uq_kiz_codes_code unique (code)", schema_sql)
        self.assertIn("constraint uq_logistics_calendar_days_service_date unique (service_date)", schema_sql)
        self.assertIn("constraint uq_representative_contacts_normalized_name unique (normalized_name)", schema_sql)
        self.assertNotIn("constraint uq_scan_codes_code unique (code)", schema_sql)
        self.assertIn("sha256 varchar(64) not null unique", schema_sql)
        self.assertIn("password_hash varchar(255)", schema_sql)
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

    def test_sql_bootstrap_and_alembic_migrations_keep_forward_only_contract(self):
        schema_sql = (ROOT_DIR / "backend/sql/001_initial_schema.sql").read_text(encoding="utf-8").lower()
        baseline = (ROOT_DIR / "backend/migrations/versions/20260616_0001_baseline.py").read_text(encoding="utf-8").lower()
        incidents = (ROOT_DIR / "backend/migrations/versions/20260617_0002_incidents.py").read_text(encoding="utf-8").lower()
        client_points = (ROOT_DIR / "backend/migrations/versions/20260623_0003_client_points.py").read_text(encoding="utf-8").lower()
        user_password_hash = (ROOT_DIR / "backend/migrations/versions/20260623_0004_user_password_hash.py").read_text(encoding="utf-8").lower()
        logistics_calendar = (ROOT_DIR / "backend/migrations/versions/20260626_0005_logistics_calendar.py").read_text(encoding="utf-8").lower()
        representative_contacts = (ROOT_DIR / "backend/migrations/versions/20260701_0006_representative_contacts.py").read_text(encoding="utf-8").lower()
        pending_event_indexes = (ROOT_DIR / "backend/migrations/versions/20260701_0007_pending_event_indexes.py").read_text(encoding="utf-8").lower()
        pending_event_leases = (ROOT_DIR / "backend/migrations/versions/20260710_0008_pending_event_leases.py").read_text(encoding="utf-8").lower()
        import_identity = (ROOT_DIR / "backend/migrations/versions/20260710_0009_import_identity.py").read_text(encoding="utf-8").lower()
        data_invariants = (ROOT_DIR / "backend/migrations/versions/20260710_0010_data_invariants.py").read_text(encoding="utf-8").lower()
        atomic_outbox = (ROOT_DIR / "backend/migrations/versions/20260710_0011_atomic_outbox.py").read_text(encoding="utf-8").lower()
        auth_identities = (ROOT_DIR / "backend/migrations/versions/20260710_0012_auth_identities.py").read_text(encoding="utf-8").lower()

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
            self.assertIn(f'"{table_name}"', baseline)
        self.assertIn("create table if not exists client_points", schema_sql)
        self.assertIn("create table if not exists logistics_calendar_days", schema_sql)
        self.assertIn("create table if not exists representative_contacts", schema_sql)
        self.assertIn("idx_logistics_calendar_days_service_date", schema_sql)
        self.assertIn("idx_representative_contacts_normalized_name", schema_sql)

        for index_name in [
            "idx_orders_status_date",
            "idx_scan_codes_code_order_item_id",
            "idx_kiz_movements_kiz_id_occurred_at",
            "uq_pending_events_idempotency_key",
        ]:
            self.assertIn(index_name, schema_sql)
            self.assertIn(index_name, baseline)
        for index_name in [
            "idx_pending_events_status_created_at",
            "idx_pending_events_status_updated_at",
            "idx_pending_events_type_status_created_at",
            "idx_pending_events_type_status_updated_at",
            "idx_pending_events_updated_created_at",
        ]:
            self.assertIn(index_name, schema_sql)
            self.assertIn(index_name, pending_event_indexes)

        self.assertIn('"incidents"', incidents)
        self.assertIn("revision = \"20260617_0002\"", incidents)
        self.assertIn("down_revision = \"20260616_0001\"", incidents)
        self.assertIn('"client_points"', client_points)
        self.assertIn("revision = \"20260623_0003\"", client_points)
        self.assertIn("down_revision = \"20260617_0002\"", client_points)
        self.assertIn('"users"', user_password_hash)
        self.assertIn('"password_hash"', user_password_hash)
        self.assertIn("revision = \"20260623_0004\"", user_password_hash)
        self.assertIn("down_revision = \"20260623_0003\"", user_password_hash)
        self.assertIn('"logistics_calendar_days"', logistics_calendar)
        self.assertIn("revision = \"20260626_0005\"", logistics_calendar)
        self.assertIn("down_revision = \"20260623_0004\"", logistics_calendar)
        self.assertIn('"representative_contacts"', representative_contacts)
        self.assertIn("revision = \"20260701_0006\"", representative_contacts)
        self.assertIn("down_revision = \"20260626_0005\"", representative_contacts)
        self.assertIn("pending_events", pending_event_indexes)
        self.assertIn("create index if not exists", pending_event_indexes)
        self.assertIn("revision = \"20260701_0007\"", pending_event_indexes)
        self.assertIn("down_revision = \"20260701_0006\"", pending_event_indexes)
        for column_name in ("available_at", "lease_owner", "lease_expires_at", "completed_at"):
            self.assertIn(column_name, pending_event_leases)
        self.assertIn("idx_pending_events_claim", pending_event_leases)
        self.assertIn("idx_pending_events_lease_expiry", pending_event_leases)
        self.assertIn("revision = \"20260710_0008\"", pending_event_leases)
        self.assertIn("down_revision = \"20260701_0007\"", pending_event_leases)
        for column_name in ("import_order_key", "import_source_order_key", "import_item_key", "source_import_key"):
            self.assertIn(column_name, import_identity)
        self.assertIn("revision = \"20260710_0009\"", import_identity)
        self.assertIn("down_revision = \"20260710_0008\"", import_identity)
        self.assertIn("revision = \"20260710_0010\"", data_invariants)
        self.assertIn("down_revision = \"20260710_0009\"", data_invariants)
        self.assertIn("not valid", data_invariants)
        self.assertIn("validate constraint", data_invariants)
        self.assertIn("create unique index concurrently", data_invariants)
        self.assertIn("baseline migration is irreversible", baseline)
        self.assertIn("incident migration is forward-only", incidents)
        self.assertIn("client points migration is forward-only", client_points)
        self.assertIn("user password migration is forward-only", user_password_hash)
        self.assertIn("logistics calendar migration is forward-only", logistics_calendar)
        self.assertIn("representative contacts migration is forward-only", representative_contacts)
        self.assertIn("pending event queue index migration is forward-only", pending_event_indexes)
        self.assertIn("pending event lease migration is forward-only", pending_event_leases)
        self.assertIn("import identity expand migration is forward-only", import_identity)
        self.assertIn("warehouse invariant migration is forward-only", data_invariants)
        self.assertIn('revision = "20260710_0011"', atomic_outbox)
        self.assertIn('down_revision = "20260710_0010"', atomic_outbox)
        self.assertIn("create index concurrently", atomic_outbox)
        self.assertIn("atomic outbox migration is forward-only", atomic_outbox)
        self.assertIn('revision = "20260710_0012"', auth_identities)
        self.assertIn('down_revision = "20260710_0011"', auth_identities)
        for table_name in ("auth_sessions", "service_principals", "service_principal_tokens"):
            self.assertIn(f'"{table_name}"', auth_identities)
        self.assertIn("auth identity migration is forward-only", auth_identities)

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
        self.assertIn('profiles: ["adminer"]', compose_text)
        self.assertIn("traefik.enable=false", compose_text)
        self.assertNotIn("traefik.http.routers.taksklad-adminer", compose_text)
        self.assertIn("traefik.http.routers.taksklad-backend.rule", compose_text)
        self.assertNotIn("5432:5432", compose_text)
        self.assertNotIn("docker-entrypoint-initdb.d", compose_text)

    def test_traefik_compose_declares_https_gateway(self):
        compose_text = (ROOT_DIR / "deploy/traefik/docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("image: traefik:v3.6", compose_text)
        self.assertIn('DOCKER_API_VERSION: "1.44"', compose_text)
        self.assertIn("--providers.docker=true", compose_text)
        self.assertIn("--entrypoints.websecure.address=:443", compose_text)
        self.assertIn("--certificatesresolvers.letsencrypt.acme.httpchallenge=true", compose_text)

    def test_tracked_config_contract_contains_only_synthetic_test_values(self):
        contract = json.loads((ROOT_DIR / "deploy/vds/config-contract.json").read_text(encoding="utf-8"))

        self.assertEqual(contract["schema"], 1)
        self.assertEqual(contract["compose_test_values"]["TAKSKLAD_ENV"], "test")
        self.assertIn("synthetic", contract["compose_test_values"]["TAKSKLAD_API_TOKEN"])
        self.assertIn("synthetic", contract["compose_test_values"]["POSTGRES_PASSWORD"])
        self.assertNotIn("private_key", json.dumps(contract).lower())

    def test_legacy_sql_bootstrap_is_explicit_and_empty_database_only(self):
        script = (ROOT_DIR / "deploy/vds/apply_schema.sh").read_text(encoding="utf-8")

        self.assertIn("ALLOW_EMPTY_UNVERSIONED_DATABASE_ONLY", script)
        self.assertIn("alembic_version", script)
        self.assertIn("existing application tables", script)


if __name__ == "__main__":
    unittest.main()
