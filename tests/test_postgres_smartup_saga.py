import os
import unittest
from datetime import date
from unittest import mock

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from backend.app.models import PendingEvent
from backend.app.outbox_service import queue_outbox_event
from backend.app.smartup_saga import (
    SMARTUP_DEAL_SAGA_EVENT_TYPE,
    execute_status_sagas,
    mark_skladbot_results,
    prepare_deal_sagas,
    record_shadow_results,
    saga_report,
)
from tests.postgres_support import create_database, drop_database, run_alembic


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


class SyntheticSagaFault(RuntimeError):
    pass


class FakeSagaClient:
    smartup_saga_fake = True

    def __init__(self, statuses=None, *, fail_once=None, observer=None):
        self.statuses = dict(statuses or {})
        self.fail_once = set(fail_once or [])
        self.observer = observer
        self.change_calls = []
        self.read_calls = []

    def get_deal_statuses(self, deal_ids):
        self.read_calls.append(list(deal_ids))
        return {deal_id: self.statuses.get(deal_id, "") for deal_id in deal_ids}

    def change_status(self, deal_ids, status_code):
        if self.observer:
            self.observer()
        self.change_calls.append((list(deal_ids), status_code))
        successes = []
        errors = []
        for deal_id in deal_ids:
            if deal_id in self.fail_once:
                self.fail_once.remove(deal_id)
                errors.append({"deal_id": deal_id, "message": "synthetic locked"})
            else:
                self.statuses[deal_id] = status_code
                successes.append({"deal_id": deal_id})
        return {
            "successes": successes,
            "errors": errors,
            "submitted": len(deal_ids),
            "deal_ids": list(deal_ids),
            "successful_deal_ids": [item["deal_id"] for item in successes],
            "failed_deal_ids": [item["deal_id"] for item in errors],
            "status": status_code,
        }


def successful_ids(response, fallback):
    explicit = response.get("successful_deal_ids")
    if explicit is not None:
        return list(explicit)
    return [] if response.get("errors") else list(fallback)


def failed_ids(response):
    return list(response.get("failed_deal_ids") or [item.get("deal_id") for item in response.get("errors") or []])


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresSmartupSagaTests(unittest.TestCase):
    database_name = "taksklad_phase10_saga"

    @classmethod
    def setUpClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.url = create_database(cls.database_name)
        run_alembic(cls.url, "upgrade", "head")
        cls.engine = create_engine(cls.url, pool_pre_ping=True)
        cls.SessionLocal = sessionmaker(bind=cls.engine, autoflush=False, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        cls.engine.dispose()
        drop_database(cls.database_name)

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(text("TRUNCATE pending_events,audit_log RESTART IDENTITY CASCADE"))

    def import_snapshots(self, deal_ids=("deal-a", "deal-b"), *, suffix="one"):
        return [
            {
                "delivery_date": "2026-07-11",
                "import_id": f"00000000-0000-0000-0000-{index:012d}",
                "status": "completed",
                "rows_total": 1,
                "rows_imported": 1,
                "orders_created": 1,
                "items_created": 1,
                "duplicate_rows": 0,
                "invalid_rows": 0,
                "deal_ids": [deal_id],
                "source_suffix": suffix,
            }
            for index, deal_id in enumerate(deal_ids, start=1)
        ]

    def prepare(self, session, deal_ids=("deal-a", "deal-b"), *, mode="enforced", suffix="one"):
        return prepare_deal_sagas(
            session,
            self.import_snapshots(deal_ids, suffix=suffix),
            source_batch_key=f"synthetic-source-{suffix}",
            target_status="B#W",
            export_date=date(2026, 7, 10),
            slot_label="12:00",
            target_delivery_date=date(2026, 7, 11),
            mode=mode,
        )

    def execute(self, session, client, events):
        return execute_status_sagas(
            session,
            client,
            events,
            target_status="B#W",
            successful_ids=successful_ids,
            failed_ids=failed_ids,
        )

    def test_durable_intents_partial_retry_and_stable_skladbot_keys(self):
        observed_states = []

        def observe_committed_intents():
            with self.SessionLocal() as observer:
                observed_states.append({
                    event.aggregate_id: (event.payload or {}).get("saga_state")
                    for event in observer.execute(
                        select(PendingEvent)
                        .where(PendingEvent.event_type == SMARTUP_DEAL_SAGA_EVENT_TYPE)
                        .order_by(PendingEvent.aggregate_id)
                    ).scalars().all()
                })

        client = FakeSagaClient(
            {"deal-a": "B#N", "deal-b": "B#N"},
            fail_once={"deal-b"},
            observer=observe_committed_intents,
        )
        with self.SessionLocal() as session:
            events = self.prepare(session)
            first = self.execute(session, client, events)
            states = {event.aggregate_id: (event.payload or {}).get("saga_state") for event in events}
            self.assertEqual(states, {"deal-a": "remote_confirmed", "deal-b": "remote_failed"})
            self.assertEqual(first["failed_deal_ids"], ["deal-b"])

            second = self.execute(session, client, [events[1]])
            self.assertEqual(second["failed_deal_ids"], [])
            self.assertEqual(client.read_calls, [["deal-b"]])
            self.assertEqual(client.change_calls, [(["deal-a", "deal-b"], "B#W"), (["deal-b"], "B#W")])

            for event in events:
                import_id = (event.payload or {})["import_id"]
                queue_outbox_event(
                    session,
                    event_type="skladbot_request_create",
                    action="skladbot_request_create",
                    aggregate_type="order",
                    aggregate_id=f"synthetic-order-{event.aggregate_id}",
                    idempotency_key=f"skladbot:create:v1:order:synthetic-order-{event.aggregate_id}",
                    payload={"import_id": import_id},
                )
            session.commit()
            mark_skladbot_results(session, events)

            repeated = self.prepare(session, suffix="two")
            sagas = session.execute(
                select(PendingEvent).where(PendingEvent.event_type == SMARTUP_DEAL_SAGA_EVENT_TYPE)
            ).scalars().all()
            creates = session.execute(
                select(PendingEvent).where(PendingEvent.event_type == "skladbot_request_create")
            ).scalars().all()

        self.assertEqual(observed_states[0], {
            "deal-a": "remote_write_started",
            "deal-b": "remote_write_started",
        })
        self.assertEqual(observed_states[1]["deal-b"], "remote_write_started")
        self.assertEqual(len(sagas), 2)
        self.assertEqual({event.id for event in repeated}, {event.id for event in events})
        self.assertEqual(len(creates), 2)
        self.assertTrue(all((event.payload or {}).get("skladbot_event_count") == 1 for event in sagas))
        self.assertTrue(all((event.payload or {}).get("saga_state") == "skladbot_queued" for event in sagas))
        self.assertTrue(all(len(value) == 64 for value in saga_report(sagas, "enforced")["workflow_key_hashes"]))

    def test_remote_success_lost_response_reconciles_without_second_write(self):
        client = FakeSagaClient({"deal-a": "B#N"})
        fired = {"value": False}

        def fault(boundary, _deal_id):
            if boundary == "smartup_to_local" and not fired["value"]:
                fired["value"] = True
                raise SyntheticSagaFault("synthetic smartup_to_local")

        with self.SessionLocal() as session:
            events = self.prepare(session, ("deal-a",))
            with mock.patch("backend.app.smartup_saga.smartup_saga_fault", side_effect=fault):
                with self.assertRaises(SyntheticSagaFault):
                    self.execute(session, client, events)
            session.rollback()
            event = session.execute(
                select(PendingEvent).where(PendingEvent.event_type == SMARTUP_DEAL_SAGA_EVENT_TYPE)
            ).scalar_one()
            self.assertEqual((event.payload or {}).get("saga_state"), "remote_write_started")

            response = self.execute(session, client, [event])
            self.assertEqual(response["reconciled"], True)
            self.assertEqual(client.change_calls, [(["deal-a"], "B#W")])
            self.assertEqual(client.read_calls, [["deal-a"]])
            self.assertEqual((event.payload or {}).get("saga_state"), "remote_confirmed")

    def test_four_boundary_faults_land_in_recoverable_states(self):
        with self.SessionLocal() as session:
            with mock.patch(
                "backend.app.smartup_saga.smartup_saga_fault",
                side_effect=lambda boundary, _deal: (_ for _ in ()).throw(SyntheticSagaFault(boundary))
                if boundary == "import_to_intent" else None,
            ):
                with self.assertRaisesRegex(SyntheticSagaFault, "import_to_intent"):
                    self.prepare(session, ("deal-a",))
            session.rollback()
            self.assertEqual(session.execute(select(PendingEvent)).scalars().all(), [])
            events = self.prepare(session, ("deal-a",))
            self.assertEqual((events[0].payload or {}).get("saga_state"), "intent_persisted")

        client = FakeSagaClient({"deal-a": "B#N"})
        with self.SessionLocal() as session:
            event = session.execute(
                select(PendingEvent).where(PendingEvent.event_type == SMARTUP_DEAL_SAGA_EVENT_TYPE)
            ).scalar_one()
            with mock.patch(
                "backend.app.smartup_saga.smartup_saga_fault",
                side_effect=lambda boundary, _deal: (_ for _ in ()).throw(SyntheticSagaFault(boundary))
                if boundary == "intent_to_smartup" else None,
            ):
                with self.assertRaisesRegex(SyntheticSagaFault, "intent_to_smartup"):
                    self.execute(session, client, [event])
            session.rollback()
            event = session.get(PendingEvent, event.id)
            self.assertEqual((event.payload or {}).get("saga_state"), "remote_write_started")
            self.execute(session, client, [event])
            self.assertEqual((event.payload or {}).get("saga_state"), "remote_confirmed")

            with self.assertRaisesRegex(SyntheticSagaFault, "local_state_to_skladbot"):
                raise SyntheticSagaFault("local_state_to_skladbot")
            self.assertEqual((event.payload or {}).get("saga_state"), "remote_confirmed")
            queue_outbox_event(
                session,
                event_type="skladbot_request_create",
                action="skladbot_request_create",
                aggregate_type="order",
                aggregate_id="synthetic-order-deal-a",
                idempotency_key="skladbot:create:v1:order:synthetic-order-deal-a",
                payload={"import_id": (event.payload or {})["import_id"]},
            )
            session.commit()
            mark_skladbot_results(session, [event])
            self.assertEqual((event.payload or {}).get("saga_state"), "skladbot_queued")

    def test_shadow_records_observation_and_rollback_mode_has_no_saga(self):
        client = FakeSagaClient({"deal-a": "B#N"})
        with self.SessionLocal() as session:
            events = self.prepare(session, ("deal-a",), mode="shadow")
            response = client.change_status(["deal-a"], "B#W")
            record_shadow_results(session, events, response, successful_ids, failed_ids)
            report = saga_report(events, "shadow")

        self.assertEqual(report["mode"], "shadow")
        self.assertEqual(report["states"], {"shadow_observed": 1})
        self.assertEqual(client.change_calls, [(["deal-a"], "B#W")])
        self.assertEqual(saga_report([], "disabled"), {
            "mode": "disabled",
            "deals": 0,
            "states": {},
            "workflow_key_hashes": [],
        })


if __name__ == "__main__":
    unittest.main()
