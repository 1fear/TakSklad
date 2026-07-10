import os
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from backend.app.health_service import build_readiness_report, readiness_http_status
from backend.app.models import PendingEvent
from tests.postgres_support import create_database, drop_database, run_alembic


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresReadinessTests(unittest.TestCase):
    database_name = "taksklad_phase3_readiness"

    @classmethod
    def setUpClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.url = create_database(cls.database_name)
        run_alembic(cls.url, "upgrade", "head")
        cls.engine = create_engine(cls.url, pool_pre_ping=True)
        cls.settings = SimpleNamespace(service_name="test", environment="test")

    @classmethod
    def tearDownClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.engine.dispose()
        drop_database(cls.database_name)

    def test_current_head_is_ready_and_stale_revision_is_503(self):
        with Session(self.engine) as session:
            current = build_readiness_report(session, self.settings)
            self.assertTrue(current["ready"])
            self.assertEqual(readiness_http_status(current), 200)
            session.execute(text("UPDATE alembic_version SET version_num = '20260616_0001'"))
            session.commit()
            stale = build_readiness_report(session, self.settings)
            self.assertFalse(stale["ready"])
            self.assertEqual(readiness_http_status(stale), 503)
            session.execute(text("UPDATE alembic_version SET version_num = '20260711_0015'"))
            session.commit()

    def test_mandatory_queue_failure_is_503(self):
        with Session(self.engine) as session:
            event = PendingEvent(
                event_type="telegram_notification",
                status="failed",
                attempts=1,
                payload={},
                last_error=None,
            )
            session.add(event)
            session.commit()
            report = build_readiness_report(session, self.settings)
            self.assertFalse(report["ready"])
            self.assertEqual(report["queue"]["hot_path_blocking_count"], 1)
            self.assertEqual(readiness_http_status(report), 503)
            session.delete(event)
            session.commit()


if __name__ == "__main__":
    unittest.main()
