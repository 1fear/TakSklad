import inspect
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.app.outbox_service import MAX_OUTBOX_PAYLOAD_BYTES, queue_outbox_event
from backend.app.skladbot_request_dry_run import queue_skladbot_create_events
from backend.app.skladbot_return_requests import queue_skladbot_return_request_create
from tests.postgres_support import create_database, drop_database, run_alembic, scalar


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresOutboxTests(unittest.TestCase):
    databases = ("taksklad_phase9_outbox", "taksklad_phase9_outbox_migration")

    @classmethod
    def setUpClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.url = create_database(cls.databases[0])
        run_alembic(cls.url, "upgrade", "head")
        cls.engine = create_engine(cls.url, pool_pre_ping=True)
        cls.SessionLocal = sessionmaker(bind=cls.engine, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.engine.dispose()
        for database in cls.databases:
            drop_database(database)

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(text("TRUNCATE pending_events CASCADE"))

    def test_partial_expand_retry_backfills_identity_and_builds_valid_index(self):
        url = create_database(self.databases[1])
        run_alembic(url, "upgrade", "20260710_0010")
        engine = create_engine(url, pool_pre_ping=True)
        try:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE pending_events ADD COLUMN action varchar(80)"))
                connection.execute(text("ALTER TABLE pending_events ADD COLUMN aggregate_type varchar(80)"))
                connection.execute(text("ALTER TABLE pending_events ADD COLUMN aggregate_id varchar(180)"))
                connection.execute(text("""
                    INSERT INTO pending_events (event_type,status,attempts,payload)
                    VALUES ('google_sheets_export','pending',0,
                            '{"action":"google_sheets_scan_export","entity_type":"order_item","entity_id":"legacy-item"}'::jsonb)
                """))
                connection.execute(text("""
                    INSERT INTO pending_events (event_type,status,attempts,payload)
                    VALUES ('google_sheets_export','failed',1, '[1,"legacy"]'::jsonb)
                """))
            run_alembic(url, "upgrade", "head")
            run_alembic(url, "upgrade", "head")
            self.assertEqual(scalar(url, "SELECT version_num FROM alembic_version"), "20260719_0020")
            with engine.connect() as connection:
                rows = connection.execute(text(
                    "SELECT action,aggregate_type,aggregate_id,status,payload "
                    "FROM pending_events ORDER BY attempts"
                )).all()
                index_state = connection.execute(text("""
                    SELECT i.indisvalid,i.indisready
                    FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
                    WHERE c.relname='idx_pending_events_action_aggregate_status'
                """)).one()
                audit_count = connection.scalar(text(
                    "SELECT count(*) FROM audit_log WHERE action='google_runtime_event_cancelled'"
                ))
            self.assertEqual(
                tuple(rows[0][:4]),
                ("google_sheets_scan_export", "order_item", "legacy-item", "cancelled"),
            )
            self.assertEqual(rows[1].status, "cancelled")
            self.assertEqual(rows[1].payload["legacy_payload"], [1, "legacy"])
            self.assertEqual(rows[1].payload["legacy_payload_type"], "list")
            self.assertEqual(audit_count, 2)
            self.assertEqual(tuple(index_state), (True, True))
        finally:
            engine.dispose()

    def test_duplicate_idempotency_is_race_safe_and_returns_existing_event(self):
        barrier = threading.Barrier(2)

        def worker(worker_number):
            with self.SessionLocal() as session:
                barrier.wait(timeout=5)
                event = queue_outbox_event(
                    session,
                    event_type="phase9_synthetic",
                    action="phase9_action",
                    aggregate_type="order",
                    aggregate_id="phase9-order",
                    idempotency_key="phase9:duplicate:key",
                    payload={"worker": worker_number},
                )
                event_id = str(event.id)
                session.commit()
                return event_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            event_ids = list(executor.map(worker, (1, 2)))
        self.assertEqual(len(set(event_ids)), 1)
        self.assertEqual(scalar(self.url, "SELECT count(*) FROM pending_events"), 1)

    def test_normalized_lookup_uses_index_without_payload_expression(self):
        with self.engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO pending_events (
                    event_type,action,aggregate_type,aggregate_id,idempotency_key,status,attempts,payload
                ) SELECT 'phase9_synthetic','phase9_action','order','order-'||value,
                         'phase9-plan-'||value,'pending',0,'{}'::jsonb
                  FROM generate_series(1,5000) value
            """))
            connection.execute(text("ANALYZE pending_events"))
            connection.execute(text("SET LOCAL enable_seqscan = off"))
            plan = "\n".join(row[0] for row in connection.execute(text("""
                EXPLAIN (COSTS OFF)
                SELECT id FROM pending_events
                 WHERE action='phase9_action' AND aggregate_type='order'
                   AND aggregate_id='order-4999' AND status='pending'
                 ORDER BY created_at,id LIMIT 1
            """)))
        self.assertIn("idx_pending_events_action_aggregate_status", plan)
        self.assertNotIn("payload", plan.lower())

    def test_payload_is_redacted_and_bounded_before_storage(self):
        raw_markers = ("SYNTHETIC-PASSWORD", "SYNTHETIC-BEARER", "SYNTHETIC-TOKEN")
        with self.SessionLocal() as session:
            event = queue_outbox_event(
                session,
                event_type="phase9_synthetic",
                action="phase9_redaction",
                aggregate_type="import",
                aggregate_id="phase9-import",
                idempotency_key="phase9:redaction:key",
                payload={
                    "password": raw_markers[0],
                    "nested": {"authorization": f"Bearer {raw_markers[1]}"},
                    "message": f"token={raw_markers[2]}",
                },
            )
            session.commit()
            encoded = json.dumps(event.payload, ensure_ascii=False, sort_keys=True)
        for marker in raw_markers:
            self.assertNotIn(marker, encoded)
        self.assertLessEqual(len(encoded.encode("utf-8")), MAX_OUTBOX_PAYLOAD_BYTES)
        with self.SessionLocal() as session:
            with self.assertRaises(ValueError):
                queue_outbox_event(
                    session,
                    event_type="phase9_synthetic",
                    action="phase9_oversize",
                    aggregate_type="import",
                    aggregate_id="phase9-import-large",
                    idempotency_key="phase9:oversize:key",
                    payload={"value": "x" * (MAX_OUTBOX_PAYLOAD_BYTES + 1)},
                )

    def test_producer_queue_helpers_never_commit_rollback_or_call_clients(self):
        helpers = (
            queue_outbox_event,
            queue_skladbot_create_events,
            queue_skladbot_return_request_create,
        )
        forbidden = (".commit(", ".rollback(", "SkladBotClient(", "append_import_records_to_google_sheets(")
        for helper in helpers:
            source = inspect.getsource(helper)
            for marker in forbidden:
                self.assertNotIn(marker, source, f"{helper.__name__} contains {marker}")


if __name__ == "__main__":
    unittest.main()
