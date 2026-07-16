import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from backend.app.event_leases import (
    DEPLOY_RECOVERABLE_EVENT_TYPES,
    build_postgres_claim_statement,
    cached_postgres_claim_statement,
    event_leases_enabled,
    recover_inflight_event_leases_after_worker_stop,
)
from backend.app.models import AuditLog, Base, PendingEvent


class EventLeaseContractTests(unittest.TestCase):
    def test_canary_flag_is_fail_closed_by_default(self):
        self.assertFalse(event_leases_enabled({}))
        self.assertTrue(event_leases_enabled({"TAKSKLAD_EVENT_LEASES_ENABLED": "1"}))
        self.assertFalse(event_leases_enabled({"TAKSKLAD_EVENT_LEASES_ENABLED": "0"}))

    def test_postgres_claim_is_one_update_returning_over_locked_candidates(self):
        now = datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc)
        statement = build_postgres_claim_statement(
            event_types=("google_sheets_export",),
            owner="worker-1",
            limit=50,
            now=now,
            expires_at=now + timedelta(minutes=30),
        )

        sql = str(statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )).upper()

        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertIn("UPDATE PENDING_EVENTS", sql)
        self.assertIn("RETURNING", sql)
        self.assertIn("AVAILABLE_AT", sql)
        self.assertIn("LEASE_EXPIRES_AT", sql)
        self.assertNotIn("PAYLOAD ->", sql)

    def test_postgres_claim_statement_shape_is_cached_without_owner_values(self):
        first = cached_postgres_claim_statement(
            event_type_count=1, limit=50, lease_duration_seconds=1800,
        )
        second = cached_postgres_claim_statement(
            event_type_count=1, limit=50, lease_duration_seconds=1800,
        )
        self.assertIs(first, second)
        sql = str(first.compile(dialect=postgresql.dialect()))
        self.assertIn("lease_owner", sql)
        self.assertIn("lease_event_type_0", sql)

    def test_deploy_recovery_only_requeues_owned_leases_after_workers_stop(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
        with Session(engine) as db:
            owned = PendingEvent(
                event_type="skladbot_request_create",
                status="processing",
                attempts=1,
                payload={},
                lease_owner="stopped-worker",
                lease_expires_at=now + timedelta(minutes=20),
                available_at=now - timedelta(minutes=1),
            )
            unowned = PendingEvent(
                event_type="telegram_notification",
                status="processing",
                attempts=1,
                payload={},
                lease_owner=None,
                available_at=now - timedelta(minutes=1),
            )
            unrelated = PendingEvent(
                event_type="smartup_auto_import_run",
                status="processing",
                attempts=1,
                payload={},
                lease_owner="other-worker",
                lease_expires_at=now + timedelta(minutes=20),
                available_at=now - timedelta(minutes=1),
            )
            db.add_all((owned, unowned, unrelated))
            db.commit()

            recovered = recover_inflight_event_leases_after_worker_stop(db, now=now)

            self.assertEqual(recovered, 1)
            db.refresh(owned)
            db.refresh(unowned)
            db.refresh(unrelated)
            self.assertEqual(owned.status, "pending")
            self.assertIsNone(owned.lease_owner)
            self.assertIsNone(owned.lease_expires_at)
            self.assertEqual(unowned.status, "processing")
            self.assertEqual(unrelated.status, "processing")
            audit = db.execute(
                select(AuditLog).where(AuditLog.action == "deploy_worker_lease_recovery")
            ).scalar_one()
            self.assertEqual(audit.payload["recovered"], 1)
            self.assertEqual(audit.payload["event_type_counts"], {"skladbot_request_create": 1})
        self.assertNotIn("google_sheets_export", DEPLOY_RECOVERABLE_EVENT_TYPES)
        self.assertIn("skladbot_request_create", DEPLOY_RECOVERABLE_EVENT_TYPES)


if __name__ == "__main__":
    unittest.main()
