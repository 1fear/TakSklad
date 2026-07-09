import os
import threading
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from backend.app.event_leases import claim_event_leases, finalize_event_leases
from tests.postgres_support import create_database, drop_database, run_alembic


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresQueueConcurrencyTests(unittest.TestCase):
    database_name = "taksklad_phase4_queue_concurrency"

    @classmethod
    def setUpClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.url = create_database(cls.database_name)
        run_alembic(cls.url, "upgrade", "head")
        cls.engine = create_engine(cls.url, pool_pre_ping=True)
        cls.SessionLocal = sessionmaker(bind=cls.engine, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.engine.dispose()
        drop_database(cls.database_name)

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(text("TRUNCATE pending_events CASCADE"))
            connection.execute(text("""
                INSERT INTO pending_events (
                    id, event_type, status, attempts, payload,
                    created_at, updated_at, available_at
                )
                SELECT gen_random_uuid(), 'phase4_synthetic', 'pending', 0, '{}'::jsonb,
                       now(), now(), now()
                FROM generate_series(1, 10000)
            """))

    def test_four_workers_claim_ten_thousand_without_duplicates_or_loss(self):
        claimed_ids = []
        durable_before_call = []
        collection_lock = threading.Lock()

        def worker(worker_number):
            owner = f"phase4-worker-{worker_number}"
            while True:
                with self.SessionLocal() as session:
                    events = claim_event_leases(
                        session,
                        event_types=("phase4_synthetic",),
                        owner=owner,
                        limit=250,
                    )
                    if not events:
                        return
                    event_ids = [event.id for event in events]
                    with self.engine.connect() as observer:
                        durable = observer.execute(text("""
                            SELECT count(*) FROM pending_events
                            WHERE lease_owner = :owner
                              AND status = 'processing'
                              AND id = ANY(:event_ids)
                        """), {"owner": owner, "event_ids": event_ids}).scalar_one()
                    with collection_lock:
                        claimed_ids.extend(str(event_id) for event_id in event_ids)
                        durable_before_call.append(durable == len(event_ids))
                    finalized = finalize_event_leases(
                        session,
                        event_ids=event_ids,
                        owner=owner,
                        status="completed",
                    )
                    self.assertEqual(finalized, len(event_ids))

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(worker, range(4)))

        counts = Counter(claimed_ids)
        with self.engine.connect() as connection:
            completed = connection.execute(text(
                "SELECT count(*) FROM pending_events WHERE status = 'completed'"
            )).scalar_one()
            processing = connection.execute(text(
                "SELECT count(*) FROM pending_events WHERE status = 'processing'"
            )).scalar_one()
        self.assertEqual(len(claimed_ids), 10000)
        self.assertEqual(max(counts.values()), 1)
        self.assertTrue(all(durable_before_call))
        self.assertEqual(completed, 10000)
        self.assertEqual(processing, 0)


if __name__ == "__main__":
    unittest.main()
