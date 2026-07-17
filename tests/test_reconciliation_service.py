import unittest
from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app.models import Base, Incident, Order, OrderItem, PendingEvent
from backend.app.reconciliation_service import preview_daily_reconciliation, run_daily_reconciliation


class ReconciliationServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def add_order(self, db, *, status="not_completed", skladbot_status="", with_request=False):
        raw = {"skladbot_status": skladbot_status}
        if with_request:
            raw.update({"skladbot_request_number": "WH-R-TEST", "skladbot_request_id": "1"})
        order = Order(
            source="test",
            order_date=date(2026, 7, 16),
            payment_type="terminal",
            client="Test",
            address="Test",
            status=status,
            raw_payload=raw,
        )
        order.items.append(OrderItem(product="SKU", quantity_pieces=10, quantity_blocks=1, scanned_blocks=0))
        db.add(order)
        db.flush()
        return order

    def test_preview_is_postgres_and_does_not_mutate(self):
        with Session(self.engine) as db:
            self.add_order(db)
            db.commit()
            result = preview_daily_reconciliation(db=db, report_date="2026-07-16")
            self.assertEqual(result["source"], "postgres")
            self.assertEqual(result["status"], "action_required")
            self.assertNotIn("google", result)
            self.assertEqual(db.execute(select(Incident)).scalars().all(), [])

    def test_execute_upserts_skladbot_incident_and_dedupes_alert(self):
        with Session(self.engine) as db:
            self.add_order(db, with_request=True, skladbot_status="error")
            db.commit()
            first = run_daily_reconciliation(db=db, report_date="2026-07-16", alert_chat_ids=["1001"])
            second = run_daily_reconciliation(db=db, report_date="2026-07-16", alert_chat_ids=["1001"])
            self.assertEqual(first["status"], "action_required")
            self.assertEqual(first["alerts"][0]["status"], "queued")
            self.assertEqual(second["alerts"][0]["status"], "deduped")
            self.assertEqual(len(db.execute(select(Incident)).scalars().all()), 1)
            self.assertEqual(len(db.execute(select(PendingEvent)).scalars().all()), 1)

    def test_completed_orders_do_not_create_skladbot_gap(self):
        with Session(self.engine) as db:
            self.add_order(db, status="completed")
            db.commit()
            result = run_daily_reconciliation(db=db, report_date="2026-07-16")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["skladbot"]["missing_request_orders"], 0)


if __name__ == "__main__":
    unittest.main()
