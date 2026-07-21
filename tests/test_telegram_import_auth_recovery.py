from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import AuditLog, Base, ImportJob, Incident, PendingEvent
from tools import telegram_import_auth_recovery as recovery


class TelegramImportAuthRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        # retry() всегда берет реальное время, поэтому событие должно попадать
        # в RECENT_WINDOW относительно now, а не на фиксированную дату.
        self.now = datetime.now(timezone.utc)
        with self.Session() as db:
            event = PendingEvent(
                event_type="telegram_excel_import",
                status="failed",
                attempts=1,
                last_error=(
                    "Client error '401 Unauthorized' for url "
                    "'http://backend-api:8000/api/v1/imports'"
                ),
                payload={
                    "document": {"file_id": "synthetic-file-id"},
                    "shipment_date": "21.07.2026",
                },
                created_at=self.now - timedelta(hours=1),
            )
            db.add(event)
            db.flush()
            db.add(Incident(
                source="telegram_import",
                severity="critical",
                status="open",
                title="Telegram Excel import failed",
                pending_event_id=event.id,
            ))
            db.commit()
            self.event_id = event.id

    def tearDown(self):
        self.engine.dispose()

    def test_inspect_and_retry_are_exact_and_duplicate_safe(self):
        with self.Session() as db:
            summary = recovery.inspect(db, now=self.now)
            self.assertEqual(summary["event_id"], str(self.event_id))
            self.assertEqual(summary["linked_imports"], 0)
        with self.Session() as db:
            with self.assertRaisesRegex(recovery.RecoveryBlocked, "exact_retry_approval_required"):
                recovery.retry(db, self.event_id, approval="WRONG")
        with self.Session() as db:
            retried = recovery.retry(db, self.event_id, approval=recovery.APPROVAL)
            self.assertEqual(retried["event_status"], "pending")
        with self.Session() as db:
            event = db.get(PendingEvent, self.event_id)
            self.assertEqual(event.status, "pending")
            audits = list(db.execute(select(AuditLog)).scalars())
            self.assertTrue(any(row.action == "pending_event_retry_requested" for row in audits))

    def test_existing_import_or_unrelated_blocker_blocks_retry(self):
        with self.Session() as db:
            db.add(ImportJob(
                source="telegram",
                status="completed",
                rows_total=1,
                rows_imported=1,
                raw_payload={"telegram_event_id": str(self.event_id)},
            ))
            db.commit()
        with self.Session() as db, self.assertRaisesRegex(
            recovery.RecoveryBlocked, "existing_import_detected"
        ):
            recovery.inspect(db, self.event_id, now=datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc))

        with self.Session() as db:
            db.query(ImportJob).delete()
            db.add(PendingEvent(event_type="skladbot_request_create", status="failed", payload={}))
            db.commit()
        with self.Session() as db, self.assertRaisesRegex(
            recovery.RecoveryBlocked, "unrelated_hot_path_blocker_present"
        ):
            recovery.inspect(db, self.event_id, now=datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc))

    def test_verify_requires_one_import_and_finalize_resolves_incident(self):
        with self.Session() as db:
            event = db.get(PendingEvent, self.event_id)
            event.status = "completed"
            db.add(ImportJob(
                source="telegram",
                status="completed",
                rows_total=2,
                rows_imported=2,
                raw_payload={
                    "telegram_event_id": str(self.event_id),
                    "orders_created": 2,
                    "items_created": 2,
                },
            ))
            db.commit()
        with self.Session() as db:
            summary = recovery.finalize(db, self.event_id, approval=recovery.APPROVAL)
            self.assertEqual(summary["linked_imports"], 1)
            self.assertEqual(summary["orders_created"], 2)
            self.assertEqual(summary["incidents_resolved"], 1)
        with self.Session() as db:
            incident = db.execute(select(Incident)).scalar_one()
            self.assertEqual(incident.status, "resolved")
            self.assertIsNotNone(incident.resolved_at)
