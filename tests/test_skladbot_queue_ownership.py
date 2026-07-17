import unittest
import uuid
from datetime import date
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import AuditLog, Base, Incident, Order, PendingEvent
from backend.app.outbox_service import queue_outbox_event as real_queue_outbox_event
from backend.app.skladbot_request_dry_run import (
    SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
    order_taksklad_marker,
    process_skladbot_create_event,
    queue_skladbot_create_events,
    skladbot_create_idempotency_key,
)
from backend.app.skladbot_return_requests import (
    SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE,
    process_skladbot_return_create_event,
    queue_skladbot_return_request_create,
    skladbot_return_create_idempotency_key,
)


class NoRemoteCallsClient:
    configured = True

    def __init__(self):
        self.create_calls = 0
        self.list_calls = 0
        self.detail_calls = 0

    def create_request(self, _payload):
        self.create_calls += 1
        raise AssertionError("unexpected create_request")

    def list_requests(self, **_kwargs):
        self.list_calls += 1
        raise AssertionError("unexpected list_requests")

    def get_request_detail(self, _request_id):
        self.detail_calls += 1
        raise AssertionError("unexpected get_request_detail")


class SkladBotQueueOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def add_order(self, db):
        order = Order(
            source="test",
            external_id=f"synthetic-{uuid.uuid4()}",
            order_date=date(2026, 7, 17),
            payment_type="Перечисление",
            client="Synthetic client",
            address="Synthetic address",
            representative="ТП1",
            status="not_completed",
            raw_payload={},
        )
        db.add(order)
        db.flush()
        return order

    @staticmethod
    def create_row(order_id):
        return {
            "status": "ready",
            "order_id": str(order_id),
            "payload": {
                "comment": "Перечисление\nТП1",
                "fields": {"comment": {"value": "Перечисление\nТП1"}},
                "products": [],
            },
        }

    @staticmethod
    def create_key(order):
        return skladbot_create_idempotency_key(
            str(order.id),
            marker=order_taksklad_marker(order),
        )

    @staticmethod
    def confirmed_items():
        return [{
            "item_id": str(uuid.uuid4()),
            "product": "Synthetic product",
            "sku": "Synthetic product",
            "quantity_blocks": 1,
            "quantity_pieces": 10,
        }]

    @staticmethod
    def add_event(
        db,
        *,
        event_type,
        aggregate_id,
        idempotency_key,
        payload_order_id,
        action=None,
        aggregate_type="order",
    ):
        event = PendingEvent(
            event_type=event_type,
            action=action if action is not None else event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            idempotency_key=idempotency_key,
            status="pending",
            attempts=0,
            payload={
                "version": 1,
                "action": action if action is not None else event_type,
                "entity_type": aggregate_type,
                "entity_id": aggregate_id,
                "idempotency_key": idempotency_key,
                "order_id": payload_order_id,
                "create_status": "queued",
            },
        )
        db.add(event)
        db.flush()
        return event

    def assert_conflict_audit(self, db, action, order_id):
        audits = db.execute(
            select(AuditLog)
            .where(AuditLog.action == action)
            .where(AuditLog.entity_id == str(order_id))
        ).scalars().all()
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].payload["reason"], "event_identity_conflict")
        self.assertEqual(set(audits[0].payload), {"reason"})

    def assert_conflict_incident(self, db, source, title, order_id):
        incidents = db.execute(
            select(Incident)
            .where(Incident.source == source)
            .where(Incident.title == title)
            .where(Incident.order_id == order_id)
        ).scalars().all()
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].status, "manual_review")
        self.assertEqual(incidents[0].raw_payload, {"reason": "event_identity_conflict"})

    def test_create_foreign_aggregate_with_target_key_is_blocked(self):
        with self.SessionLocal() as db:
            target = self.add_order(db)
            foreign = self.add_order(db)
            key = self.create_key(target)
            event = self.add_event(
                db,
                event_type=SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
                aggregate_id=str(foreign.id),
                idempotency_key=key,
                payload_order_id=str(foreign.id),
            )
            row = self.create_row(target.id)

            self.assertEqual(queue_skladbot_create_events(db, "synthetic-import", [row]), 0)
            self.assertEqual(row["status"], "blocked")
            self.assertIn("manual review required", row["error"])
            self.assertNotIn("skladbot_create_event_id", target.raw_payload)
            self.assertEqual(target.raw_payload.get("skladbot_status"), "manual_review")
            row["status"] = "ready"
            self.assertEqual(queue_skladbot_create_events(db, "synthetic-import", [row]), 0)
            self.assert_conflict_audit(db, "skladbot_request_create_identity_conflict", target.id)
            self.assert_conflict_incident(
                db,
                "skladbot_create",
                "SkladBot create queue identity conflict",
                target.id,
            )

            client = NoRemoteCallsClient()
            process_skladbot_create_event(db, event, client)
            self.assertEqual((client.create_calls, client.list_calls, client.detail_calls), (0, 0, 0))

    def test_create_target_aggregate_with_foreign_key_does_not_suppress_target(self):
        with self.SessionLocal() as db:
            target = self.add_order(db)
            foreign_key = skladbot_create_idempotency_key(str(uuid.uuid4()))
            corrupt = self.add_event(
                db,
                event_type=SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
                aggregate_id=str(target.id),
                idempotency_key=foreign_key,
                payload_order_id=str(target.id),
            )
            row = self.create_row(target.id)

            self.assertEqual(queue_skladbot_create_events(db, "synthetic-import", [row]), 1)
            expected_key = self.create_key(target)
            queued = db.execute(
                select(PendingEvent).where(PendingEvent.idempotency_key == expected_key)
            ).scalar_one()
            self.assertNotEqual(queued.id, corrupt.id)
            self.assertEqual(row["create_event_id"], str(queued.id))

    def test_create_same_key_wrong_kind_and_malformed_ownership_are_blocked(self):
        scenarios = (
            {"event_type": SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE},
            {"event_type": SKLADBOT_REQUEST_CREATE_EVENT_TYPE, "aggregate_id": "malformed"},
            {"event_type": SKLADBOT_REQUEST_CREATE_EVENT_TYPE, "aggregate_type": None},
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario), self.SessionLocal() as db:
                target = self.add_order(db)
                key = self.create_key(target)
                self.add_event(
                    db,
                    event_type=scenario["event_type"],
                    aggregate_id=scenario.get("aggregate_id", str(target.id)),
                    aggregate_type=scenario.get("aggregate_type", "order"),
                    idempotency_key=key,
                    payload_order_id=str(target.id),
                )
                row = self.create_row(target.id)

                self.assertEqual(queue_skladbot_create_events(db, "synthetic-import", [row]), 0)
                self.assertEqual(row["status"], "blocked")
                self.assertNotIn("skladbot_create_event_id", target.raw_payload)
                db.rollback()

    def test_create_exact_retry_is_idempotent(self):
        with self.SessionLocal() as db:
            order = self.add_order(db)
            first_row = self.create_row(order.id)
            second_row = self.create_row(order.id)

            self.assertEqual(queue_skladbot_create_events(db, "synthetic-import", [first_row]), 1)
            self.assertEqual(queue_skladbot_create_events(db, "synthetic-import", [second_row]), 0)
            self.assertEqual(second_row["status"], "queued")
            self.assertEqual(second_row["create_event_id"], first_row["create_event_id"])
            self.assertEqual(
                db.execute(select(PendingEvent).where(
                    PendingEvent.idempotency_key == self.create_key(order)
                )).scalars().all().__len__(),
                1,
            )

    def test_same_order_legacy_v1_pending_or_started_suppresses_new_v2(self):
        for post_state in ("", "started"):
            with self.subTest(post_state=post_state), self.SessionLocal() as db:
                order = self.add_order(db)
                legacy_key = skladbot_create_idempotency_key(str(order.id))
                legacy = self.add_event(
                    db,
                    event_type=SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
                    aggregate_id=str(order.id),
                    idempotency_key=legacy_key,
                    payload_order_id=str(order.id),
                )
                legacy.attempts = 1
                legacy.status = "processing" if post_state else "pending"
                legacy.payload = {**(legacy.payload or {}), "post_state": post_state}
                row = self.create_row(order.id)

                self.assertEqual(queue_skladbot_create_events(db, "synthetic-import", [row]), 0)
                self.assertEqual(row["create_event_id"], str(legacy.id))
                self.assertEqual(
                    db.execute(select(PendingEvent).where(
                        PendingEvent.aggregate_id == str(order.id)
                    )).scalars().all().__len__(),
                    1,
                )
                if post_state:
                    client = NoRemoteCallsClient()
                    result = process_skladbot_create_event(db, legacy, client)
                    self.assertEqual(result["status"], "ambiguous")
                    self.assertEqual((client.create_calls, client.list_calls, client.detail_calls), (0, 0, 0))
                db.rollback()

    def test_foreign_or_malformed_legacy_v1_is_never_reused(self):
        scenarios = ("foreign", "malformed_marker")
        for scenario in scenarios:
            with self.subTest(scenario=scenario), self.SessionLocal() as db:
                target = self.add_order(db)
                foreign = self.add_order(db)
                legacy_key = skladbot_create_idempotency_key(str(target.id))
                event = self.add_event(
                    db,
                    event_type=SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
                    aggregate_id=str(foreign.id) if scenario == "foreign" else str(target.id),
                    idempotency_key=legacy_key,
                    payload_order_id=str(foreign.id) if scenario == "foreign" else str(target.id),
                )
                if scenario == "malformed_marker":
                    event.payload = {
                        **(event.payload or {}),
                        "taksklad_marker": "TakSklad ref: malformed",
                    }
                row = self.create_row(target.id)

                self.assertEqual(queue_skladbot_create_events(db, "synthetic-import", [row]), 0)
                self.assertEqual(row["status"], "blocked")
                self.assertNotIn("skladbot_create_event_id", target.raw_payload)
                self.assert_conflict_audit(db, "skladbot_request_create_identity_conflict", target.id)
                db.rollback()

    def test_return_foreign_aggregate_with_target_key_is_manual_review(self):
        with self.SessionLocal() as db:
            target = self.add_order(db)
            foreign = self.add_order(db)
            key = skladbot_return_create_idempotency_key(str(target.id))
            event = self.add_event(
                db,
                event_type=SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE,
                aggregate_id=str(foreign.id),
                idempotency_key=key,
                payload_order_id=str(foreign.id),
            )

            queued = queue_skladbot_return_request_create(db, target, self.confirmed_items())
            self.assertIsNone(queued)
            self.assertEqual(target.raw_payload.get("skladbot_return_request_status"), "manual_review")
            self.assertIn("manual review required", target.raw_payload.get("skladbot_return_error", ""))
            self.assertNotIn("skladbot_return_create_event_id", target.raw_payload)
            self.assertIsNone(queue_skladbot_return_request_create(db, target, self.confirmed_items()))
            self.assert_conflict_audit(db, "skladbot_return_request_create_identity_conflict", target.id)
            self.assert_conflict_incident(
                db,
                "skladbot_return_create",
                "SkladBot return queue identity conflict",
                target.id,
            )

            client = NoRemoteCallsClient()
            process_skladbot_return_create_event(db, event, client)
            self.assertEqual((client.create_calls, client.list_calls, client.detail_calls), (0, 0, 0))

    def test_return_target_aggregate_with_foreign_key_does_not_suppress_target(self):
        with self.SessionLocal() as db:
            target = self.add_order(db)
            foreign_key = skladbot_return_create_idempotency_key(str(uuid.uuid4()))
            corrupt = self.add_event(
                db,
                event_type=SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE,
                aggregate_id=str(target.id),
                idempotency_key=foreign_key,
                payload_order_id=str(target.id),
            )

            queued = queue_skladbot_return_request_create(db, target, self.confirmed_items())
            self.assertIsNotNone(queued)
            self.assertNotEqual(queued.id, corrupt.id)
            self.assertEqual(queued.idempotency_key, skladbot_return_create_idempotency_key(str(target.id)))

    def test_return_same_key_wrong_kind_and_malformed_ownership_are_blocked(self):
        scenarios = (
            {"event_type": SKLADBOT_REQUEST_CREATE_EVENT_TYPE},
            {"event_type": SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE, "aggregate_id": "malformed"},
            {"event_type": SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE, "aggregate_type": None},
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario), self.SessionLocal() as db:
                target = self.add_order(db)
                key = skladbot_return_create_idempotency_key(str(target.id))
                self.add_event(
                    db,
                    event_type=scenario["event_type"],
                    aggregate_id=scenario.get("aggregate_id", str(target.id)),
                    aggregate_type=scenario.get("aggregate_type", "order"),
                    idempotency_key=key,
                    payload_order_id=str(target.id),
                )

                self.assertIsNone(queue_skladbot_return_request_create(db, target, self.confirmed_items()))
                self.assertEqual(target.raw_payload.get("skladbot_return_request_status"), "manual_review")
                self.assertNotIn("skladbot_return_create_event_id", target.raw_payload)
                db.rollback()

    def test_return_exact_retry_is_idempotent(self):
        with self.SessionLocal() as db:
            order = self.add_order(db)
            first = queue_skladbot_return_request_create(db, order, self.confirmed_items())
            second = queue_skladbot_return_request_create(db, order, self.confirmed_items())

            self.assertIsNotNone(first)
            self.assertEqual(second.id, first.id)
            self.assertEqual(
                db.execute(select(PendingEvent).where(
                    PendingEvent.idempotency_key == skladbot_return_create_idempotency_key(str(order.id))
                )).scalars().all().__len__(),
                1,
            )

    def test_unique_conflict_path_never_rebinds_foreign_create_or_return_event(self):
        cases = (
            (
                "backend.app.skladbot_request_dry_run.queue_outbox_event",
                SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
                self.create_key,
                lambda db, order: queue_skladbot_create_events(
                    db, "synthetic-import", [self.create_row(order.id)]
                ),
                "skladbot_create_event_id",
            ),
            (
                "backend.app.skladbot_return_requests.queue_outbox_event",
                SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE,
                skladbot_return_create_idempotency_key,
                lambda db, order: queue_skladbot_return_request_create(
                    db, order, self.confirmed_items()
                ),
                "skladbot_return_create_event_id",
            ),
        )
        for patch_target, event_type, key_builder, queue_call, pointer in cases:
            with self.subTest(event_type=event_type), self.SessionLocal() as db:
                target = self.add_order(db)
                foreign = self.add_order(db)
                expected_key = (
                    key_builder(target)
                    if event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE
                    else key_builder(str(target.id))
                )
                captured = {}

                def inject_conflict(session, **kwargs):
                    event = self.add_event(
                        session,
                        event_type=event_type,
                        aggregate_id=str(foreign.id),
                        idempotency_key=expected_key,
                        payload_order_id=str(foreign.id),
                    )
                    captured["event"] = event
                    return real_queue_outbox_event(session, **kwargs)

                with mock.patch(patch_target, side_effect=inject_conflict):
                    queue_call(db, target)

                self.assertNotIn(pointer, target.raw_payload)
                self.assertEqual(
                    db.execute(select(PendingEvent).where(PendingEvent.idempotency_key == expected_key)).scalar_one().id,
                    captured["event"].id,
                )
                client = NoRemoteCallsClient()
                if event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE:
                    process_skladbot_create_event(db, captured["event"], client)
                else:
                    process_skladbot_return_create_event(db, captured["event"], client)
                self.assertEqual((client.create_calls, client.list_calls, client.detail_calls), (0, 0, 0))
                db.rollback()

    def test_generic_outbox_default_preserves_legacy_reuse_and_backfill(self):
        with self.SessionLocal() as db:
            legacy = PendingEvent(
                event_type="legacy_producer",
                action=None,
                aggregate_type=None,
                aggregate_id=None,
                idempotency_key="synthetic:legacy:default",
                status="pending",
                attempts=0,
                payload={"legacy_payload": True},
            )
            db.add(legacy)
            db.flush()

            reused = real_queue_outbox_event(
                db,
                event_type="legacy_producer",
                action="legacy_action",
                aggregate_type="legacy_entity",
                aggregate_id="legacy-id",
                idempotency_key="synthetic:legacy:default",
                payload={"new_payload": True},
            )

            self.assertEqual(reused.id, legacy.id)
            self.assertEqual(reused.action, "legacy_action")
            self.assertEqual(reused.aggregate_type, "legacy_entity")
            self.assertEqual(reused.aggregate_id, "legacy-id")
            self.assertIs(reused.payload.get("legacy_payload"), True)
            self.assertNotIn("new_payload", reused.payload)


if __name__ == "__main__":
    unittest.main()
