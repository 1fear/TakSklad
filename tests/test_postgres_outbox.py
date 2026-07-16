import inspect
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.app.google_sheets_pending import queue_google_sheets_export
from backend.app.imports_service import export_import_records_to_google_sheets
from backend.app.order_actions_service import queue_order_projection_to_google
from backend.app.orders_service import queue_order_google_intent, record_google_sheets_export_result
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
            run_alembic(url, "upgrade", "head")
            self.assertEqual(scalar(url, "SELECT version_num FROM alembic_version"), "20260716_0018")
            with engine.connect() as connection:
                row = connection.execute(text(
                    "SELECT action,aggregate_type,aggregate_id FROM pending_events"
                )).one()
                index_state = connection.execute(text("""
                    SELECT i.indisvalid,i.indisready
                    FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
                    WHERE c.relname='idx_pending_events_action_aggregate_status'
                """)).one()
            self.assertEqual(tuple(row), ("google_sheets_scan_export", "order_item", "legacy-item"))
            self.assertEqual(tuple(index_state), (True, True))
        finally:
            engine.dispose()

    def test_large_import_records_are_chunked_without_lost_payload(self):
        records = [
            {
                "ID импорта": f"phase9-large-{index}",
                "Клиент": f"SYNTHETIC CLIENT {index}",
                "Описание": "x" * 700,
            }
            for index in range(5000)
        ]
        with self.SessionLocal() as session:
            result = export_import_records_to_google_sheets(session, records, import_job_id="phase9-large-import")
            session.commit()
            events = session.execute(text("""
                SELECT payload FROM pending_events
                WHERE action='google_sheets_import_export' AND aggregate_id='phase9-large-import'
                ORDER BY (payload->>'chunk_index')::int
            """)).scalars().all()
        self.assertGreater(result["events_queued"], 1)
        self.assertEqual(len(events), result["events_queued"])
        self.assertEqual(sum(len(payload["records"]) for payload in events), 5000)
        self.assertEqual([payload["chunk_index"] for payload in events], list(range(1, len(events) + 1)))
        self.assertTrue(all(payload["chunk_count"] == len(events) for payload in events))
        self.assertTrue(all(
            len(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            <= MAX_OUTBOX_PAYLOAD_BYTES
            for payload in events
        ))

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

    def test_google_fast_path_coalesces_active_legacy_identity(self):
        with self.SessionLocal() as session:
            first = queue_google_sheets_export(
                session,
                "google_sheets_archive_export",
                "order",
                "phase26-coalesced-order",
                result={"status": "queued"},
                payload={"idempotency_key": "phase26:legacy:key"},
            )
            session.commit()
            second = queue_google_sheets_export(
                session,
                "google_sheets_archive_export",
                "order",
                "phase26-coalesced-order",
                result={"status": "queued"},
                payload={"idempotency_key": "phase26:new:key"},
            )
            session.commit()
            count = session.execute(text(
                "SELECT count(*) FROM pending_events WHERE aggregate_id='phase26-coalesced-order'"
            )).scalar_one()

        self.assertEqual(first.id, second.id)
        self.assertEqual(count, 1)
        self.assertEqual(second.idempotency_key, "phase26:legacy:key")
        self.assertEqual(second.payload["idempotency_key"], "phase26:legacy:key")

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
            queue_google_sheets_export,
            queue_order_google_intent,
            record_google_sheets_export_result,
            queue_order_projection_to_google,
            export_import_records_to_google_sheets,
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
