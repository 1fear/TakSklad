import json
import os
import subprocess
import sys
import threading
import time
import unittest

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.postgres_support import create_database, drop_database, run_alembic, scalar
from tools.check_data_invariants import run_preflight


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresInvariantTests(unittest.TestCase):
    databases = (
        "taksklad_phase8_constraints",
        "taksklad_phase8_violations",
        "taksklad_phase8_timeout",
        "taksklad_phase8_stress",
    )

    @classmethod
    def setUpClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.constrained_url = create_database(cls.databases[0])
        run_alembic(cls.constrained_url, "upgrade", "head")

    @classmethod
    def tearDownClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        for database in cls.databases:
            drop_database(database)

    def test_constraints_reject_invalid_rows_and_keep_legacy_statuses(self):
        engine = create_engine(self.constrained_url, pool_pre_ping=True)
        try:
            with engine.begin() as connection:
                for status in ("not_completed", "completed", "done", "closed", "returned", "archived_no_kiz", "cancelled"):
                    connection.execute(text("""
                        INSERT INTO orders (source, payment_type, client, address, status, raw_payload)
                        VALUES ('synthetic_invariant', 'synthetic', :client, 'SYNTHETIC ADDRESS', :status, '{}'::jsonb)
                    """), {"client": f"SYNTHETIC {status}", "status": status})
                order_id = connection.execute(text("SELECT id FROM orders LIMIT 1")).scalar_one()
                for status in (
                    "not_completed", "completed", "done", "closed", "returned",
                    "removed_from_google_sheet", "archived_no_kiz", "cancelled",
                ):
                    connection.execute(text("""
                        INSERT INTO order_items (
                            order_id, product, quantity_pieces, quantity_blocks, scanned_blocks,
                            requires_kiz, status, raw_payload
                        ) VALUES (:order_id,:product,10,1,0,true,:status,'{}'::jsonb)
                    """), {"order_id": order_id, "product": f"SYNTHETIC {status}", "status": status})
                for status in ("created", "completed", "completed_with_errors", "failed"):
                    connection.execute(text("""
                        INSERT INTO imports (source,status,rows_total,rows_imported,raw_payload)
                        VALUES ('synthetic',:status,1,1,'{}'::jsonb)
                    """), {"status": status})
                for status in (
                    "pending", "failed", "error", "processing", "completed", "blocked",
                    "dead", "cancelled", "active", "waiting_shipment_date", "waiting_date_choice",
                ):
                    connection.execute(text("""
                        INSERT INTO pending_events (event_type,status,attempts,payload)
                        VALUES ('synthetic',:status,0,'{}'::jsonb)
                    """), {"status": status})
            with self.assertRaises(IntegrityError), Session(engine) as session:
                session.execute(text("""
                    INSERT INTO orders (source, payment_type, client, address, status, raw_payload)
                    VALUES ('synthetic_invariant','synthetic','SYNTHETIC BAD','SYNTHETIC','unsupported','{}'::jsonb)
                """))
                session.commit()
            with self.assertRaises(IntegrityError), Session(engine) as session:
                session.execute(text("""
                    INSERT INTO order_items (
                        order_id, product, quantity_pieces, quantity_blocks, scanned_blocks,
                        requires_kiz, status, raw_payload
                    ) VALUES (:order_id,'SYNTHETIC BAD',10,1,2,true,'not_completed','{}'::jsonb)
                """), {"order_id": order_id})
                session.commit()
            with self.assertRaises(IntegrityError), Session(engine) as session:
                session.execute(text("""
                    INSERT INTO imports (source,status,rows_total,rows_imported,raw_payload)
                    VALUES ('synthetic','created',1,2,'{}'::jsonb)
                """))
                session.commit()
            with self.assertRaises(IntegrityError), Session(engine) as session:
                session.execute(text("""
                    INSERT INTO pending_events (event_type,status,attempts,payload)
                    VALUES ('synthetic','pending',-1,'{}'::jsonb)
                """))
                session.commit()
            with self.assertRaises(IntegrityError), Session(engine) as session:
                session.execute(text("""
                    INSERT INTO order_items (
                        order_id, product, quantity_pieces, quantity_blocks, scanned_blocks,
                        requires_kiz, status, source_import_id, raw_payload
                    ) VALUES (:order_id,'SYNTHETIC BAD IDENTITY',10,1,0,true,'not_completed',
                              'source-without-key','{}'::jsonb)
                """), {"order_id": order_id})
                session.commit()
            with engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO orders (
                        source, payment_type, client, address, status, import_order_key, raw_payload
                    ) VALUES ('synthetic_invariant','synthetic','SYNTHETIC UNIQUE 1','SYNTHETIC',
                              'not_completed','same-active-key','{}'::jsonb)
                """))
            with self.assertRaises(IntegrityError), Session(engine) as session:
                session.execute(text("""
                    INSERT INTO orders (
                        source, payment_type, client, address, status, import_order_key, raw_payload
                    ) VALUES ('synthetic_invariant','synthetic','SYNTHETIC UNIQUE 2','SYNTHETIC',
                              'not_completed','same-active-key','{}'::jsonb)
                """))
                session.commit()
        finally:
            engine.dispose()

    def test_read_only_preflight_reports_all_classes_and_blocks_migration(self):
        url = create_database(self.databases[1])
        run_alembic(url, "upgrade", "20260710_0009")
        engine = create_engine(url, pool_pre_ping=True)
        try:
            with engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO orders (
                        id, source, payment_type, client, address, status, import_order_key, raw_payload
                    ) VALUES (
                        '00000000-0000-0000-0000-000000000801','synthetic','synthetic',
                        'SYNTHETIC VIOLATION','SYNTHETIC','unsupported','duplicate-key','{}'::jsonb
                    ),(
                        '00000000-0000-0000-0000-000000000802','synthetic','synthetic',
                        'SYNTHETIC DUPLICATE','SYNTHETIC','not_completed','duplicate-key','{}'::jsonb
                    );
                    INSERT INTO order_items (
                        order_id, product, quantity_pieces, quantity_blocks, scanned_blocks,
                        pieces_per_block, requires_kiz, status, source_import_id, raw_payload
                    ) VALUES (
                        '00000000-0000-0000-0000-000000000801','SYNTHETIC',-1,1,2,0,true,
                        'unsupported','source-without-key','{}'::jsonb
                    );
                    INSERT INTO imports (source,status,rows_total,rows_imported,raw_payload)
                    VALUES ('synthetic','unsupported',1,2,'{}'::jsonb);
                    INSERT INTO pending_events (event_type,status,attempts,payload)
                    VALUES ('synthetic','unsupported',-1,'{}'::jsonb)
                """))
            before = scalar(url, "SELECT count(*) FROM orders")
            report = run_preflight(url)
            after = scalar(url, "SELECT count(*) FROM orders")
            self.assertTrue(report["zero_mutation"])
            self.assertEqual(before, after)
            for name in (
                "order_item_negative_quantities", "order_item_nonpositive_pieces_per_block",
                "order_item_scanned_exceeds_plan", "unsupported_order_status", "unsupported_item_status",
                "unsupported_import_status", "invalid_import_row_counts", "unsupported_event_status",
                "negative_event_attempts", "source_identity_pair_mismatch", "duplicate_active_order_identity",
            ):
                self.assertGreater(report["invariants"][name], 0, name)
            gate = subprocess.run(
                [
                    sys.executable, "tools/check_data_invariants.py",
                    "--database-url", url, "--apply-gate",
                ],
                cwd=os.path.dirname(os.path.dirname(__file__)),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(gate.returncode, 1, gate.stdout)
            gate_report = json.loads(gate.stdout)
            self.assertEqual(gate_report["mode"], "apply_gate")
            self.assertFalse(gate_report["ddl_executed"])
            self.assertEqual(gate_report["automatic_repairs"], 0)
            self.assertTrue(gate_report["zero_mutation"])
            with self.assertRaises(AssertionError):
                run_alembic(url, "upgrade", "20260710_0010")
            self.assertEqual(scalar(url, "SELECT version_num FROM alembic_version"), "20260710_0009")
            self.assertEqual(scalar(url, "SELECT count(*) FROM orders"), before)
        finally:
            engine.dispose()

    def test_lock_timeout_leaves_previous_head_without_partial_contract(self):
        url = create_database(self.databases[2])
        run_alembic(url, "upgrade", "20260710_0009")
        engine = create_engine(url, pool_pre_ping=True)
        holder = engine.connect()
        transaction = holder.begin()
        holder.execute(text("LOCK TABLE order_items IN ACCESS EXCLUSIVE MODE"))
        result = {}

        def migrate():
            started = time.monotonic()
            try:
                run_alembic(url, "upgrade", "20260710_0010")
                result["status"] = "unexpected_pass"
            except AssertionError as exc:
                result["status"] = "timeout"
                result["error"] = str(exc)
            result["elapsed"] = time.monotonic() - started

        worker = threading.Thread(target=migrate, daemon=True)
        worker.start()
        worker.join(timeout=10)
        transaction.rollback()
        holder.close()
        worker.join(timeout=5)
        try:
            self.assertEqual(result.get("status"), "timeout")
            self.assertLess(result["elapsed"], 8)
            self.assertEqual(scalar(url, "SELECT version_num FROM alembic_version"), "20260710_0009")
            inspector = inspect(engine)
            constraints = {item["name"] for item in inspector.get_check_constraints("order_items")}
            indexes = {item["name"] for item in inspector.get_indexes("orders")}
            self.assertNotIn("ck_order_items_quantities_nonnegative", constraints)
            self.assertNotIn("uq_orders_active_import_order_key", indexes)
            run_alembic(url, "upgrade", "20260710_0010")
            self.assertEqual(scalar(url, "SELECT version_num FROM alembic_version"), "20260710_0010")
            self.assertEqual(scalar(
                url,
                "SELECT count(*) FROM pg_constraint WHERE conname IN ("
                "'ck_order_items_quantities_nonnegative','ck_order_items_pieces_per_block_positive',"
                "'ck_order_items_scanned_within_plan','ck_orders_supported_status',"
                "'ck_order_items_supported_status','ck_imports_supported_status','ck_imports_row_counts',"
                "'ck_pending_events_supported_status','ck_pending_events_attempts_nonnegative',"
                "'ck_order_items_source_identity_pair','ck_orders_import_keys_nonblank',"
                "'ck_order_items_import_keys_nonblank') AND convalidated",
            ), 12)
            self.assertEqual(scalar(
                url,
                "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                "WHERE c.relname IN ('uq_orders_active_import_order_key',"
                "'uq_order_items_order_source_import_key',"
                "'uq_order_items_order_import_item_key_fallback') "
                "AND i.indisvalid AND i.indisready",
            ), 3)
        finally:
            engine.dispose()

    def test_stress_previous_head_migrates_to_single_head(self):
        url = create_database(self.databases[3])
        run_alembic(url, "upgrade", "20260710_0009")
        engine = create_engine(url, pool_pre_ping=True)
        try:
            with engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO orders (
                        id, source, external_id, payment_type, client, address, status,
                        import_order_key, import_source_order_key, raw_payload
                    ) SELECT gen_random_uuid(),'synthetic_stress','SYNTHETIC-'||value,'synthetic',
                        'SYNTHETIC CLIENT '||value,'SYNTHETIC ADDRESS','not_completed',
                        md5(value::text)||md5('order'||value::text),md5(value::text)||md5('order'||value::text),'{}'::jsonb
                    FROM generate_series(1,5000) value
                """))
                connection.execute(text("""
                    INSERT INTO order_items (
                        order_id, product, quantity_pieces, quantity_blocks, scanned_blocks,
                        pieces_per_block, requires_kiz, status, raw_payload
                    ) SELECT id,'SYNTHETIC PRODUCT',20,2,0,10,true,'not_completed','{}'::jsonb FROM orders
                """))
                connection.execute(text("""
                    INSERT INTO imports (source,status,rows_total,rows_imported,raw_payload)
                    SELECT 'synthetic_stress','completed',10,10,'{}'::jsonb
                    FROM generate_series(1,5000)
                """))
                connection.execute(text("""
                    INSERT INTO pending_events (event_type,status,attempts,payload)
                    SELECT 'synthetic_stress','pending',0,'{}'::jsonb
                    FROM generate_series(1,5000)
                """))
            started = time.monotonic()
            run_alembic(url, "upgrade", "20260710_0010")
            elapsed = time.monotonic() - started
            self.assertEqual(scalar(url, "SELECT version_num FROM alembic_version"), "20260710_0010")
            self.assertLess(elapsed, 30)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
