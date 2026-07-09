import os
import unittest

from sqlalchemy import create_engine, inspect

from tests.postgres_support import create_database, drop_database, run_alembic, scalar


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))
CURRENT_HEAD = "20260710_0010"
PREVIOUS_HEAD = "20260710_0009"


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
                    INSERT INTO orders (
                        id, source, external_id, payment_type, client, address, status, raw_payload
                    ) VALUES (
                        '00000000-0000-0000-0000-000000000701', 'synthetic_migration',
                        'synthetic-legacy-order', 'synthetic', 'SYNTHETIC CLIENT',
                        'SYNTHETIC ADDRESS', 'not_completed',
                        '{"order_key":"synthetic-legacy-order"}'::jsonb
                    );
                    INSERT INTO order_items (
                        id, order_id, product, quantity_pieces, quantity_blocks, scanned_blocks,
                        requires_kiz, status, raw_payload
                    ) VALUES (
                        '00000000-0000-0000-0000-000000000702',
                        '00000000-0000-0000-0000-000000000701', 'SYNTHETIC PRODUCT',
                        10, 1, 0, true, 'not_completed',
                        '{"item_key":"synthetic-legacy-item","source_import_id":"synthetic-legacy-row"}'::jsonb
                    )
                """)
        finally:
            engine.dispose()
        run_alembic(url, "upgrade", "head")
        run_alembic(url, "upgrade", "head")

        self.assertEqual(scalar(url, "SELECT version_num FROM alembic_version"), CURRENT_HEAD)
        self.assertEqual(scalar(url, "SELECT count(*) FROM alembic_version"), 1)
        self.assertEqual(scalar(
            url,
            "SELECT count(*) FROM orders WHERE external_id='synthetic-legacy-order' "
            "AND raw_payload->>'order_key'='synthetic-legacy-order' "
            "AND import_order_key IS NULL AND import_source_order_key IS NULL",
        ), 1)
        self.assertEqual(scalar(
            url,
            "SELECT count(*) FROM order_items WHERE raw_payload->>'source_import_id'='synthetic-legacy-row' "
            "AND import_item_key IS NULL AND source_import_key IS NULL AND source_import_id IS NULL",
        ), 1)


if __name__ == "__main__":
    unittest.main()
