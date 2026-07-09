import os
import unittest

from sqlalchemy import create_engine, inspect

from tests.postgres_support import create_database, drop_database, run_alembic, scalar


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))
CURRENT_HEAD = "20260710_0008"
PREVIOUS_HEAD = "20260701_0007"


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresMigrationTests(unittest.TestCase):
    databases = ("taksklad_phase2_empty", "taksklad_phase2_previous")

    @classmethod
    def tearDownClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        for name in cls.databases:
            drop_database(name)

    def test_empty_database_upgrades_to_exactly_one_current_head(self):
        url = create_database(self.databases[0])

        run_alembic(url, "upgrade", "head")

        self.assertEqual(scalar(url, "SELECT version_num FROM alembic_version"), CURRENT_HEAD)
        self.assertEqual(scalar(url, "SELECT count(*) FROM alembic_version"), 1)
        engine = create_engine(url)
        try:
            tables = set(inspect(engine).get_table_names())
            pending_columns = {column["name"] for column in inspect(engine).get_columns("pending_events")}
            pending_indexes = {index["name"] for index in inspect(engine).get_indexes("pending_events")}
        finally:
            engine.dispose()
        self.assertTrue({"orders", "order_items", "pending_events", "kiz_codes"}.issubset(tables))
        self.assertTrue({"available_at", "lease_owner", "lease_expires_at", "completed_at"}.issubset(pending_columns))
        self.assertTrue({"idx_pending_events_claim", "idx_pending_events_lease_expiry"}.issubset(pending_indexes))

    def test_previous_head_and_repeated_head_upgrade_are_idempotent(self):
        url = create_database(self.databases[1])

        run_alembic(url, "upgrade", PREVIOUS_HEAD)
        self.assertEqual(scalar(url, "SELECT version_num FROM alembic_version"), PREVIOUS_HEAD)
        engine = create_engine(url)
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("""
                    INSERT INTO pending_events (
                        id, event_type, status, attempts, payload, created_at, updated_at
                    ) VALUES
                        (gen_random_uuid(), 'migration_pending', 'pending', 0,
                         '{"next_attempt_at":"2030-01-01T00:00:00+00:00"}'::jsonb, now(), now()),
                        (gen_random_uuid(), 'migration_processing', 'processing', 1,
                         '{}'::jsonb, now(), now())
                """)
        finally:
            engine.dispose()
        run_alembic(url, "upgrade", "head")
        run_alembic(url, "upgrade", "head")

        self.assertEqual(scalar(url, "SELECT version_num FROM alembic_version"), CURRENT_HEAD)
        self.assertEqual(scalar(url, "SELECT count(*) FROM alembic_version"), 1)
        self.assertEqual(scalar(
            url,
            "SELECT count(*) FROM pending_events WHERE event_type = 'migration_pending' AND available_at = '2030-01-01T00:00:00+00:00'::timestamptz",
        ), 1)
        self.assertEqual(scalar(
            url,
            "SELECT count(*) FROM pending_events WHERE event_type = 'migration_processing' AND lease_owner = 'legacy-expired' AND lease_expires_at IS NOT NULL",
        ), 1)


if __name__ == "__main__":
    unittest.main()
