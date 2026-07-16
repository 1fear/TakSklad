import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from backend.app.health_service import build_readiness_report, readiness_http_status
from backend.app.models import WorkerHeartbeat
from backend.app.observability_context import bind_correlation_id, reset_correlation_id
from backend.app.worker_observability import (
    KNOWN_WORKERS,
    build_worker_readiness,
    observed_worker_cycle,
    record_cycle_result,
    record_cycle_start,
    record_cycle_progress,
)
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
            self.assertTrue(all(row.last_progress_at is not None for row in rows))
            self.assertTrue(all(row.last_progress_phase == "cycle_succeeded" for row in rows))
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
                    last_progress_at=now,
                    last_success_at=now,
                ))
            session.commit()
            target = session.get(WorkerHeartbeat, "telegram")
            target.status = "running"
            target.last_cycle_started_at = now - timedelta(seconds=24)
            target.last_progress_at = now - timedelta(seconds=24)
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

    def test_running_cycle_stays_ready_only_while_real_progress_is_fresh(self):
        now = datetime.now(timezone.utc)
        cycle_id = "00000000-0000-4000-8000-000000000009"
        token = bind_correlation_id(cycle_id)
        try:
            record_cycle_start(
                "skladbot",
                10,
                grace_seconds=3,
                session_factory=self.session_factory,
                now=now - timedelta(seconds=60),
            )
        finally:
            reset_correlation_id(token)
        record_cycle_progress(
            "skladbot",
            "remote_details_processed:3",
            correlation_id=cycle_id,
            session_factory=self.session_factory,
            now=now,
        )
        with Session(self.engine) as session:
            row = session.get(WorkerHeartbeat, "skladbot")
            row.last_cycle_started_at = now - timedelta(seconds=60)
            session.commit()
            report = build_worker_readiness(
                session,
                required_workers=("skladbot",),
                now=now + timedelta(seconds=20),
            )
            self.assertEqual(report["status"], "ok")
            worker = report["workers"][0]
            self.assertEqual(worker["last_progress_phase"], "remote_details_processed:3")
            self.assertEqual(worker["age_seconds"], 20)

            row.last_progress_at = now - timedelta(seconds=24)
            session.commit()
            stale = build_worker_readiness(
                session,
                required_workers=("skladbot",),
                now=now,
            )
            self.assertEqual(stale["status"], "unhealthy")
            self.assertEqual(stale["unhealthy"], ["skladbot"])

    def test_late_progress_cannot_reopen_finished_cycle(self):
        cycle_id = "00000000-0000-4000-8000-000000000012"
        token = bind_correlation_id(cycle_id)
        try:
            record_cycle_start("skladbot", 10, session_factory=self.session_factory)
        finally:
            reset_correlation_id(token)
        record_cycle_result(
            "skladbot",
            correlation_id=cycle_id,
            session_factory=self.session_factory,
        )
        record_cycle_progress(
            "skladbot",
            "late_progress",
            correlation_id=cycle_id,
            session_factory=self.session_factory,
        )

        with Session(self.engine) as session:
            row = session.get(WorkerHeartbeat, "skladbot")
            self.assertEqual(row.status, "success")
            self.assertEqual(row.last_progress_phase, "cycle_succeeded")

    def test_old_cycle_cannot_refresh_or_finish_newer_cycle(self):
        old_id = "00000000-0000-4000-8000-000000000010"
        new_id = "00000000-0000-4000-8000-000000000011"
        old_token = bind_correlation_id(old_id)
        try:
            record_cycle_start("skladbot", 10, session_factory=self.session_factory)
        finally:
            reset_correlation_id(old_token)
        new_token = bind_correlation_id(new_id)
        try:
            record_cycle_start("skladbot", 10, session_factory=self.session_factory)
        finally:
            reset_correlation_id(new_token)

        record_cycle_progress(
            "skladbot",
            "stale_cycle_progress",
            correlation_id=old_id,
            session_factory=self.session_factory,
        )
        record_cycle_result(
            "skladbot",
            correlation_id=old_id,
            session_factory=self.session_factory,
        )

        with Session(self.engine) as session:
            row = session.get(WorkerHeartbeat, "skladbot")
            self.assertEqual(row.correlation_id, new_id)
            self.assertEqual(row.status, "running")
            self.assertEqual(row.last_progress_phase, "cycle_started")


if __name__ == "__main__":
    unittest.main()
