import os
import tempfile
import unittest
import uuid
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.models import Base, Order, OrderItem, PendingEvent
from backend.app.skladbot_request_dry_run import (
    process_skladbot_create_event,
    queue_skladbot_create_events,
)


class SimulatedConnectionLoss(BaseException):
    """Обрыв соединения посреди чтения: всё незакоммиченное теряется."""


class RemoteReadProbeClient:
    configured = True

    def __init__(self, on_detail=None, *, response_id=7001, detail_number="WH-R-7001"):
        self.on_detail = on_detail
        self.response_id = response_id
        self.detail_number = detail_number
        self.create_calls = 0
        self.detail_calls = []

    def create_request(self, _payload):
        self.create_calls += 1
        return {"data": {"id": self.response_id}}

    def get_request_detail(self, request_id):
        self.detail_calls.append(request_id)
        if self.on_detail is not None:
            self.on_detail(request_id)
        return {"id": request_id, "delivery_number": self.detail_number}

    def list_requests(self, **_kwargs):
        return []


class SkladBotCreateCommitBeforeRemoteReadTests(unittest.TestCase):
    """28.09.2026: номер созданной заявки терялся, пока транзакция ждала медленный GET."""

    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.engine = create_engine(f"sqlite+pysqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        # как в backend/app/db.py: expire_on_commit по умолчанию, autoflush выключен
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        os.unlink(self.db_path)

    def seed_create_event(self):
        with self.SessionLocal() as db:
            order = Order(
                id=uuid.uuid4(),
                source="test",
                external_id=f"synthetic-{uuid.uuid4()}",
                order_date=date(2026, 9, 29),
                payment_type="Перечисление",
                client="Synthetic client",
                address="Synthetic address",
                representative="ТП1",
                status="not_completed",
                raw_payload={},
            )
            order.items.append(OrderItem(
                product="Chapman RED OP 20",
                quantity_pieces=10,
                quantity_blocks=1,
                pieces_per_block=10,
                scanned_blocks=0,
                status="not_completed",
                raw_payload={"synthetic": True},
            ))
            db.add(order)
            db.flush()
            row = {
                "status": "ready",
                "order_id": str(order.id),
                "payload": {
                    "comment": "Перечисление\nТП1",
                    "fields": {"comment": {"value": "Перечисление\nТП1"}},
                    "products": [],
                },
            }
            self.assertEqual(queue_skladbot_create_events(db, "synthetic-import", [row]), 1)
            db.commit()
            return order.id, uuid.UUID(row["create_event_id"])

    def stored_event_payload(self, event_id):
        with self.SessionLocal() as other:
            return dict(other.get(PendingEvent, event_id).payload or {})

    def test_detail_read_after_post_runs_without_open_transaction_and_sees_durable_id(self):
        _order_id, event_id = self.seed_create_event()
        seen = {}
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)

            def probe(_request_id):
                seen["in_transaction"] = db.in_transaction()
                seen["stored"] = self.stored_event_payload(event_id)

            client = RemoteReadProbeClient(probe)
            result = process_skladbot_create_event(db, event, client)

        self.assertEqual(result["status"], "created")
        self.assertEqual(client.detail_calls, [7001])
        self.assertFalse(seen["in_transaction"])
        self.assertEqual(seen["stored"].get("post_state"), "response_received")
        self.assertEqual(str(seen["stored"].get("post_response_request_id")), "7001")

    def test_connection_loss_during_detail_read_recovers_by_durable_id_without_second_post(self):
        order_id, event_id = self.seed_create_event()

        def lose_connection(_request_id):
            raise SimulatedConnectionLoss()

        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            first = RemoteReadProbeClient(lose_connection)
            with self.assertRaises(SimulatedConnectionLoss):
                process_skladbot_create_event(db, event, first)
            db.rollback()
        self.assertEqual(first.create_calls, 1)

        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            event.status = "processing"
            event.attempts = 2
            db.commit()
            second = RemoteReadProbeClient()
            result = process_skladbot_create_event(db, event, second)
            db.commit()

        self.assertEqual(result["status"], "created_recovered")
        self.assertEqual(result["request_number"], "WH-R-7001")
        self.assertEqual(second.create_calls, 0)
        self.assertEqual(second.detail_calls, [7001])
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            self.assertEqual(order.raw_payload.get("skladbot_request_number"), "WH-R-7001")

    def test_reconcile_detail_read_on_retry_runs_without_open_transaction(self):
        _order_id, event_id = self.seed_create_event()
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            event.payload = {
                **(event.payload or {}),
                "post_state": "response_received",
                "post_response_request_id": 7001,
            }
            event.status = "processing"
            event.attempts = 2
            db.commit()

        seen = {}
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)

            def probe(_request_id):
                seen["in_transaction"] = db.in_transaction()

            client = RemoteReadProbeClient(probe)
            result = process_skladbot_create_event(db, event, client)

        self.assertEqual(result["status"], "created_recovered")
        self.assertEqual(client.create_calls, 0)
        self.assertFalse(seen["in_transaction"])


if __name__ == "__main__":
    unittest.main()
