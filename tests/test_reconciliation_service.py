import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import AuditLog, Base, Incident, Order, OrderItem, PendingEvent
from backend.app.reconciliation_service import parse_report_date, preview_daily_reconciliation, run_daily_reconciliation


class ReconciliationServiceTests(unittest.TestCase):
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

    def add_order(
        self,
        db,
        *,
        order_date=date(2026, 6, 10),
        client="Reconcile Client",
        status="not_completed",
        source_import_id="import-1",
        source_order_id="order-1",
        skladbot_request_number="WH-R-1",
        skladbot_request_id="1",
        skladbot_status="found",
    ):
        order = Order(
            payment_type="Перечисление",
            client=client,
            address="Ташкент, тестовый адрес",
            representative="ТП1 Test",
            order_date=order_date,
            status=status,
            raw_payload={
                "skladbot_request_number": skladbot_request_number,
                "skladbot_request_id": skladbot_request_id,
                "skladbot_status": skladbot_status,
            },
        )
        item = OrderItem(
            order=order,
            product="Chapman RED OP 20",
            quantity_pieces=10,
            quantity_blocks=1,
            pieces_per_block=10,
            scanned_blocks=0,
            status="not_completed",
            raw_payload={
                "source_import_id": source_import_id,
                "source_order_id": source_order_id,
                "block_price": 240000,
                "line_total": 240000,
            },
        )
        db.add(order)
        db.flush()
        return order, item

    def google_record(self, **overrides):
        record = {
            "row_number": 2,
            "source_sheet": "data",
            "source_import_id": "import-1",
            "source_order_id": "order-1",
            "order_date": date(2026, 6, 10),
            "payment_type": "Перечисление",
            "client": "Reconcile Client",
            "address": "Ташкент, тестовый адрес",
            "representative": "ТП1 Test",
            "product": "Chapman RED OP 20",
            "quantity_pieces": 10,
            "quantity_blocks": 1,
            "status": "Не выполнено",
            "skladbot_request_number": "WH-R-1",
            "skladbot_request_id": "1",
            "skladbot_status": "Найдено",
        }
        record.update(overrides)
        return record

    def test_reconciliation_counts_drift_creates_incidents_and_dedupes_alerts(self):
        with self.SessionLocal() as db:
            self.add_order(db, source_import_id="import-match", source_order_id="order-match")
            self.add_order(
                db,
                client="DB Only Client",
                source_import_id="import-db-only",
                source_order_id="order-db-only",
                skladbot_request_number="",
                skladbot_request_id="",
                skladbot_status="",
            )
            db.commit()

            google_records = [
                self.google_record(
                    row_number=2,
                    source_import_id="import-match",
                    source_order_id="order-match",
                    status="Выполнено",
                    skladbot_request_number="",
                    skladbot_request_id="",
                ),
                self.google_record(
                    row_number=3,
                    source_import_id="import-google-only",
                    source_order_id="order-google-only",
                    client="Google Only Client",
                ),
            ]

            first = run_daily_reconciliation(
                db=db,
                report_date=date(2026, 6, 10),
                google_records=google_records,
                alert_chat_ids=["-5271267499"],
            )
            first_events = db.execute(select(PendingEvent).where(PendingEvent.event_type == "telegram_notification")).scalars().all()
            first_event_ids = {str(event.id) for event in first_events}

            second = run_daily_reconciliation(
                db=db,
                report_date=date(2026, 6, 10),
                google_records=google_records,
                alert_chat_ids=["-5271267499"],
            )
            second_events = db.execute(select(PendingEvent).where(PendingEvent.event_type == "telegram_notification")).scalars().all()
            incidents = db.execute(select(Incident).where(Incident.source == "daily_reconciliation")).scalars().all()

        self.assertEqual(first["source"], "postgres")
        self.assertEqual(first["status"], "action_required")
        self.assertEqual(first["google"]["google_only_rows"], 1)
        self.assertEqual(first["google"]["db_only_active_items"], 1)
        self.assertEqual(first["google"]["status_mismatches"], 1)
        self.assertEqual(first["google"]["wh_r_mismatches"], 2)
        self.assertEqual(first["skladbot"]["missing_request_orders"], 1)
        self.assertEqual(first["skladbot"]["problem_status_orders"], 0)
        self.assertEqual(len(first["incidents"]), 2)
        self.assertEqual({incident.severity for incident in incidents}, {"critical"})
        self.assertEqual(
            {incident.external_ref for incident in incidents},
            {
                "reconciliation:2026-06-10:google_mirror_mismatch",
                "reconciliation:2026-06-10:skladbot_gap",
            },
        )
        self.assertEqual(len(first_events), 2)
        self.assertEqual(len(second_events), 2)
        self.assertEqual({str(event.id) for event in second_events}, first_event_ids)
        self.assertTrue(all(alert["status"] == "deduped" for alert in second["alerts"]))
        for event in first_events:
            payload = event.payload or {}
            self.assertEqual(payload["chat_id"], "-5271267499")
            self.assertIn("Что сделать:", payload["text"])
            self.assertIn("daily_reconciliation", payload["text"])

    def test_preview_reports_candidates_without_database_mutation(self):
        with self.SessionLocal() as db:
            self.add_order(
                db,
                source_import_id="preview-import",
                source_order_id="preview-order",
                skladbot_request_number="",
                skladbot_request_id="",
                skladbot_status="",
            )
            db.commit()
            before = {
                "incidents": db.query(Incident).count(),
                "events": db.query(PendingEvent).count(),
                "audits": db.query(AuditLog).count(),
            }

            result = preview_daily_reconciliation(
                db=db,
                report_date=date(2026, 6, 10),
                google_records=[],
            )
            db.rollback()
            after = {
                "incidents": db.query(Incident).count(),
                "events": db.query(PendingEvent).count(),
                "audits": db.query(AuditLog).count(),
            }

        self.assertEqual(result["mode"], "preview")
        self.assertEqual(result["status"], "action_required")
        self.assertTrue(result["incidents"])
        self.assertEqual(result["alerts"], [])
        self.assertEqual(after, before)

    def test_reconciliation_google_down_records_mirror_issue_without_failed_db_workflow(self):
        with self.SessionLocal() as db:
            self.add_order(db)
            db.commit()

            result = run_daily_reconciliation(
                db=db,
                report_date="10.06.2026",
                google_error="APIError: [429] token=secret 0104006396053978217SECRETKIZVALUE",
                alert_chat_ids=["123"],
            )
            incidents = db.execute(select(Incident).where(Incident.source == "daily_reconciliation")).scalars().all()
            notifications = db.execute(select(PendingEvent).where(PendingEvent.event_type == "telegram_notification")).scalars().all()

        self.assertEqual(result["source"], "postgres")
        self.assertEqual(result["status"], "mirror_issue")
        self.assertEqual(result["google"]["status"], "error")
        self.assertEqual(result["skladbot"]["missing_request_orders"], 0)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].severity, "warning")
        self.assertEqual(incidents[0].external_ref, "reconciliation:2026-06-10:google_mirror_unavailable")
        self.assertEqual(notifications, [])
        dumped = str(result) + str(incidents[0].raw_payload)
        self.assertNotIn("secret", dumped)
        self.assertNotIn("0104006396053978217SECRETKIZVALUE", dumped)

    def test_returned_order_with_completed_google_status_is_not_status_mismatch(self):
        with self.SessionLocal() as db:
            self.add_order(
                db,
                status="returned",
                source_import_id="import-returned",
                source_order_id="order-returned",
                skladbot_request_number="WH-R-BACKEND",
                skladbot_request_id="1001",
                skladbot_status="found",
            )
            db.commit()

            result = run_daily_reconciliation(
                db=db,
                report_date=date(2026, 6, 10),
                google_records=[
                    self.google_record(
                        source_import_id="import-returned",
                        source_order_id="order-returned",
                        status="Выполнено",
                        skladbot_request_number="WH-R-GOOGLE",
                        skladbot_request_id="2002",
                        skladbot_status="Создано",
                    ),
                ],
                alert_chat_ids=["123"],
            )
            incidents = db.execute(select(Incident).where(Incident.source == "daily_reconciliation")).scalars().all()
            notifications = db.execute(select(PendingEvent).where(PendingEvent.event_type == "telegram_notification")).scalars().all()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["google"]["status_mismatches"], 0)
        self.assertEqual(result["google"]["wh_r_mismatches"], 0)
        self.assertEqual(incidents, [])
        self.assertEqual(notifications, [])

    def test_completed_item_in_active_multi_sku_order_matches_completed_google_row(self):
        with self.SessionLocal() as db:
            order, completed_item = self.add_order(
                db,
                source_import_id="import-completed",
                source_order_id="order-completed",
            )
            completed_item.status = "completed"
            completed_item.scanned_blocks = 1
            open_item = OrderItem(
                order=order,
                product="Chapman Brown SSL 100`20",
                quantity_pieces=10,
                quantity_blocks=1,
                pieces_per_block=10,
                scanned_blocks=0,
                status="not_completed",
                raw_payload={
                    "source_import_id": "import-open",
                    "source_order_id": "order-open",
                    "block_price": 240000,
                    "line_total": 240000,
                },
            )
            db.add(open_item)
            db.commit()

            result = run_daily_reconciliation(
                db=db,
                report_date=date(2026, 6, 10),
                google_records=[
                    self.google_record(
                        source_import_id="import-completed",
                        source_order_id="order-completed",
                        status="Выполнено",
                    ),
                    self.google_record(
                        row_number=3,
                        source_import_id="import-open",
                        source_order_id="order-open",
                        product="Chapman Brown SSL 100`20",
                        status="Не выполнено",
                    ),
                ],
                alert_chat_ids=["123"],
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["google"]["status_mismatches"], 0)
        self.assertEqual(result["google"]["db_only_active_items"], 0)
        self.assertEqual(result["google"]["google_only_rows"], 0)

    def test_default_reconciliation_report_date_uses_business_timezone(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 6, 21, 20, 30, tzinfo=timezone.utc)
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)

        with patch("backend.app.reconciliation_service.datetime", FixedDateTime):
            self.assertEqual(parse_report_date(None), date(2026, 6, 22))

    def test_reconciliation_aggregates_skladbot_problem_status_without_row_spam(self):
        with self.SessionLocal() as db:
            self.add_order(
                db,
                source_import_id="import-pending",
                source_order_id="order-pending",
                skladbot_request_number="WH-R-PENDING",
                skladbot_request_id="1001",
                skladbot_status="pending",
            )
            self.add_order(
                db,
                client="Problem Client 2",
                source_import_id="import-error",
                source_order_id="order-error",
                skladbot_request_number="WH-R-ERROR",
                skladbot_request_id="1002",
                skladbot_status="error",
            )
            db.commit()

            google_records = [
                self.google_record(
                    row_number=2,
                    source_import_id="import-pending",
                    source_order_id="order-pending",
                    skladbot_request_number="WH-R-PENDING",
                    skladbot_request_id="1001",
                    skladbot_status="Проверяется",
                ),
                self.google_record(
                    row_number=3,
                    client="Problem Client 2",
                    source_import_id="import-error",
                    source_order_id="order-error",
                    skladbot_request_number="WH-R-ERROR",
                    skladbot_request_id="1002",
                    skladbot_status="Ошибка синхронизации",
                ),
            ]
            result = run_daily_reconciliation(
                db=db,
                report_date=date(2026, 6, 10),
                google_records=google_records,
                alert_chat_ids=["123"],
            )
            incidents = db.execute(select(Incident).where(Incident.source == "daily_reconciliation")).scalars().all()
            notifications = db.execute(select(PendingEvent).where(PendingEvent.event_type == "telegram_notification")).scalars().all()

        self.assertEqual(result["status"], "action_required")
        self.assertEqual(result["google"]["google_only_rows"], 0)
        self.assertEqual(result["google"]["db_only_active_items"], 0)
        self.assertEqual(result["google"]["status_mismatches"], 0)
        self.assertEqual(result["google"]["wh_r_mismatches"], 0)
        self.assertEqual(result["skladbot"]["missing_request_orders"], 0)
        self.assertEqual(result["skladbot"]["problem_status_orders"], 2)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].external_ref, "reconciliation:2026-06-10:skladbot_gap")
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].payload["incident_id"], str(incidents[0].id))


if __name__ == "__main__":
    unittest.main()
