import unittest
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.models import Base, Order, SmartupFulfillment, SmartupFulfillmentOrder
from backend.app.smartup_saga import (
    FulfillmentTransitionError,
    fulfillment_payload_hash,
    fulfillment_workflow_key,
    get_or_create_fulfillment,
    link_fulfillment_orders,
    transition_fulfillment,
)


class SmartupFulfillmentUnitTests(unittest.TestCase):
    def test_workflow_key_uses_business_identity_only(self):
        first = fulfillment_workflow_key(
            source_scope="smartup:project-a:filial-1",
            deal_id="deal-123",
            request_type="shipment",
            revision=1,
        )
        repeated = fulfillment_workflow_key(
            source_scope=" smartup:project-a:filial-1 ",
            deal_id=" deal-123 ",
            request_type=" SHIPMENT ",
            revision=1,
        )
        next_revision = fulfillment_workflow_key(
            source_scope="smartup:project-a:filial-1",
            deal_id="deal-123",
            request_type="shipment",
            revision=2,
        )

        self.assertEqual(first, repeated)
        self.assertTrue(first.startswith("smartup:fulfillment:v1:"))
        self.assertNotEqual(first, next_revision)
        self.assertNotIn("deal-123", first)

    def test_workflow_key_rejects_incomplete_identity(self):
        for field, value in (
            ("source_scope", ""),
            ("deal_id", ""),
            ("request_type", ""),
        ):
            values = {
                "source_scope": "smartup:project-a:filial-1",
                "deal_id": "deal-123",
                "request_type": "shipment",
                "revision": 1,
            }
            values[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                fulfillment_workflow_key(**values)

        with self.assertRaises(ValueError):
            fulfillment_workflow_key(
                source_scope="smartup:project-a:filial-1",
                deal_id="deal-123",
                request_type="shipment",
                revision=0,
            )

    def test_payload_hash_is_canonical(self):
        first = fulfillment_payload_hash({"deal_id": "deal-123", "items": [{"quantity": 2, "sku": "red"}]})
        reordered = fulfillment_payload_hash({"items": [{"sku": "red", "quantity": 2}], "deal_id": "deal-123"})
        changed = fulfillment_payload_hash({"deal_id": "deal-123", "items": [{"quantity": 3, "sku": "red"}]})

        self.assertEqual(first, reordered)
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, changed)

    def test_transition_enforces_state_machine_and_backoff_fields(self):
        fulfillment = SmartupFulfillment(
            workflow_key="smartup:fulfillment:v1:" + "a" * 64,
            source_scope="smartup:project-a:filial-1",
            deal_id="deal-123",
            request_type="shipment",
            revision=1,
            target_status="B#W",
            payload_hash="b" * 64,
            state="local_ready",
            raw_payload={},
        )
        retry_at = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)

        transition_fulfillment(None, fulfillment, "smartup_write_started")
        transition_fulfillment(
            None,
            fulfillment,
            "smartup_ambiguous",
            error="response lost",
            retry_at=retry_at,
            increment_retry=True,
        )

        self.assertEqual(fulfillment.state, "smartup_ambiguous")
        self.assertEqual(fulfillment.retry_attempts, 1)
        self.assertEqual(fulfillment.available_at, retry_at)
        self.assertEqual(fulfillment.last_error, "response lost")

        with self.assertRaises(FulfillmentTransitionError):
            transition_fulfillment(None, fulfillment, "skladbot_created")

    def test_sqlite_resolution_and_mapping_are_idempotent(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        try:
            with Session(engine, expire_on_commit=False) as session:
                order = Order(
                    source="smartup",
                    external_id="deal-123",
                    payment_type="terminal",
                    client="Synthetic client",
                    address="Synthetic address",
                    status="not_completed",
                    raw_payload={},
                )
                session.add(order)
                session.flush()
                first = get_or_create_fulfillment(
                    session,
                    source_scope="smartup:project-a:filial-1",
                    deal_id="deal-123",
                    target_status="B#W",
                    payload={"deal_id": "deal-123"},
                )
                second = get_or_create_fulfillment(
                    session,
                    source_scope="smartup:project-a:filial-1",
                    deal_id="deal-123",
                    target_status="B#W",
                    payload={"deal_id": "deal-123"},
                )
                first_links = link_fulfillment_orders(session, first, [order.id, order.id])
                second_links = link_fulfillment_orders(session, second, [order.id])
                session.commit()

                self.assertEqual(first.id, second.id)
                self.assertEqual([item.id for item in first_links], [item.id for item in second_links])
                self.assertEqual(session.query(SmartupFulfillment).count(), 1)
                self.assertEqual(session.query(SmartupFulfillmentOrder).count(), 1)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
