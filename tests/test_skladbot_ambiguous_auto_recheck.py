import os
import tempfile
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.models import AuditLog, Base, Incident, Order, OrderItem, PendingEvent
from backend.app.skladbot_ambiguous_recheck import (
    SKLADBOT_AMBIGUOUS_AUTO_RECHECK_ENV,
    recheck_ambiguous_skladbot_creates,
)
from backend.app.skladbot_contracts import business_today, normalize_request_payload, request_match_diagnostics
from backend.app.skladbot_request_dry_run import (
    default_create_processing_result,
    finish_skladbot_create_event,
    process_pending_skladbot_request_creates,
    process_skladbot_create_event,
    queue_skladbot_create_events,
    stable_payload_hash,
)
from backend.app.skladbot_worker_runner import run_worker_cycle


ENABLED = {SKLADBOT_AMBIGUOUS_AUTO_RECHECK_ENV: "true"}
ORDER_DATE = date(2026, 10, 15)
CLIENT = '"SYNTHETIC MARKET" MCHJ'
PAYMENT = "Перечисление"
PRODUCT = "Chapman RED OP 20"
SECOND_PRODUCT = "Chapman BROWN OP 20"


class TimeoutOnPostClient:
    """POST уходит в таймаут: исход на стороне SkladBot неизвестен."""

    configured = True

    def create_request(self, _payload):
        raise TimeoutError("SkladBot API POST timeout")

    def get_request_detail(self, request_id):
        raise AssertionError(f"detail read is not expected: {request_id}")

    def list_requests(self, **_kwargs):
        return []


class RecheckClient:
    configured = True

    def __init__(self, items=(), details=None, *, list_error=None, create_response_id=None):
        self.items = list(items)
        self.details = dict(details or {})
        self.list_error = list_error
        self.create_response_id = create_response_id
        self.list_calls = []
        self.detail_calls = []
        self.create_calls = 0

    def list_requests(self, type_id=None, limit=None):
        self.list_calls.append(limit)
        if self.list_error is not None:
            raise self.list_error
        return list(self.items)

    def get_request_detail(self, request_id):
        self.detail_calls.append(int(request_id))
        detail = self.details.get(int(request_id))
        if isinstance(detail, Exception):
            raise detail
        if detail is None:
            raise RuntimeError(f"SkladBot API HTTP 404: No query results for model {request_id}")
        return detail

    def create_request(self, _payload):
        self.create_calls += 1
        request_id = self.create_response_id
        self.details.setdefault(request_id, {"id": request_id, "delivery_number": f"WH-R-{request_id}"})
        return {"data": {"id": request_id}}


def list_item(request_id, created_on):
    return {
        "id": request_id,
        "delivery_number": f"WH-R-{request_id}",
        "created_at": created_on.isoformat(),
        "type": "Отгрузка 3PL",
    }


def request_detail(request_id, *, client=CLIENT, products=((PRODUCT, 1),), unloading_date=ORDER_DATE):
    return {
        "id": request_id,
        "delivery_number": f"WH-R-{request_id}",
        "comment": f"{PAYMENT}\nТП1",
        "fields": [
            {"field": "unloading_date", "value": unloading_date.isoformat()},
            {"field": "company_name", "value": client},
        ],
        "products": [{"name": name, "amount": amount} for name, amount in products],
    }


class SkladBotAmbiguousAutoRecheckTests(unittest.TestCase):
    """14.10.2026: пять заказов простояли без заявки три часа после таймаута POST,
    сверка «наши заявки против привязанных к заказам» делалась руками."""

    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.engine = create_engine(f"sqlite+pysqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.today = business_today()
        env = mock.patch.dict(os.environ, {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        os.unlink(self.db_path)

    def seed_order(self, db, *, client=CLIENT, raw_payload=None, products=((PRODUCT, 1),)):
        order = Order(
            id=uuid.uuid4(),
            source="test",
            external_id=f"synthetic-{uuid.uuid4()}",
            order_date=ORDER_DATE,
            payment_type=PAYMENT,
            client=client,
            address="Synthetic address",
            representative="ТП1",
            status="not_completed",
            raw_payload=dict(raw_payload or {}),
        )
        for product, blocks in products:
            order.items.append(OrderItem(
                product=product,
                quantity_pieces=10 * blocks,
                quantity_blocks=blocks,
                pieces_per_block=10,
                scanned_blocks=0,
                status="not_completed",
                raw_payload={"synthetic": True},
            ))
        db.add(order)
        db.flush()
        return order

    def seed_ambiguous_event(self, *, client=CLIENT, products=((PRODUCT, 1),)):
        """Заказ и событие проходят настоящий путь до ambiguous: POST с таймаутом."""
        with self.SessionLocal() as db:
            order = self.seed_order(db, client=client, products=products)
            row = {
                "status": "ready",
                "order_id": str(order.id),
                "payload": {
                    "comment": f"{PAYMENT}\nТП1",
                    "fields": {"comment": {"value": f"{PAYMENT}\nТП1"}},
                    "products": [],
                },
            }
            self.assertEqual(queue_skladbot_create_events(db, "synthetic-import", [row]), 1)
            db.commit()
            event_id = uuid.UUID(row["create_event_id"])
            order_id = order.id
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            event.status = "processing"
            event.attempts = 1
            db.commit()
            result = process_skladbot_create_event(db, event, TimeoutOnPostClient())
            self.assertEqual(result["status"], "ambiguous")
            finish_skladbot_create_event(db, event, result, default_create_processing_result())
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            self.assertEqual(event.status, "blocked")
            self.assertEqual(event.payload.get("post_state"), "ambiguous")
        return order_id, event_id

    def covering_items(self, *extra):
        """Список наших заявок, который доходит до заявок задолго до зависшего POST."""
        items = [list_item(500 + offset, self.today - timedelta(days=3)) for offset in range(3)]
        items.extend(extra)
        return sorted(items, key=lambda item: int(item["id"]), reverse=True)

    def recheck(self, client, now):
        with mock.patch.dict(os.environ, ENABLED):
            with self.SessionLocal() as db:
                return recheck_ambiguous_skladbot_creates(db, client, now=now)

    def event(self, event_id):
        with self.SessionLocal() as db:
            return db.get(PendingEvent, event_id)

    def order(self, order_id):
        with self.SessionLocal() as db:
            return db.get(Order, order_id)

    def audit_actions(self, event_id):
        with self.SessionLocal() as db:
            return [
                row.action
                for row in db.execute(
                    select(AuditLog).where(AuditLog.entity_id == str(event_id)).order_by(AuditLog.created_at)
                ).scalars()
            ]

    def test_disabled_flag_leaves_ambiguous_event_untouched(self):
        _order_id, event_id = self.seed_ambiguous_event()
        client = RecheckClient(self.covering_items())
        now = datetime.now(timezone.utc) + timedelta(minutes=30)

        with mock.patch.dict(os.environ, {SKLADBOT_AMBIGUOUS_AUTO_RECHECK_ENV: ""}):
            with self.SessionLocal() as db:
                result = recheck_ambiguous_skladbot_creates(db, client, now=now)

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(client.list_calls, [])
        self.assertEqual(self.event(event_id).status, "blocked")

    def test_no_orphan_twice_requeues_event_and_worker_creates_request(self):
        order_id, event_id = self.seed_ambiguous_event()
        client = RecheckClient(self.covering_items(), create_response_id=9001)
        start = datetime.now(timezone.utc)

        first = self.recheck(client, start + timedelta(minutes=6))
        self.assertEqual(first["clean_first"], 1)
        self.assertEqual(self.event(event_id).status, "blocked")

        second = self.recheck(client, start + timedelta(minutes=12))
        self.assertEqual(second["requeued"], 1)
        event = self.event(event_id)
        self.assertEqual(event.status, "pending")
        self.assertEqual(event.attempts, 0)
        self.assertEqual(event.payload.get("post_state"), "retry_scheduled")
        self.assertEqual(event.payload.get("create_status"), "queued")
        self.assertEqual(event.payload["auto_recheck"]["retries"], 1)
        self.assertIn("skladbot_create_auto_recheck_requeued", self.audit_actions(event_id))

        with self.SessionLocal() as db:
            processed = process_pending_skladbot_request_creates(db, client=client)
        self.assertEqual(processed["created"], 1)
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(self.order(order_id).raw_payload.get("skladbot_request_number"), "WH-R-9001")
        self.assertEqual(self.event(event_id).status, "completed")
        with self.SessionLocal() as db:
            incident = db.execute(
                select(Incident).where(Incident.pending_event_id == event_id)
            ).scalar_one()
        self.assertEqual(incident.status, "resolved")

    def test_second_clean_check_too_soon_does_not_requeue(self):
        _order_id, event_id = self.seed_ambiguous_event()
        client = RecheckClient(self.covering_items())
        start = datetime.now(timezone.utc)

        self.recheck(client, start + timedelta(minutes=6))
        result = self.recheck(client, start + timedelta(minutes=8))

        self.assertEqual(result["requeued"], 0)
        self.assertEqual(self.event(event_id).status, "blocked")

    def test_first_check_waits_after_post_timeout(self):
        _order_id, event_id = self.seed_ambiguous_event()
        client = RecheckClient(self.covering_items())

        result = self.recheck(client, datetime.now(timezone.utc) + timedelta(minutes=1))

        self.assertEqual(result["checked"], 0)
        self.assertEqual(client.list_calls, [])
        self.assertNotIn("auto_recheck", self.event(event_id).payload)

    def test_single_matching_orphan_is_linked_without_post(self):
        order_id, event_id = self.seed_ambiguous_event()
        orphan = list_item(777, self.today)
        detail = request_detail(777)
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            diagnostics = request_match_diagnostics(order, normalize_request_payload(orphan, detail))
        self.assertTrue(diagnostics["matched"], diagnostics)
        client = RecheckClient(self.covering_items(orphan), {777: detail})

        result = self.recheck(client, datetime.now(timezone.utc) + timedelta(minutes=6))

        self.assertEqual(result["linked"], 1)
        self.assertEqual(client.create_calls, 0)
        order = self.order(order_id)
        self.assertEqual(order.raw_payload.get("skladbot_request_number"), "WH-R-777")
        self.assertEqual(order.raw_payload.get("skladbot_status"), "created_recovered")
        self.assertEqual(self.event(event_id).status, "completed")
        with self.SessionLocal() as db:
            incident = db.execute(
                select(Incident).where(Incident.pending_event_id == event_id)
            ).scalar_one()
        self.assertEqual(incident.status, "resolved")

    def test_orphan_linked_by_another_process_during_recheck_is_not_linked_twice(self):
        order_id, event_id = self.seed_ambiguous_event()
        orphan = list_item(784, self.today)
        client = RecheckClient(self.covering_items(orphan), {784: request_detail(784)})
        real_detail = client.get_request_detail

        def detail_while_other_worker_links(request_id):
            # пока идёт сверка, параллельный процесс привязывает эту заявку к своему заказу
            with self.SessionLocal() as other:
                self.seed_order(other, raw_payload={
                    "skladbot_request_id": str(request_id),
                    "skladbot_request_number": f"WH-R-{request_id}",
                })
                other.commit()
            return real_detail(request_id)

        client.get_request_detail = detail_while_other_worker_links

        result = self.recheck(client, datetime.now(timezone.utc) + timedelta(minutes=6))

        self.assertEqual(result["linked"], 0)
        self.assertEqual(self.event(event_id).status, "blocked")
        self.assertFalse(self.order(order_id).raw_payload.get("skladbot_request_number"))

    def test_orphan_for_same_client_and_date_with_other_content_blocks_requeue(self):
        _order_id, event_id = self.seed_ambiguous_event()
        orphan = list_item(778, self.today)
        detail = request_detail(778, products=((PRODUCT, 3),))
        client = RecheckClient(self.covering_items(orphan), {778: detail})
        start = datetime.now(timezone.utc)

        self.recheck(client, start + timedelta(minutes=6))
        result = self.recheck(client, start + timedelta(minutes=30))

        self.assertEqual(result["requeued"], 0)
        event = self.event(event_id)
        self.assertEqual(event.status, "blocked")
        self.assertEqual(event.payload["auto_recheck"]["last_verdict"], "manual")

    def test_orphan_of_other_client_does_not_block_requeue(self):
        _order_id, event_id = self.seed_ambiguous_event()
        orphan = list_item(779, self.today)
        detail = request_detail(779, client='"OTHER SHOP" MCHJ')
        client = RecheckClient(self.covering_items(orphan), {779: detail})
        start = datetime.now(timezone.utc)

        self.recheck(client, start + timedelta(minutes=6))
        result = self.recheck(client, start + timedelta(minutes=12))

        self.assertEqual(result["requeued"], 1)
        self.assertEqual(self.event(event_id).status, "pending")

    def test_two_matching_orphans_go_to_manual_review(self):
        order_id, event_id = self.seed_ambiguous_event()
        first, second = list_item(780, self.today), list_item(781, self.today)
        client = RecheckClient(
            self.covering_items(first, second),
            {780: request_detail(780), 781: request_detail(781)},
        )

        result = self.recheck(client, datetime.now(timezone.utc) + timedelta(minutes=6))

        self.assertEqual(result["linked"], 0)
        self.assertEqual(self.event(event_id).status, "blocked")
        self.assertFalse(self.order(order_id).raw_payload.get("skladbot_request_number"))
        self.assertEqual(self.event(event_id).payload["auto_recheck"]["last_verdict"], "manual")

    def test_list_that_does_not_reach_before_post_never_requeues(self):
        _order_id, event_id = self.seed_ambiguous_event()
        # все заявки короткого списка привязаны: сирот нет, решает только покрытие
        with self.SessionLocal() as db:
            for offset in range(5):
                self.seed_order(db, raw_payload={
                    "skladbot_request_id": str(900 + offset),
                    "skladbot_request_number": f"WH-R-{900 + offset}",
                })
            db.commit()
        short_list = [list_item(900 + offset, self.today) for offset in range(5)]
        client = RecheckClient(short_list)
        start = datetime.now(timezone.utc)

        self.recheck(client, start + timedelta(minutes=6))
        result = self.recheck(client, start + timedelta(minutes=30))

        self.assertEqual(result["requeued"], 0)
        event = self.event(event_id)
        self.assertEqual(event.status, "blocked")
        self.assertEqual(event.payload["auto_recheck"]["last_verdict"], "unproven")

    def test_list_that_misses_newest_linked_request_never_requeues(self):
        _order_id, event_id = self.seed_ambiguous_event()
        with self.SessionLocal() as db:
            self.seed_order(
                db,
                raw_payload={"skladbot_request_id": "990", "skladbot_request_number": "WH-R-990"},
            )
            db.commit()
        client = RecheckClient(self.covering_items())
        start = datetime.now(timezone.utc)

        self.recheck(client, start + timedelta(minutes=6))
        result = self.recheck(client, start + timedelta(minutes=30))

        self.assertEqual(result["requeued"], 0)
        event = self.event(event_id)
        self.assertEqual(event.status, "blocked")
        self.assertEqual(event.payload["auto_recheck"]["last_verdict"], "unproven")

    def test_list_failure_changes_nothing(self):
        _order_id, event_id = self.seed_ambiguous_event()
        client = RecheckClient(list_error=TimeoutError("SkladBot API timeout"))

        result = self.recheck(client, datetime.now(timezone.utc) + timedelta(minutes=6))

        self.assertEqual(result["status"], "list_failed")
        event = self.event(event_id)
        self.assertEqual(event.status, "blocked")
        self.assertNotIn("first_clean_at", event.payload.get("auto_recheck") or {})

    def test_unreadable_orphan_blocks_requeue(self):
        _order_id, event_id = self.seed_ambiguous_event()
        orphan = list_item(782, self.today)
        client = RecheckClient(self.covering_items(orphan), {782: TimeoutError("SkladBot API timeout")})
        start = datetime.now(timezone.utc)

        self.recheck(client, start + timedelta(minutes=6))
        result = self.recheck(client, start + timedelta(minutes=30))

        self.assertEqual(result["requeued"], 0)
        self.assertEqual(self.event(event_id).status, "blocked")

    def test_requests_linked_to_orders_are_not_orphans(self):
        _order_id, event_id = self.seed_ambiguous_event()
        linked = list_item(783, self.today)
        with self.SessionLocal() as db:
            self.seed_order(
                db,
                raw_payload={"skladbot_request_id": "783", "skladbot_request_number": "WH-R-783"},
            )
            db.commit()
        client = RecheckClient(self.covering_items(linked), {783: request_detail(783)})

        result = self.recheck(client, datetime.now(timezone.utc) + timedelta(minutes=6))

        self.assertEqual(result["linked"], 0)
        self.assertEqual(result["clean_first"], 1)
        self.assertNotIn(783, client.detail_calls)

    def test_auto_retry_happens_once(self):
        _order_id, event_id = self.seed_ambiguous_event()
        client = RecheckClient(self.covering_items())
        start = datetime.now(timezone.utc)
        self.recheck(client, start + timedelta(minutes=6))
        self.recheck(client, start + timedelta(minutes=12))
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            event.status = "processing"
            event.attempts = 1
            db.commit()
            result = process_skladbot_create_event(db, event, TimeoutOnPostClient())
            finish_skladbot_create_event(db, event, result, default_create_processing_result())
        self.assertEqual(self.event(event_id).status, "blocked")

        later = datetime.now(timezone.utc)
        self.recheck(client, later + timedelta(minutes=6))
        result = self.recheck(client, later + timedelta(minutes=12))

        self.assertEqual(result["requeued"], 0)
        event = self.event(event_id)
        self.assertEqual(event.status, "blocked")
        self.assertEqual(event.payload["auto_recheck"]["last_verdict"], "manual")

    def test_order_changed_after_post_is_not_requeued(self):
        order_id, event_id = self.seed_ambiguous_event()
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            order.items[0].quantity_blocks = 2
            order.items[0].quantity_pieces = 20
            db.commit()
        client = RecheckClient(self.covering_items())
        start = datetime.now(timezone.utc)

        self.recheck(client, start + timedelta(minutes=6))
        result = self.recheck(client, start + timedelta(minutes=12))

        self.assertEqual(result["requeued"], 0)
        self.assertEqual(self.event(event_id).status, "blocked")

    def test_same_items_in_other_order_count_as_unchanged_order(self):
        # 14.10.2026 на бою: у заказов из двух позиций порядок позиций при повторной
        # сборке тела запроса отличался от отправленного, состав тот же
        _order_id, event_id = self.seed_ambiguous_event(products=((PRODUCT, 2), (SECOND_PRODUCT, 1)))
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            sent = dict(event.payload["request_payload"])
            self.assertEqual(len(sent["products"]), 2)
            sent["products"] = list(reversed(sent["products"]))
            event.payload = {**event.payload, "request_payload": sent, "request_payload_hash": stable_payload_hash(sent)}
            db.commit()
        client = RecheckClient(self.covering_items())
        start = datetime.now(timezone.utc)

        self.recheck(client, start + timedelta(minutes=6))
        result = self.recheck(client, start + timedelta(minutes=12))

        self.assertEqual(result["requeued"], 1)
        self.assertEqual(self.event(event_id).status, "pending")

    def test_event_with_known_response_id_is_left_for_manual_review(self):
        _order_id, event_id = self.seed_ambiguous_event()
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            event.payload = {**event.payload, "post_response_request_id": 9100}
            db.commit()
        client = RecheckClient(self.covering_items())

        result = self.recheck(client, datetime.now(timezone.utc) + timedelta(minutes=6))

        self.assertEqual(result["checked"], 0)
        self.assertEqual(client.list_calls, [])


class SkladBotWorkerRunnerAmbiguousRecheckTests(unittest.TestCase):
    def run_cycle(self, recheck_processor):
        context = mock.MagicMock()
        context.__enter__.return_value = object()
        return run_worker_cycle(
            session_factory=lambda: context,
            create_processor=mock.Mock(return_value={"checked": 0}),
            return_processor=mock.Mock(return_value={"checked": 0}),
            sync_processor=mock.Mock(return_value={"updated": 0}),
            ambiguous_recheck_processor=recheck_processor,
        )

    def test_runner_calls_recheck_only_when_enabled(self):
        recheck_processor = mock.Mock(return_value={"checked": 1})

        with mock.patch.dict(os.environ, {SKLADBOT_AMBIGUOUS_AUTO_RECHECK_ENV: ""}):
            disabled = self.run_cycle(recheck_processor)
        recheck_processor.assert_not_called()
        self.assertNotIn("ambiguous_recheck", disabled)

        with mock.patch.dict(os.environ, ENABLED):
            enabled = self.run_cycle(recheck_processor)
        recheck_processor.assert_called_once()
        self.assertEqual(enabled["ambiguous_recheck"], {"checked": 1})

    def test_recheck_failure_does_not_stop_return_and_sync_processing(self):
        context = mock.MagicMock()
        context.__enter__.return_value = object()
        return_processor = mock.Mock(return_value={"checked": 0})
        sync_processor = mock.Mock(return_value={"updated": 0})

        with mock.patch.dict(os.environ, ENABLED):
            run_worker_cycle(
                session_factory=lambda: context,
                create_processor=mock.Mock(return_value={"checked": 0}),
                return_processor=return_processor,
                sync_processor=sync_processor,
                ambiguous_recheck_processor=mock.Mock(side_effect=RuntimeError("recheck broke")),
            )

        return_processor.assert_called_once()
        sync_processor.assert_called_once()


if __name__ == "__main__":
    unittest.main()
