import os
import threading
import unittest
import uuid
from datetime import date, datetime, timezone
from unittest import mock

from sqlalchemy import create_engine, event as sqlalchemy_event
from sqlalchemy.orm import sessionmaker

from backend.app.models import Order, OrderItem, PendingEvent
from backend.app.skladbot_request_dry_run import (
    SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
    process_pending_skladbot_request_creates,
    skladbot_create_idempotency_key,
)
from backend.app.skladbot_return_requests import (
    process_pending_skladbot_return_request_creates,
    queue_skladbot_return_request_create,
)
from tests.postgres_support import create_database, drop_database, run_alembic


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresSkladBotNonLeaseConcurrencyTests(unittest.TestCase):
    database_name = "taksklad_skladbot_nonlease_concurrency"

    @classmethod
    def setUpClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.url = create_database(cls.database_name)
        run_alembic(cls.url, "upgrade", "head")
        cls.engine = create_engine(cls.url, pool_pre_ping=True)
        cls.SessionLocal = sessionmaker(bind=cls.engine, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.engine.dispose()
        drop_database(cls.database_name)

    def setUp(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("TRUNCATE pending_events, order_items, orders CASCADE")

    def test_order_retry_scheduled_overlap_posts_once(self):
        self._assert_overlap_posts_once(return_create=False)

    def test_return_retry_scheduled_overlap_posts_once(self):
        self._assert_overlap_posts_once(return_create=True)

    def _assert_overlap_posts_once(self, *, return_create):
        with self.SessionLocal() as db:
            order = Order(
                source="test",
                external_id=f"synthetic-{uuid.uuid4()}",
                order_date=date(2026, 7, 17),
                payment_type="Перечисление",
                client="Synthetic client",
                address="Synthetic address",
                representative="ТП1",
                status="returned" if return_create else "not_completed",
                raw_payload={},
            )
            db.add(order)
            db.flush()
            item = OrderItem(
                order_id=order.id,
                product="Chapman RED OP 20",
                quantity_pieces=20,
                quantity_blocks=2,
                pieces_per_block=10,
                scanned_blocks=0,
                status="not_completed",
                raw_payload={},
            )
            db.add(item)
            db.flush()
            if return_create:
                confirmed = [{
                    "item_id": str(item.id),
                    "product": item.product,
                    "sku": item.product,
                    "quantity_blocks": 2,
                    "quantity_pieces": 20,
                }]
                event = queue_skladbot_return_request_create(db, order, confirmed)
            else:
                event = PendingEvent(
                    event_type=SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
                    action=SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
                    aggregate_type="order",
                    aggregate_id=str(order.id),
                    idempotency_key=skladbot_create_idempotency_key(str(order.id)),
                    status="pending",
                    attempts=1,
                    payload={"order_id": str(order.id), "post_state": "retry_scheduled"},
                    available_at=datetime.now(timezone.utc),
                )
                db.add(event)
            event.attempts = 1
            event.status = "pending"
            event.available_at = datetime.now(timezone.utc)
            event.payload = {**(event.payload or {}), "post_state": "retry_scheduled"}
            db.commit()
            event_id = event.id

        claim_barrier = threading.Barrier(2)
        errors = []
        results = []
        result_lock = threading.Lock()

        class RacingClient:
            configured = True

            def __init__(self):
                self.create_calls = 0
                self.lock = threading.Lock()

            def create_request(self, payload):
                with self.lock:
                    request_id = (7300 if return_create else 7400) + self.create_calls
                    self.create_calls += 1
                return {"data": {"id": request_id}}

            def list_requests(self, type_id=None):
                raise AssertionError("non-lease claim proof must not require fuzzy lookup")

            def get_request_detail(self, request_id):
                return {"id": request_id, "delivery_number": f"WH-R-{request_id}"}

        client = RacingClient()

        def synchronize_locked_claim(_conn, _cursor, _statement, _parameters, context, _executemany):
            if context.execution_options.get("taksklad_nonlease_claim"):
                claim_barrier.wait(timeout=10)

        sqlalchemy_event.listen(self.engine, "after_cursor_execute", synchronize_locked_claim)

        def worker():
            try:
                with self.SessionLocal() as db:
                    if return_create:
                        result = process_pending_skladbot_return_request_creates(db, client=client, limit=1)
                    else:
                        result = process_pending_skladbot_request_creates(db, client=client, limit=1)
                with result_lock:
                    results.append(result)
            except Exception as exc:  # pragma: no cover - asserted below
                with result_lock:
                    errors.append(exc)

        with mock.patch.dict(
            "os.environ",
            {"TAKSKLAD_EVENT_LEASES_ENABLED": "0", "SKLADBOT_CREATE_REQUESTS_MODE": "enabled"},
            clear=False,
        ):
            threads = [threading.Thread(target=worker) for _index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
        sqlalchemy_event.remove(self.engine, "after_cursor_execute", synchronize_locked_claim)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(result["checked"] for result in results), [0, 1])
        self.assertEqual(sum(result["created"] for result in results), 1)
        self.assertEqual(client.create_calls, 1)
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
        self.assertEqual(event.status, "completed")
        self.assertEqual(event.attempts, 2)


if __name__ == "__main__":
    unittest.main()
