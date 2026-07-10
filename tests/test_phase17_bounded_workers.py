import os
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.google_sheets_sync_worker import load_item_index, load_missing_reconciliation_batch
from backend.app.models import Base, Order, OrderItem, PendingEvent
from backend.app.skladbot_return_requests import (
    count_pending_skladbot_return_create_events,
    reset_stale_skladbot_return_create_events,
)
from backend.app.skladbot_worker import load_skladbot_sync_orders, skladbot_order_cursor_for_batch
from backend.app.smartup_saga import (
    SMARTUP_DEAL_SAGA_EVENT_TYPE,
    load_slot_sagas,
    mark_skladbot_results,
)


class Phase17BoundedWorkerTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    @staticmethod
    def pending_event(event_type, key, payload, status="pending"):
        return PendingEvent(
            event_type=event_type,
            action=event_type,
            aggregate_type="synthetic",
            aggregate_id=key,
            idempotency_key=key,
            status=status,
            payload=payload,
        )

    def test_smartup_saga_queries_are_filtered_by_current_identifiers_in_sql(self):
        statements = []
        event.listen(
            self.engine,
            "before_cursor_execute",
            lambda _conn, _cursor, statement, _params, _context, _many: statements.append(statement),
        )
        with self.SessionLocal() as db:
            saga = self.pending_event(
                SMARTUP_DEAL_SAGA_EVENT_TYPE,
                "saga-current",
                {
                    "import_id": "import-current",
                    "saga_state": "remote_confirmed",
                    "export_date": "2026-07-10",
                    "slot": "12:00",
                    "target_delivery_date": "2026-07-11",
                },
            )
            unrelated_saga = self.pending_event(
                SMARTUP_DEAL_SAGA_EVENT_TYPE,
                "saga-unrelated",
                {
                    "import_id": "import-unrelated",
                    "saga_state": "remote_confirmed",
                    "export_date": "2026-07-09",
                    "slot": "17:00",
                    "target_delivery_date": "2026-07-10",
                },
            )
            current_create = self.pending_event(
                "skladbot_request_create",
                "create-current",
                {"import_id": "import-current"},
            )
            unrelated_create = self.pending_event(
                "skladbot_request_create",
                "create-unrelated",
                {"import_id": "import-unrelated"},
            )
            db.add_all([saga, unrelated_saga, current_create, unrelated_create])
            db.commit()

            loaded = load_slot_sagas(
                db,
                export_date=date(2026, 7, 10),
                slot_label="12:00",
                target_delivery_date=date(2026, 7, 11),
            )
            statements.clear()
            mark_skladbot_results(db, [saga])

            self.assertEqual([item.id for item in loaded], [saga.id])
            self.assertEqual((saga.payload or {})["skladbot_event_keys"], ["create-current"])
            selects = "\n".join(statement for statement in statements if statement.lstrip().upper().startswith("SELECT"))
            self.assertIn("JSON_EXTRACT", selects.upper())
            self.assertIn("pending_events.payload", selects)

    def test_google_item_index_loads_only_current_sheet_identifiers(self):
        statements = []
        event.listen(
            self.engine,
            "before_cursor_execute",
            lambda _conn, _cursor, statement, _params, _context, _many: statements.append(statement),
        )
        with self.SessionLocal() as db:
            for index in range(6):
                order = Order(
                    order_date=date(2026, 7, 10),
                    payment_type="cash",
                    client=f"client-{index}",
                    address="address",
                    status="not_completed",
                    raw_payload={},
                )
                order.items = [OrderItem(
                    product="product",
                    quantity_blocks=1,
                    status="not_completed",
                    raw_payload={
                        "source_import_id": f"import-{index}",
                        "source_order_id": f"order-{index}",
                    },
                )]
                db.add(order)
            db.commit()
            db.expunge_all()

            index = load_item_index(db, [{
                "source_import_id": "import-3",
                "source_order_id": "order-3",
            }])

            self.assertEqual(set(index["source_import_id"]), {"import-3"})
            self.assertEqual(set(index["source_order_id"]), {"order-3"})
            loaded_orders = [value for value in db.identity_map.values() if isinstance(value, Order)]
            self.assertEqual(len(loaded_orders), 1)
        select_sql = "\n".join(statement for statement in statements if statement.lstrip().upper().startswith("SELECT"))
        self.assertIn("order_items.source_import_key IN", select_sql)

    def test_google_missing_reconciliation_reads_only_one_identity_bounded_batch(self):
        with self.SessionLocal() as db:
            for index in range(5):
                order = Order(
                    order_date=date(2026, 7, 10),
                    payment_type="cash",
                    client=f"client-{index}",
                    address="address",
                    status="not_completed",
                    raw_payload={},
                )
                order.items = [OrderItem(
                    product="product",
                    quantity_blocks=1,
                    status="not_completed",
                    source_import_id=f"import-{index}",
                    source_import_key=f"key-{index}",
                    raw_payload={"source_import_id": f"import-{index}"},
                )]
                db.add(order)
            identityless = Order(
                order_date=date(2026, 7, 10),
                payment_type="cash",
                client="identityless",
                address="address",
                status="not_completed",
                raw_payload={},
            )
            identityless.items = [OrderItem(
                product="product",
                quantity_blocks=1,
                status="not_completed",
                raw_payload={},
            )]
            db.add(identityless)
            db.commit()

            first = load_missing_reconciliation_batch(db, batch_size=2)
            second = load_missing_reconciliation_batch(db, cursor=str(first[-1].id), batch_size=2)

            self.assertEqual(len(first), 2)
            self.assertEqual(len(second), 2)
            self.assertTrue({item.id for item in first}.isdisjoint(item.id for item in second))
            self.assertNotIn(identityless.items[0].id, {item.id for item in [*first, *second]})

    def test_google_legacy_source_order_lookup_returns_one_item_for_every_requested_key(self):
        with self.SessionLocal() as db:
            for source_order_id, item_count in (("order-1", 2), ("order-2", 1)):
                order = Order(
                    order_date=date(2026, 7, 10),
                    payment_type="cash",
                    client=source_order_id,
                    address="address",
                    status="not_completed",
                    raw_payload={},
                )
                order.items = [
                    OrderItem(
                        product=f"product-{index}",
                        quantity_blocks=1,
                        status="not_completed",
                        raw_payload={"source_order_id": source_order_id},
                    )
                    for index in range(item_count)
                ]
                db.add(order)
            db.commit()

            index = load_item_index(db, [
                {"source_order_id": "order-1"},
                {"source_order_id": "order-2"},
            ])

            self.assertEqual(set(index["source_order_id"]), {"order-1", "order-2"})

    def test_skladbot_order_loader_uses_stable_bounded_batches(self):
        with self.SessionLocal() as db:
            for index in range(5):
                order = Order(
                    order_date=date(2026, 7, 10),
                    payment_type="cash",
                    client=f"client-{index}",
                    address="address",
                    status="not_completed",
                    raw_payload={},
                )
                order.items = [OrderItem(product="product", quantity_blocks=1, status="not_completed")]
                db.add(order)
            db.commit()

            first, _, _ = load_skladbot_sync_orders(db, limit=2, cursor="")
            second, _, _ = load_skladbot_sync_orders(
                db,
                limit=2,
                cursor=skladbot_order_cursor_for_batch(first),
            )

            self.assertEqual(len(first), 2)
            self.assertEqual(len(second), 2)
            self.assertTrue({order.id for order in first}.isdisjoint(order.id for order in second))

    def test_return_stale_reset_is_bounded_and_remaining_count_uses_sql(self):
        old = datetime.now(timezone.utc) - timedelta(hours=1)
        with self.SessionLocal() as db:
            db.add_all([
                self.pending_event(
                    "skladbot_return_request_create",
                    f"return-{index}",
                    {"order_id": f"order-{index}"},
                    status="processing",
                )
                for index in range(5)
            ])
            db.commit()
            for item in db.execute(select(PendingEvent)).scalars():
                item.updated_at = old
            db.commit()

            with mock.patch.dict(os.environ, {"SKLADBOT_RETURN_STALE_RESET_LIMIT": "2"}):
                reset = reset_stale_skladbot_return_create_events(db)

            self.assertEqual(reset, 2)
            self.assertEqual(count_pending_skladbot_return_create_events(db), 2)
            processing = db.execute(
                select(PendingEvent).where(PendingEvent.status == "processing")
            ).scalars().all()
            self.assertEqual(len(processing), 3)


if __name__ == "__main__":
    unittest.main()
