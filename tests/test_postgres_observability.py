import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from backend.app.health_service import build_readiness_report, readiness_http_status
from backend.app.models import WorkerHeartbeat
from backend.app.worker_observability import KNOWN_WORKERS, observed_worker_cycle
from tests.postgres_support import create_database, drop_database, run_alembic


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresObservabilityTests(unittest.TestCase):
    database_name = "taksklad_phase25_observability"

    @classmethod
    def setUpClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.url = create_database(cls.database_name)
        run_alembic(cls.url, "upgrade", "head")
        cls.engine = create_engine(cls.url, pool_pre_ping=True)
        cls.session_factory = sessionmaker(bind=cls.engine, expire_on_commit=False)
        cls.settings = SimpleNamespace(
            service_name="test",
            environment="test",
            worker_heartbeat_required_names=KNOWN_WORKERS,
        )

    @classmethod
    def tearDownClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.engine.dispose()
        drop_database(cls.database_name)

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM worker_heartbeats"))

    def test_real_cycle_writes_success_heartbeat_for_every_worker(self):
        for worker_name in KNOWN_WORKERS:
            with observed_worker_cycle(worker_name, 10, grace_seconds=3, session_factory=self.session_factory):
                pass

        with Session(self.engine) as session:
            rows = session.query(WorkerHeartbeat).order_by(WorkerHeartbeat.worker_name).all()
            self.assertEqual([row.worker_name for row in rows], list(KNOWN_WORKERS))
            self.assertTrue(all(row.status == "success" for row in rows))
            self.assertTrue(all(row.last_success_at is not None for row in rows))
            self.assertTrue(all(len(row.correlation_id) == 36 for row in rows))
            report = build_readiness_report(session, self.settings)
            self.assertTrue(report["ready"])
            self.assertEqual(readiness_http_status(report), 200)

    def test_hung_loop_fails_closed_after_two_intervals_plus_grace_and_recovers(self):
        now = datetime.now(timezone.utc)
        with Session(self.engine) as session:
            for worker_name in KNOWN_WORKERS:
                session.add(WorkerHeartbeat(
                    worker_name=worker_name,
                    interval_seconds=10,
                    grace_seconds=3,
                    status="success",
                    correlation_id="00000000-0000-4000-8000-000000000000",
                    last_cycle_started_at=now,
                    last_success_at=now,
                ))
            session.commit()
            target = session.get(WorkerHeartbeat, "telegram")
            target.status = "running"
            target.last_cycle_started_at = now - timedelta(seconds=24)
            session.commit()

            report = build_readiness_report(session, self.settings)
            self.assertFalse(report["ready"])
            self.assertEqual(readiness_http_status(report), 503)
            self.assertEqual(report["workers"]["unhealthy"], ["telegram"])

        with observed_worker_cycle("telegram", 10, grace_seconds=3, session_factory=self.session_factory):
            pass
        with Session(self.engine) as session:
            recovered = build_readiness_report(session, self.settings)
            self.assertTrue(recovered["ready"])
            self.assertEqual(recovered["workers"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
