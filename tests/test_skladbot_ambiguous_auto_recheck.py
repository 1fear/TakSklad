import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app import skladbot_ambiguous_recheck
from backend.app.models import AuditLog, Base, Incident, Order, OrderItem, PendingEvent
from backend.app.skladbot_ambiguous_recheck import (
    SKLADBOT_AMBIGUOUS_AUTO_RECHECK_MODE_ENV,
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


FULL = {SKLADBOT_AMBIGUOUS_AUTO_RECHECK_MODE_ENV: "full"}
CLIENT = '"SYNTHETIC MARKET" MCHJ'
PAYMENT = "Перечисление"
PRODUCT = "Chapman RED OP 20"
SECOND_PRODUCT = "Chapman BROWN OP 20"


def order_date():
    return business_today() + timedelta(days=1)


class TimeoutOnPostClient:
    """POST уходит в таймаут: исход на стороне SkladBot неизвестен"""

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


def list_item(request_id, created_on, **extra):
    return {
        "id": request_id,
        "delivery_number": f"WH-R-{request_id}",
        "created_at": created_on.isoformat(),
        "type": "Отгрузка 3PL",
        **extra,
    }


def request_detail(request_id, *, client=CLIENT, products=((PRODUCT, 1),), unloading_date=None, **extra):
    return {
        "id": request_id,
        "delivery_number": f"WH-R-{request_id}",
        "comment": f"{PAYMENT}\nТП1",
        "fields": [
            {"field": "unloading_date", "value": (unloading_date or order_date()).isoformat()},
            {"field": "company_name", "value": client},
        ],
        "products": [{"name": name, "amount": amount} for name, amount in products],
        **extra,
    }


class RecheckFixture(unittest.TestCase):
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

    def seed_order(self, db, *, client=CLIENT, raw_payload=None, products=((PRODUCT, 1),), status="not_completed", when=None):
        order = Order(
            id=uuid.uuid4(),
            source="test",
            external_id=f"synthetic-{uuid.uuid4()}",
            order_date=when or order_date(),
            payment_type=PAYMENT,
            client=client,
            address="Synthetic address",
            representative="ТП1",
            status=status,
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

    def seed_linked_order(self, request_id, **raw_payload):
        with self.SessionLocal() as db:
            self.seed_order(db, raw_payload={
                "skladbot_request_id": str(request_id),
                "skladbot_request_number": f"WH-R-{request_id}",
                **raw_payload,
            })
            db.commit()

    def seed_ambiguous_event(self, *, client=CLIENT, products=((PRODUCT, 1),)):
        """Заказ и событие проходят настоящий путь до ambiguous: POST с таймаутом"""
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
        self.time_out_post(event_id)
        return order_id, event_id

    def time_out_post(self, event_id):
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            event.status = "processing"
            event.attempts = 1
            db.commit()
            result = process_skladbot_create_event(db, event, TimeoutOnPostClient())
            self.assertEqual(result["status"], "ambiguous")
            finish_skladbot_create_event(db, event, result, default_create_processing_result())
        event = self.event(event_id)
        self.assertEqual(event.status, "blocked")
        self.assertEqual(event.payload.get("post_state"), "ambiguous")

    def covering_items(self, *extra):
        """Список наших заявок, который доходит до заявок задолго до зависшего POST

        Все его заявки привязаны к заказам, сиротами могут быть только те, что переданы в extra
        """
        items = [list_item(500 + offset, self.today - timedelta(days=3)) for offset in range(3)]
        # привязанная заявка сегодняшнего дня, как в настоящем списке: попадает в окно сирот
        items.append(list_item(510, self.today))
        items.extend(extra)
        return sorted(items, key=lambda item: (item["created_at"], int(item["id"])), reverse=True)

    def link_background(self):
        for request_id in (500, 501, 502, 510):
            self.seed_linked_order(request_id)

    def recheck(self, client, now, mode="full"):
        with mock.patch.dict(os.environ, {SKLADBOT_AMBIGUOUS_AUTO_RECHECK_MODE_ENV: mode}):
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

    def incident_status(self, event_id):
        with self.SessionLocal() as db:
            return db.execute(
                select(Incident.status).where(Incident.pending_event_id == event_id)
            ).scalar_one()

    def two_clean_checks(self, client, mode="full"):
        start = datetime.now(timezone.utc)
        self.recheck(client, start + timedelta(minutes=6), mode=mode)
        return self.recheck(client, start + timedelta(minutes=12), mode=mode)


class SkladBotAmbiguousAutoRecheckTests(RecheckFixture):
    """14.10.2026: пять заказов простояли без заявки три часа после таймаута POST,
    сверка «наши заявки против привязанных к заказам» делалась руками"""

    def setUp(self):
        super().setUp()
        self.link_background()

    def test_mode_off_leaves_ambiguous_event_untouched(self):
        _order_id, event_id = self.seed_ambiguous_event()
        client = RecheckClient(self.covering_items())

        result = self.recheck(client, datetime.now(timezone.utc) + timedelta(minutes=30), mode="")

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
        self.assertEqual(self.incident_status(event_id), "resolved")

    def test_no_database_transaction_is_open_while_skladbot_answers(self):
        self.seed_ambiguous_event()
        orphan = list_item(777, self.today)
        seen = []
        with mock.patch.dict(os.environ, FULL):
            with self.SessionLocal() as db:
                client = RecheckClient(self.covering_items(orphan), {777: request_detail(777)})
                real_list, real_detail = client.list_requests, client.get_request_detail

                def listing(**kwargs):
                    seen.append(("list", db.in_transaction()))
                    return real_list(**kwargs)

                def detail(request_id):
                    seen.append(("detail", db.in_transaction()))
                    return real_detail(request_id)

                client.list_requests, client.get_request_detail = listing, detail
                recheck_ambiguous_skladbot_creates(db, client, now=datetime.now(timezone.utc) + timedelta(minutes=6))

        self.assertEqual(seen, [("list", False), ("detail", False)])

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
        self.assertEqual(self.incident_status(event_id), "resolved")

    def test_unreadable_or_incomplete_orphan_detail_never_requeues(self):
        # 14.10.2026, рецензия: пустая или иная по форме деталь потерянной заявки
        # не совпадала ни с чем и читалась как «сирота не наша», дальше шёл дубль
        broken = {
            "error": TimeoutError("SkladBot API timeout"),
            "empty": {},
            "null data": {"data": None},
            "no recipient": request_detail(785, client=""),
            "no products": request_detail(785, products=()),
            "fields as dict": {**request_detail(785), "fields": {"company_name": CLIENT}},
        }
        for name, detail in broken.items():
            with self.subTest(name):
                _order_id, event_id = self.seed_ambiguous_event()
                orphan = list_item(785, self.today)
                client = RecheckClient(self.covering_items(orphan), {785: detail})

                result = self.two_clean_checks(client)

                self.assertEqual(result["requeued"], 0)
                event = self.event(event_id)
                self.assertEqual(event.status, "blocked")
                self.assertEqual(event.payload["auto_recheck"]["last_reason"], "orphan_details_unavailable")
                self.assertEqual(client.create_calls, 0)
                with self.SessionLocal() as db:
                    db.delete(db.get(PendingEvent, event_id))
                    db.commit()

    def test_detail_with_other_id_than_list_row_is_not_linked(self):
        order_id, event_id = self.seed_ambiguous_event()
        orphan = list_item(777, self.today)
        client = RecheckClient(self.covering_items(orphan), {777: request_detail(888)})

        result = self.recheck(client, datetime.now(timezone.utc) + timedelta(minutes=6))

        self.assertEqual(result["linked"], 0)
        self.assertFalse(self.order(order_id).raw_payload.get("skladbot_request_id"))
        self.assertEqual(self.event(event_id).status, "blocked")

    def test_orphan_with_archived_and_completed_flags_is_still_linked(self):
        # 14.10.2026 на бою: SkladBot отдаёт archived=1 и is_completed=1 у всех 500 заявок
        # списка, включая созданные за минуты до чтения, эти флаги ничего не значат
        order_id, event_id = self.seed_ambiguous_event()
        orphan = list_item(786, self.today, archived=1, is_completed=1)
        detail = request_detail(786, archived=1, isCompleted=True)
        client = RecheckClient(self.covering_items(orphan), {786: detail})

        result = self.recheck(client, datetime.now(timezone.utc) + timedelta(minutes=6))

        self.assertEqual(result["linked"], 1)
        self.assertEqual(self.order(order_id).raw_payload.get("skladbot_request_number"), "WH-R-786")

    def test_orphan_linked_by_another_process_during_recheck_is_not_linked_twice(self):
        order_id, event_id = self.seed_ambiguous_event()
        orphan = list_item(784, self.today)
        client = RecheckClient(self.covering_items(orphan), {784: request_detail(784)})
        real_detail = client.get_request_detail

        def detail_while_other_worker_links(request_id):
            # пока идёт сверка, параллельный процесс привязывает эту заявку к своему заказу
            self.seed_linked_order(request_id)
            return real_detail(request_id)

        client.get_request_detail = detail_while_other_worker_links

        result = self.recheck(client, datetime.now(timezone.utc) + timedelta(minutes=6))

        self.assertEqual(result["linked"], 0)
        self.assertEqual(self.event(event_id).status, "blocked")
        self.assertFalse(self.order(order_id).raw_payload.get("skladbot_request_number"))

    def test_orphan_received_by_neighbour_post_during_recheck_is_not_linked(self):
        # ответ POST соседнего заказа записан в событие, а до заказа ещё не дошёл
        order_id, event_id = self.seed_ambiguous_event()
        neighbour_order_id, neighbour_event_id = self.seed_ambiguous_event()
        with self.SessionLocal() as db:
            neighbour = db.get(PendingEvent, neighbour_event_id)
            neighbour.status = "completed"
            db.commit()
        orphan = list_item(787, self.today)
        client = RecheckClient(self.covering_items(orphan), {787: request_detail(787)})
        real_detail = client.get_request_detail

        def detail_while_neighbour_gets_response(request_id):
            with self.SessionLocal() as other:
                neighbour = other.get(PendingEvent, neighbour_event_id)
                neighbour.payload = {**neighbour.payload, "post_response_request_id": request_id}
                other.commit()
            return real_detail(request_id)

        client.get_request_detail = detail_while_neighbour_gets_response

        result = self.recheck(client, datetime.now(timezone.utc) + timedelta(minutes=6))

        self.assertEqual(result["linked"], 0)
        self.assertFalse(self.order(order_id).raw_payload.get("skladbot_request_id"))
        self.assertFalse(self.order(neighbour_order_id).raw_payload.get("skladbot_request_id"))

    def test_orphan_for_same_client_and_date_with_other_content_blocks_requeue(self):
        _order_id, event_id = self.seed_ambiguous_event()
        orphan = list_item(778, self.today)
        client = RecheckClient(self.covering_items(orphan), {778: request_detail(778, products=((PRODUCT, 3),))})
        start = datetime.now(timezone.utc)

        self.recheck(client, start + timedelta(minutes=6))
        result = self.recheck(client, start + timedelta(minutes=30))

        self.assertEqual(result["requeued"], 0)
        event = self.event(event_id)
        self.assertEqual(event.status, "blocked")
        self.assertEqual(event.payload["auto_recheck"]["last_reason"], "similar_orphan")

    def test_unexplained_orphan_of_other_client_blocks_requeue(self):
        # повтор только при нуле сирот: несовпадение по содержимому не доказывает, что заявка чужая
        _order_id, event_id = self.seed_ambiguous_event()
        orphan = list_item(779, self.today)
        client = RecheckClient(self.covering_items(orphan), {779: request_detail(779, client='"OTHER SHOP" MCHJ')})

        result = self.two_clean_checks(client)

        self.assertEqual(result["requeued"], 0)
        event = self.event(event_id)
        self.assertEqual(event.status, "blocked")
        self.assertEqual(event.payload["auto_recheck"]["last_reason"], "unexplained_orphan")

    def test_orphan_owned_by_another_candidate_does_not_block_requeue(self):
        # 10.09.2026: в одном сбое часть заявок была создана (класс A), часть нет (класс B)
        linked_order_id, linked_event_id = self.seed_ambiguous_event()
        lost_order_id, lost_event_id = self.seed_ambiguous_event(client='"SECOND SHOP" MCHJ')
        orphan = list_item(788, self.today)
        client = RecheckClient(self.covering_items(orphan), {788: request_detail(788)})
        start = datetime.now(timezone.utc)

        first = self.recheck(client, start + timedelta(minutes=6))
        self.assertEqual(first["linked"], 1)
        self.assertEqual(first["clean_first"], 1)
        second = self.recheck(client, start + timedelta(minutes=12))

        self.assertEqual(second["requeued"], 1)
        self.assertEqual(self.order(linked_order_id).raw_payload.get("skladbot_request_number"), "WH-R-788")
        self.assertEqual(self.event(lost_event_id).status, "pending")
        self.assertFalse(self.order(lost_order_id).raw_payload.get("skladbot_request_id"))

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

    def test_two_candidates_and_one_matching_orphan_both_go_to_manual_review(self):
        first_order_id, first_event_id = self.seed_ambiguous_event()
        second_order_id, second_event_id = self.seed_ambiguous_event()
        orphan = list_item(789, self.today)
        client = RecheckClient(self.covering_items(orphan), {789: request_detail(789)})

        result = self.two_clean_checks(client)

        self.assertEqual(result["linked"] + result["requeued"], 0)
        for order_id, event_id in ((first_order_id, first_event_id), (second_order_id, second_event_id)):
            self.assertEqual(self.event(event_id).status, "blocked")
            self.assertFalse(self.order(order_id).raw_payload.get("skladbot_request_id"))

    def test_list_that_does_not_reach_before_post_never_requeues(self):
        _order_id, event_id = self.seed_ambiguous_event()
        for offset in range(5):
            self.seed_linked_order(900 + offset)
        client = RecheckClient([list_item(904 - offset, self.today) for offset in range(5)])
        start = datetime.now(timezone.utc)

        self.recheck(client, start + timedelta(minutes=6))
        result = self.recheck(client, start + timedelta(minutes=30))

        self.assertEqual(result["requeued"], 0)
        event = self.event(event_id)
        self.assertEqual(event.status, "blocked")
        self.assertEqual(event.payload["auto_recheck"]["last_verdict"], "unproven")

    def test_list_that_misses_newest_linked_request_never_requeues(self):
        _order_id, event_id = self.seed_ambiguous_event()
        self.seed_linked_order(990)
        client = RecheckClient(self.covering_items())
        start = datetime.now(timezone.utc)

        self.recheck(client, start + timedelta(minutes=6))
        result = self.recheck(client, start + timedelta(minutes=30))

        self.assertEqual(result["requeued"], 0)
        self.assertEqual(self.event(event_id).payload["auto_recheck"]["last_verdict"], "unproven")

    def test_list_not_sorted_newest_first_never_requeues(self):
        # порядок «новые сверху» держится только на наблюдении: список проверяет его сам
        _order_id, event_id = self.seed_ambiguous_event()
        self.seed_linked_order(503)
        newest_first = self.covering_items(list_item(503, self.today))
        self.assertGreater(newest_first[0]["created_at"], newest_first[-1]["created_at"])
        client = RecheckClient(list(reversed(newest_first)))

        result = self.two_clean_checks(client)

        self.assertEqual(result["requeued"], 0)
        self.assertEqual(self.event(event_id).payload["auto_recheck"]["last_reason"], "list_not_sorted_newest_first")

    def test_request_linked_only_by_number_is_not_an_orphan(self):
        _order_id, event_id = self.seed_ambiguous_event()
        with self.SessionLocal() as db:
            self.seed_order(db, raw_payload={"skladbot_request_number": "WH-R-790"})
            db.commit()
        client = RecheckClient(self.covering_items(list_item(790, self.today)), {790: request_detail(790)})

        result = self.recheck(client, datetime.now(timezone.utc) + timedelta(minutes=6))

        self.assertEqual(result["clean_first"], 1)
        self.assertNotIn(790, client.detail_calls)

    def test_orphans_over_detail_limit_are_not_read_and_never_requeue(self):
        _order_id, event_id = self.seed_ambiguous_event()
        orphans = [list_item(800 + offset, self.today) for offset in range(4)]
        client = RecheckClient(self.covering_items(*orphans))

        with mock.patch.dict(os.environ, {"SKLADBOT_AMBIGUOUS_RECHECK_DETAIL_LIMIT": "3"}):
            result = self.two_clean_checks(client)

        self.assertEqual(result["requeued"], 0)
        self.assertEqual(client.detail_calls, [])
        self.assertEqual(self.event(event_id).payload["auto_recheck"]["last_verdict"], "unproven")

    def test_detail_reading_stops_at_time_budget(self):
        _order_id, event_id = self.seed_ambiguous_event()
        orphan = list_item(777, self.today)
        client = RecheckClient(self.covering_items(orphan), {777: request_detail(777)})

        with mock.patch.object(skladbot_ambiguous_recheck, "DETAIL_TIME_BUDGET_SECONDS", 0):
            result = self.recheck(client, datetime.now(timezone.utc) + timedelta(minutes=6))

        self.assertEqual(result["linked"], 0)
        self.assertEqual(client.detail_calls, [])
        self.assertEqual(self.event(event_id).payload["auto_recheck"]["last_verdict"], "unproven")

    def test_list_failure_pauses_candidates(self):
        _order_id, event_id = self.seed_ambiguous_event()
        client = RecheckClient(list_error=TimeoutError("SkladBot API timeout"))
        start = datetime.now(timezone.utc)

        result = self.recheck(client, start + timedelta(minutes=6))
        again = self.recheck(client, start + timedelta(minutes=7))

        self.assertEqual(result["status"], "list_failed")
        self.assertEqual(again["checked"], 0)
        self.assertEqual(len(client.list_calls), 1)
        event = self.event(event_id)
        self.assertEqual(event.status, "blocked")
        self.assertNotIn("first_clean_at", event.payload.get("auto_recheck") or {})

    def test_failure_in_one_candidate_does_not_stop_others_and_pauses_it(self):
        broken_order_id, broken_event_id = self.seed_ambiguous_event()
        _order_id, event_id = self.seed_ambiguous_event(client='"SECOND SHOP" MCHJ')
        client = RecheckClient(self.covering_items())
        real_apply = skladbot_ambiguous_recheck.apply_verdict

        def apply_failing_for_broken(db, candidate, *args, **kwargs):
            if candidate["event_id"] == broken_event_id:
                raise RuntimeError("synthetic apply failure")
            return real_apply(db, candidate, *args, **kwargs)

        start = datetime.now(timezone.utc)
        with mock.patch.object(skladbot_ambiguous_recheck, "apply_verdict", apply_failing_for_broken):
            result = self.recheck(client, start + timedelta(minutes=6))
            again = self.recheck(client, start + timedelta(minutes=8))

        self.assertEqual(result["clean_first"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(again["checked"], 0)
        self.assertIn("synthetic apply failure", self.event(broken_event_id).payload["auto_recheck"]["last_error"])

    def test_auto_retry_happens_once(self):
        _order_id, event_id = self.seed_ambiguous_event()
        client = RecheckClient(self.covering_items())
        self.two_clean_checks(client)
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            event.status = "processing"
            event.attempts = 1
            db.commit()
            result = process_skladbot_create_event(db, event, TimeoutOnPostClient())
            finish_skladbot_create_event(db, event, result, default_create_processing_result())
        self.assertEqual(self.event(event_id).status, "blocked")

        result = self.two_clean_checks(client)

        self.assertEqual(result["requeued"], 0)
        event = self.event(event_id)
        self.assertEqual(event.status, "blocked")
        self.assertEqual(event.payload["auto_recheck"]["last_reason"], "auto_retry_limit")

    def test_clean_series_restarts_after_new_ambiguous_post(self):
        # 14.10.2026, рецензия: первая чистая проверка до ручного re-arm засчитывалась новому POST
        _order_id, event_id = self.seed_ambiguous_event()
        client = RecheckClient(self.covering_items())
        start = datetime.now(timezone.utc)
        self.recheck(client, start + timedelta(minutes=6))
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            event.payload = {**event.payload, "post_state": "retry_scheduled", "create_status": "queued"}
            db.commit()
        self.time_out_post(event_id)

        result = self.recheck(client, datetime.now(timezone.utc) + timedelta(minutes=12))

        self.assertEqual(result["requeued"], 0)
        self.assertEqual(result["clean_first"], 1)

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

        result = self.two_clean_checks(client)

        self.assertEqual(result["requeued"], 1)
        self.assertEqual(self.event(event_id).status, "pending")

    def test_order_changed_after_post_is_not_requeued(self):
        order_id, event_id = self.seed_ambiguous_event()
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            order.items[0].quantity_blocks = 2
            order.items[0].quantity_pieces = 20
            db.commit()
        client = RecheckClient(self.covering_items())

        result = self.two_clean_checks(client)

        self.assertEqual(result["requeued"], 0)
        self.assertEqual(self.event(event_id).status, "blocked")

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

    def test_inactive_or_past_orders_are_never_rechecked(self):
        # 14.10.2026, рецензия: отменённый заказ через 10 минут получил бы заявку
        cases = {
            "cancelled": {"status": "cancelled"},
            "completed": {"status": "completed"},
            "returned": {"status": "returned"},
            "archived": {"status": "archived_no_kiz"},
            "past date": {"order_date": business_today() - timedelta(days=1)},
        }
        for name, change in cases.items():
            with self.subTest(name):
                order_id, event_id = self.seed_ambiguous_event()
                with self.SessionLocal() as db:
                    order = db.get(Order, order_id)
                    for field, value in change.items():
                        setattr(order, field, value)
                    db.commit()
                client = RecheckClient(self.covering_items())

                result = self.two_clean_checks(client)

                self.assertEqual(result["requeued"] + result["linked"], 0)
                self.assertEqual(client.list_calls, [])
                self.assertEqual(self.event(event_id).status, "blocked")
                with self.SessionLocal() as db:
                    db.delete(db.get(PendingEvent, event_id))
                    db.commit()

    def test_event_with_failed_ownership_is_never_rechecked(self):
        order_id, event_id = self.seed_ambiguous_event()
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            event.aggregate_id = str(uuid.uuid4())
            db.commit()
        orphan = list_item(791, self.today)
        client = RecheckClient(self.covering_items(orphan), {791: request_detail(791)})

        result = self.recheck(client, datetime.now(timezone.utc) + timedelta(minutes=6))

        self.assertEqual(result["checked"], 0)
        self.assertEqual(client.list_calls, [])
        self.assertFalse(self.order(order_id).raw_payload.get("skladbot_request_id"))

    def test_already_linked_or_missing_order_is_never_listed(self):
        order_id, event_id = self.seed_ambiguous_event()
        _missing_order_id, missing_event_id = self.seed_ambiguous_event(client='"SECOND SHOP" MCHJ')
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            order.raw_payload = {**order.raw_payload, "skladbot_request_id": "792", "skladbot_request_number": "WH-R-792"}
            db.delete(db.get(Order, _missing_order_id))
            db.commit()
        client = RecheckClient(self.covering_items())
        start = datetime.now(timezone.utc)

        for minutes in (6, 7, 8):
            result = self.recheck(client, start + timedelta(minutes=minutes))
            self.assertEqual(result["checked"], 0)

        self.assertEqual(client.list_calls, [])

    def test_create_in_flight_or_queued_postpones_recheck(self):
        _order_id, event_id = self.seed_ambiguous_event()
        _busy_order_id, busy_event_id = self.seed_ambiguous_event(client='"SECOND SHOP" MCHJ')
        client = RecheckClient(self.covering_items())
        for status, post_state in (("processing", "started"), ("pending", "retry_scheduled")):
            with self.subTest(status):
                with self.SessionLocal() as db:
                    busy = db.get(PendingEvent, busy_event_id)
                    busy.status = status
                    busy.payload = {**busy.payload, "post_state": post_state}
                    busy.updated_at = datetime.now(timezone.utc)
                    db.commit()

                result = self.recheck(client, datetime.now(timezone.utc) + timedelta(minutes=6))

                self.assertEqual(result["status"], "create_busy")
                self.assertEqual(client.list_calls, [])
                self.assertNotIn("auto_recheck", self.event(event_id).payload)

    def test_observe_mode_records_verdicts_without_link_or_requeue(self):
        linked_order_id, linked_event_id = self.seed_ambiguous_event()
        lost_order_id, lost_event_id = self.seed_ambiguous_event(client='"SECOND SHOP" MCHJ')
        orphan = list_item(793, self.today)
        client = RecheckClient(self.covering_items(orphan), {793: request_detail(793)})

        self.two_clean_checks(client, mode="observe")

        self.assertFalse(self.order(linked_order_id).raw_payload.get("skladbot_request_id"))
        self.assertEqual(self.event(linked_event_id).status, "blocked")
        self.assertEqual(self.event(lost_event_id).status, "blocked")
        self.assertIn("skladbot_create_auto_recheck_observed", self.audit_actions(linked_event_id))
        self.assertIn("skladbot_create_auto_recheck_observed", self.audit_actions(lost_event_id))
        self.assertEqual(self.event(linked_event_id).payload["auto_recheck"]["observed_verdict"], "link")
        self.assertEqual(self.event(lost_event_id).payload["auto_recheck"]["observed_verdict"], "clean")

    def test_link_mode_links_but_never_requeues(self):
        linked_order_id, _linked_event_id = self.seed_ambiguous_event()
        _lost_order_id, lost_event_id = self.seed_ambiguous_event(client='"SECOND SHOP" MCHJ')
        orphan = list_item(794, self.today)
        client = RecheckClient(self.covering_items(orphan), {794: request_detail(794)})

        result = self.two_clean_checks(client, mode="link")

        self.assertEqual(result["requeued"], 0)
        self.assertEqual(self.order(linked_order_id).raw_payload.get("skladbot_request_number"), "WH-R-794")
        event = self.event(lost_event_id)
        self.assertEqual(event.status, "blocked")
        self.assertEqual(event.payload["auto_recheck"]["last_reason"], "requeue_disabled_by_mode")


class SkladBotWorkerRunnerAmbiguousRecheckTests(RecheckFixture):
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

    def test_runner_calls_recheck_only_when_mode_is_on(self):
        recheck_processor = mock.Mock(return_value={"checked": 1})

        with mock.patch.dict(os.environ, {SKLADBOT_AMBIGUOUS_AUTO_RECHECK_MODE_ENV: "off"}):
            disabled = self.run_cycle(recheck_processor)
        recheck_processor.assert_not_called()
        self.assertNotIn("ambiguous_recheck", disabled)

        with mock.patch.dict(os.environ, FULL):
            enabled = self.run_cycle(recheck_processor)
        recheck_processor.assert_called_once()
        self.assertEqual(enabled["ambiguous_recheck"], {"checked": 1})

    def test_recheck_failure_does_not_stop_sync_processing(self):
        context = mock.MagicMock()
        context.__enter__.return_value = object()
        sync_processor = mock.Mock(return_value={"updated": 0})

        with mock.patch.dict(os.environ, FULL):
            run_worker_cycle(
                session_factory=lambda: context,
                create_processor=mock.Mock(return_value={"checked": 0}),
                return_processor=mock.Mock(return_value={"checked": 0}),
                sync_processor=sync_processor,
                ambiguous_recheck_processor=mock.Mock(side_effect=RuntimeError("recheck broke")),
            )

        sync_processor.assert_called_once()

    def test_runner_runs_real_recheck_against_database(self):
        with mock.patch.dict(os.environ, FULL):
            result = run_worker_cycle(
                session_factory=self.SessionLocal,
                create_processor=mock.Mock(return_value={"checked": 0}),
                return_processor=mock.Mock(return_value={"checked": 0}),
                sync_processor=mock.Mock(return_value={"updated": 0}),
            )

        self.assertEqual(result["ambiguous_recheck"]["status"], "completed")
        self.assertEqual(result["ambiguous_recheck"]["checked"], 0)


if __name__ == "__main__":
    unittest.main()
