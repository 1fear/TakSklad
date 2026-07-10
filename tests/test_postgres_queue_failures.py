import os
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, event as sqlalchemy_event, text
from sqlalchemy.orm import sessionmaker

from backend.app.event_leases import (
    LeaseOwnershipError,
    claim_event_leases,
    finalize_event_leases,
    recover_expired_event_leases,
)
from tests.postgres_support import create_database, drop_database, run_alembic


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresQueueFailureTests(unittest.TestCase):
    database_name = "taksklad_phase4_queue_failures"

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
                ) VALUES (
                    gen_random_uuid(), 'phase4_failure', 'pending', 0, '{}'::jsonb,
                    now(), now(), now()
                )
            """))

    def test_stale_owner_cannot_finalize_reassigned_lease(self):
        with self.SessionLocal() as session:
            first = claim_event_leases(session, event_types=("phase4_failure",), owner="owner-a", limit=1)[0]
            session.execute(text(
                "UPDATE pending_events SET lease_expires_at = now() - interval '1 second' WHERE id = :id"
            ), {"id": first.id})
            session.commit()
        with self.SessionLocal() as session:
            second = claim_event_leases(session, event_types=("phase4_failure",), owner="owner-b", limit=1)[0]
            self.assertEqual(second.id, first.id)
        with self.SessionLocal() as session:
            with self.assertRaises(LeaseOwnershipError):
                finalize_event_leases(session, event_ids=(first.id,), owner="owner-a", status="completed")
            self.assertEqual(finalize_event_leases(
                session, event_ids=(first.id,), owner="owner-b", status="completed"
            ), 1)

    def test_only_expired_leases_recover(self):
        now = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO pending_events (
                    id, event_type, status, attempts, payload, created_at, updated_at,
                    available_at, lease_owner, lease_expires_at
                ) VALUES
                    (gen_random_uuid(), 'phase4_failure', 'processing', 1, '{}'::jsonb, now(), now(), now(), 'expired', :expired),
                    (gen_random_uuid(), 'phase4_failure', 'processing', 1, '{}'::jsonb, now(), now(), now(), 'active', :active)
            """), {"expired": now - timedelta(seconds=1), "active": now + timedelta(minutes=5)})
        with self.SessionLocal() as session:
            recovered = recover_expired_event_leases(
                session, event_types=("phase4_failure",), now=now
            )
        self.assertEqual(recovered, 1)
        with self.engine.connect() as connection:
            rows = dict(connection.execute(text(
                "SELECT lease_owner, status FROM pending_events WHERE lease_owner IS NOT NULL"
            )).all())
        self.assertEqual(rows, {"active": "processing"})

    def test_claim_commit_crash_rolls_back_without_losing_event(self):
        with self.SessionLocal() as session:
            def fail_commit(_session):
                raise RuntimeError("synthetic claim commit crash")

            sqlalchemy_event.listen(session, "before_commit", fail_commit, once=True)
            with self.assertRaises(RuntimeError):
                claim_event_leases(session, event_types=("phase4_failure",), owner="crashed", limit=1)
        with self.engine.connect() as connection:
            row = connection.execute(text(
                "SELECT status, lease_owner FROM pending_events ORDER BY created_at LIMIT 1"
            )).one()
        self.assertEqual(tuple(row), ("pending", None))

    def test_finalize_commit_crash_remains_recoverable_and_not_stuck(self):
        with self.SessionLocal() as session:
            claimed = claim_event_leases(
                session, event_types=("phase4_failure",), owner="finalize-crash", limit=1,
            )[0]
            claimed_id = claimed.id

            def fail_commit(_session):
                raise RuntimeError("synthetic finalize commit crash")

            sqlalchemy_event.listen(session, "before_commit", fail_commit, once=True)
            with self.assertRaises(RuntimeError):
                finalize_event_leases(
                    session,
                    event_ids=(claimed_id,),
                    owner="finalize-crash",
                    status="completed",
                )
        with self.engine.begin() as connection:
            row = connection.execute(text(
                "SELECT status, lease_owner FROM pending_events WHERE id = :id"
            ), {"id": claimed_id}).one()
            self.assertEqual(tuple(row), ("processing", "finalize-crash"))
            connection.execute(text(
                "UPDATE pending_events SET lease_expires_at = now() - interval '1 second' WHERE id = :id"
            ), {"id": claimed_id})
        with self.SessionLocal() as session:
            recovered = claim_event_leases(
                session, event_types=("phase4_failure",), owner="recovery", limit=1,
            )[0]
            self.assertEqual(recovered.id, claimed_id)
            finalize_event_leases(
                session, event_ids=(recovered.id,), owner="recovery", status="completed",
            )
        with self.engine.connect() as connection:
            final = connection.execute(text(
                "SELECT status, lease_owner FROM pending_events WHERE id = :id"
            ), {"id": claimed_id}).one()
        self.assertEqual(tuple(final), ("completed", None))

    def test_retry_claim_uses_available_at_index(self):
        with self.engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO pending_events (
                    id, event_type, status, attempts, payload, created_at, updated_at, available_at
                )
                SELECT gen_random_uuid(), 'phase4_failure', 'pending', 0, '{}'::jsonb,
                       now(), now(), now() + interval '1 hour'
                FROM generate_series(1, 5000)
            """))
            connection.execute(text("ANALYZE pending_events"))
            connection.execute(text("SET LOCAL enable_seqscan = off"))
            plan = "\n".join(row[0] for row in connection.execute(text("""
                EXPLAIN (COSTS OFF)
                SELECT id FROM pending_events
                WHERE event_type = 'phase4_failure'
                  AND status IN ('pending', 'failed')
                  AND available_at <= now()
                ORDER BY available_at, created_at, id
                LIMIT 100
            """)))
        self.assertIn("idx_pending_events_claim_ordered", plan)
        self.assertNotIn("payload", plan.lower())


if __name__ == "__main__":
    unittest.main()
