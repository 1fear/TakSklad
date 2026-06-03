import unittest
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.google_sheets_pending import (
    GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
    acquire_google_sheets_export_lock,
    process_pending_google_sheets_exports,
    release_google_sheets_export_lock,
)
from backend.app.models import Base, PendingEvent


class GoogleSheetsPendingLockTests(unittest.TestCase):
    def test_postgres_lock_does_not_use_session_level_advisory_lock(self):
        db = mock.Mock()
        db.bind.dialect.name = "postgresql"

        self.assertTrue(acquire_google_sheets_export_lock(db))
        release_google_sheets_export_lock(db)

        db.execute.assert_not_called()

    def test_rate_limit_keeps_event_pending_and_stops_batch(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        try:
            with SessionLocal() as db:
                db.add(PendingEvent(
                    event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
                    status="pending",
                    payload={"action": "google_sheets_skladbot_export", "entity_id": "active_orders"},
                ))
                db.add(PendingEvent(
                    event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
                    status="pending",
                    payload={"action": "google_sheets_scan_export", "entity_id": "item-1"},
                ))
                db.commit()

                with mock.patch(
                    "backend.app.google_sheets_pending.run_google_sheets_export_event",
                    side_effect=RuntimeError("APIError: [429]: Quota exceeded"),
                ) as export_mock, mock.patch("backend.app.google_sheets_pending.logger.exception"), mock.patch(
                    "backend.app.google_sheets_pending.logger.warning"
                ):
                    result = process_pending_google_sheets_exports(db, limit=50)

                events = db.execute(
                    select(PendingEvent).order_by(PendingEvent.created_at, PendingEvent.id)
                ).scalars().all()

            self.assertEqual(result["status"], "paused")
            self.assertEqual(result["checked"], 2)
            self.assertEqual(result["failed"], 0)
            self.assertEqual(result["remaining"], 2)
            self.assertEqual(export_mock.call_count, 1)
            self.assertEqual([event.status for event in events], ["pending", "pending"])
            self.assertEqual(events[0].attempts, 1)
            self.assertIn("429", events[0].last_error)
            self.assertEqual(events[1].attempts, 0)
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
