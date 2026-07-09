import os
import unittest

from sqlalchemy import create_engine, inspect

from tests.postgres_support import create_database, drop_database, run_alembic, scalar


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))
CURRENT_HEAD = "20260701_0007"
PREVIOUS_HEAD = "20260701_0006"


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
        finally:
            engine.dispose()
        self.assertTrue({"orders", "order_items", "pending_events", "kiz_codes"}.issubset(tables))

    def test_previous_head_and_repeated_head_upgrade_are_idempotent(self):
        url = create_database(self.databases[1])

        run_alembic(url, "upgrade", PREVIOUS_HEAD)
        self.assertEqual(scalar(url, "SELECT version_num FROM alembic_version"), PREVIOUS_HEAD)
        run_alembic(url, "upgrade", "head")
        run_alembic(url, "upgrade", "head")

        self.assertEqual(scalar(url, "SELECT version_num FROM alembic_version"), CURRENT_HEAD)
        self.assertEqual(scalar(url, "SELECT count(*) FROM alembic_version"), 1)


if __name__ == "__main__":
    unittest.main()
