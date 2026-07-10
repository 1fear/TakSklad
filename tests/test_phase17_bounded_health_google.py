import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.google_sheets_pending import (
    GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
    STALE_PROCESSING_RESET_BATCH_SIZE,
    count_pending_export_events,
    google_sheets_export_cooldown_until,
    load_skladbot_export_orders,
    mark_google_sheets_export_synced,
    reset_stale_processing_export_events,
)
from backend.app.health_service import (
    SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
    build_google_mirror_readiness,
    count_unresolved_hot_path_failures,
)
from backend.app.models import Base, PendingEvent


class Phase17BoundedHealthGoogleTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def capture_selects(self):
        statements = []

        def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", capture)
        self.addCleanup(event.remove, self.engine, "before_cursor_execute", capture)
        return statements

    def test_hot_path_failure_count_is_one_correlated_aggregate_query(self):
        failed_at = datetime(2026, 7, 6, 17, 7, tzinfo=timezone.utc)
        success_at = failed_at + timedelta(hours=2)
        with self.SessionLocal() as db:
            db.add_all([
                PendingEvent(
                    event_type=SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
                    idempotency_key="skladbot_daily_report:2026-07-06:chat-1:scheduled:daily_skladbot:v2",
                    status="failed",
                    payload={"report_date": "2026-07-06", "kind": "daily_skladbot"},
                    created_at=failed_at,
                    updated_at=failed_at,
                ),
                PendingEvent(
                    event_type=SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
                    idempotency_key="skladbot_daily_report:2026-07-06:chat-1:manual:daily_skladbot:v3",
                    status="completed",
                    payload={
                        "report_date": "2026-07-06",
                        "kind": "daily_skladbot",
                        "success": True,
                    },
                    created_at=success_at,
                    updated_at=success_at,
                ),
                PendingEvent(event_type="telegram_notification", status="failed", payload={}),
            ])
            db.commit()
            statements = self.capture_selects()

            self.assertEqual(count_unresolved_hot_path_failures(db), 1)

        self.assertEqual(len(statements), 1)
        sql = statements[0].upper()
        self.assertIn("COUNT(", sql)
        self.assertIn("EXISTS", sql)

    def test_hot_path_failure_count_preserves_legacy_string_success(self):
        failed_at = datetime(2026, 7, 6, 17, 7, tzinfo=timezone.utc)
        with self.SessionLocal() as db:
            db.add_all([
                PendingEvent(
                    event_type=SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
                    idempotency_key="skladbot_daily_report:2026-07-06:chat-1:scheduled:daily_skladbot:v2",
                    status="failed",
                    payload={"report_date": "2026-07-06", "kind": "daily_skladbot"},
                    created_at=failed_at,
                    updated_at=failed_at,
                ),
                PendingEvent(
                    event_type=SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
                    idempotency_key="skladbot_daily_report:2026-07-06:chat-1:manual:daily_skladbot:v3",
                    status="completed",
                    payload={
                        "report_date": "2026-07-06",
                        "kind": "daily_skladbot",
                        "success": "true",
                    },
                    created_at=failed_at + timedelta(hours=2),
                    updated_at=failed_at + timedelta(hours=2),
                ),
            ])
            db.commit()

            self.assertEqual(count_unresolved_hot_path_failures(db), 0)

    def test_google_readiness_uses_aggregates_and_bounded_retry_scan(self):
        now = datetime(2026, 7, 10, 7, 0, tzinfo=timezone.utc)
        retry_at = now + timedelta(minutes=3)
        with self.SessionLocal() as db:
            db.add_all([
                PendingEvent(
                    event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
                    status="pending",
                    payload={"next_attempt_at": retry_at.isoformat()},
                    created_at=now - timedelta(minutes=20),
                ),
                PendingEvent(
                    event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
                    status="failed",
                    payload={},
                    last_error="quota",
                ),
            ])
            db.commit()
            statements = self.capture_selects()

            report = build_google_mirror_readiness(db, now=now)

        self.assertEqual(report["summary"], {"failed": 1, "pending": 1})
        self.assertEqual(report["next_attempt_at"], retry_at.isoformat())
        self.assertEqual(len(statements), 4)
        sql = "\n".join(statements).upper()
        self.assertIn("GROUP BY", sql)
        self.assertIn("MIN(", sql)
        self.assertIn("LIMIT", sql)

    def test_google_mutations_and_counts_do_not_scan_unrelated_entities(self):
        with self.SessionLocal() as db:
            db.add_all([
                PendingEvent(
                    event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
                    status="pending",
                    payload={"action": "target", "entity_id": "one"},
                ),
                *[
                    PendingEvent(
                        event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
                        status="pending",
                        payload={"action": "other", "entity_id": str(index)},
                    )
                    for index in range(50)
                ],
            ])
            db.commit()
            statements = self.capture_selects()

            self.assertEqual(mark_google_sheets_export_synced(db, "target", "one"), 1)
            db.flush()
            self.assertEqual(count_pending_export_events(db), 50)

        self.assertEqual(len(statements), 2)
        self.assertIn("JSON_EXTRACT", statements[0].upper())
        self.assertIn("COUNT(", statements[1].upper())

    def test_stale_reset_is_hard_batched_and_missing_order_ids_issue_no_query(self):
        stale_at = datetime.now(timezone.utc) - timedelta(hours=1)
        with self.SessionLocal() as db:
            db.add_all([
                PendingEvent(
                    event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
                    status="processing",
                    payload={"entity_id": str(index)},
                    updated_at=stale_at,
                )
                for index in range(STALE_PROCESSING_RESET_BATCH_SIZE + 5)
            ])
            db.commit()

            self.assertEqual(reset_stale_processing_export_events(db), STALE_PROCESSING_RESET_BATCH_SIZE)
            remaining = db.execute(
                select(PendingEvent).where(PendingEvent.status == "processing")
            ).scalars().all()
            self.assertEqual(len(remaining), 5)

        db = mock.Mock()
        self.assertEqual(load_skladbot_export_orders(db, {}), [])
        db.execute.assert_not_called()

    def test_cooldown_filters_expired_rows_before_bounded_scan(self):
        now = datetime(2026, 7, 10, 7, 0, tzinfo=timezone.utc)
        retry_at = now + timedelta(minutes=4)
        with self.SessionLocal() as db:
            db.add_all([
                PendingEvent(
                    event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
                    status="pending",
                    payload={"next_attempt_at": (now - timedelta(days=index + 1)).isoformat()},
                )
                for index in range(250)
            ])
            db.add(PendingEvent(
                event_type=GOOGLE_SHEETS_EXPORT_EVENT_TYPE,
                status="pending",
                payload={"next_attempt_at": retry_at.isoformat()},
            ))
            db.commit()

            self.assertEqual(google_sheets_export_cooldown_until(db, now=now), retry_at)


if __name__ == "__main__":
    unittest.main()
