import os
import threading
import unittest

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tests.postgres_support import TwoSessionBarrier, create_database, drop_database, run_alembic


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresTwoSessionBarrierTests(unittest.TestCase):
    database_name = "taksklad_phase2_concurrency"

    @classmethod
    def setUpClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.url = create_database(cls.database_name)
        run_alembic(cls.url, "upgrade", "head")
        cls.engine = create_engine(cls.url, pool_pre_ping=True)
        with cls.engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE phase2_claims (id integer PRIMARY KEY, state text NOT NULL)"
            ))

    @classmethod
    def tearDownClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.engine.dispose()
        drop_database(cls.database_name)

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(text("TRUNCATE phase2_claims"))
            connection.execute(text("INSERT INTO phase2_claims (id, state) VALUES (1, 'pending')"))

    def test_second_session_skip_locked_returns_without_duplicate_claim(self):
        barrier = TwoSessionBarrier()
        result = []
        holder = Session(self.engine)
        holder.begin()
        holder.execute(text("SELECT id FROM phase2_claims WHERE id = 1 FOR UPDATE"))

        def worker():
            try:
                with Session(self.engine) as session, session.begin():
                    barrier.worker_started()
                    rows = session.execute(text(
                        "SELECT id FROM phase2_claims WHERE id = 1 FOR UPDATE SKIP LOCKED"
                    )).scalars().all()
                    result.extend(rows)
            except Exception as exc:
                barrier.capture_error(exc)
            finally:
                barrier.mark_completed()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        barrier.wait_for_worker()
        self.assertTrue(barrier.completed.wait(2))
        holder.commit()
        holder.close()
        thread.join(timeout=2)

        barrier.assert_no_errors(self)
        self.assertEqual(result, [])

    def test_second_session_blocks_until_holder_releases_row(self):
        barrier = TwoSessionBarrier()
        result = []
        holder = Session(self.engine)
        holder.begin()
        holder.execute(text("SELECT id FROM phase2_claims WHERE id = 1 FOR UPDATE"))

        def worker():
            try:
                with Session(self.engine) as session, session.begin():
                    barrier.worker_started()
                    rows = session.execute(text(
                        "SELECT id FROM phase2_claims WHERE id = 1 FOR UPDATE"
                    )).scalars().all()
                    result.extend(rows)
            except Exception as exc:
                barrier.capture_error(exc)
            finally:
                barrier.mark_completed()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        barrier.wait_for_worker()
        self.assertFalse(barrier.completed.wait(0.2))
        holder.commit()
        holder.close()
        self.assertTrue(barrier.completed.wait(2))
        thread.join(timeout=2)

        barrier.assert_no_errors(self)
        self.assertEqual(result, [1])


if __name__ == "__main__":
    unittest.main()
