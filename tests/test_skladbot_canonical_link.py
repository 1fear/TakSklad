import unittest
import uuid
from datetime import date
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import Base, Incident, Order, OrderItem, PendingEvent
from backend.app.skladbot_contracts import (
    canonical_remote_request_id,
    canonical_skladbot_request_evidence_link,
    canonical_skladbot_request_number,
)
from backend.app.skladbot_request_dry_run import (
    find_existing_skladbot_request_for_order,
    normalize_created_request_response,
    order_taksklad_marker,
    process_skladbot_create_event,
    queue_skladbot_create_events,
    reconcile_ambiguous_skladbot_request,
    save_skladbot_create_result,
    skladbot_create_event_ownership_is_valid,
    skladbot_create_idempotency_key,
)
from backend.app.skladbot_worker import (
    CandidateRequests,
    fetch_candidate_requests,
    skladbot_create_event_belongs_to_order,
    update_orders_from_skladbot,
)


class CanonicalLinkClient:
    configured = True

    def __init__(self, response_id, *, detail_id=7001, detail_number="WH-R-7001"):
        self.response_id = response_id
        self.detail_id = detail_id
        self.detail_number = detail_number
        self.create_calls = 0
        self.list_calls = 0
        self.detail_calls = []

    def create_request(self, _payload):
        self.create_calls += 1
        return {"data": {"id": self.response_id}}

    def get_request_detail(self, request_id):
        self.detail_calls.append(request_id)
        return {"id": self.detail_id, "delivery_number": self.detail_number}

    def list_requests(self, **_kwargs):
        self.list_calls += 1
        return []


class SkladBotCanonicalLinkTests(unittest.TestCase):
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

    def seed_create_event(self, db, *, order_id=None):
        order_id = order_id or uuid.uuid4()
        order = Order(
            id=order_id,
            source="test",
            external_id=f"synthetic-{order_id}",
            order_date=date(2026, 7, 17),
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
        event = db.get(PendingEvent, uuid.UUID(row["create_event_id"]))
        return order, event

    @staticmethod
    def candidate(request_id="7001", number="WH-R-7001", *, list_id=None, detail_id=None):
        list_id = request_id if list_id is None else list_id
        detail_id = request_id if detail_id is None else detail_id
        return {
            "id": request_id,
            "number": number,
            "comment": "",
            "raw": {
                "list": {"id": list_id, "delivery_number": number},
                "detail": {"id": detail_id, "delivery_number": number},
            },
        }

    def test_canonical_remote_request_id_policy_has_no_float_conversion(self):
        invalid = (
            "7.001e3",
            "7001.9",
            True,
            "+7001",
            "-7001",
            "0",
            "07001",
            "9" * 21,
            "not-an-id",
        )
        for value in invalid:
            with self.subTest(value=value):
                self.assertEqual(canonical_remote_request_id(value), "")
                self.assertEqual(normalize_created_request_response({"data": {"id": value}})["id"], "")
        for value in (7001, "7001", " 7001 "):
            with self.subTest(value=value):
                self.assertEqual(canonical_remote_request_id(value), "7001")
                self.assertEqual(normalize_created_request_response({"data": {"id": value}})["id"], "7001")

    def test_canonical_request_number_policy_is_shared_and_bounded(self):
        for value, expected in (
            ("WH-R-7001", "WH-R-7001"),
            ("WR-7001-A", "WR-7001-A"),
            (" WH-R-7001 ", "WH-R-7001"),
            ("NOT-A-SKLADBOT-NUMBER", ""),
            ("WH-R-", ""),
            ("WH-R-7001_1", ""),
            ("WH-R-" + "A" * 80, ""),
        ):
            with self.subTest(value=value):
                self.assertEqual(canonical_skladbot_request_number(value), expected)

    def test_raw_evidence_number_may_come_from_either_side_but_never_conflict(self):
        cases = (
            ({"list": {"id": "7001", "delivery_number": "WH-R-7001"}, "detail": {"id": "7001"}}, True),
            ({"list": {"id": "7001"}, "detail": {"id": "7001", "delivery_number": "WH-R-7001"}}, True),
            ({"list": {"id": "7001", "delivery_number": "WH-R-7001"}, "detail": {"id": "7001", "delivery_number": "WH-R-7001"}}, True),
            ({"list": {"id": "7001", "delivery_number": "WH-R-7001"}, "detail": {"id": "7001", "delivery_number": "WR-7001"}}, False),
            ({"list": {"id": "7001", "delivery_number": "bad number"}, "detail": {"id": "7001"}}, False),
            ({"list": {"id": "7001"}, "detail": {"id": "7001", "delivery_number": "bad number"}}, False),
            ({"list": {"id": "7001"}, "detail": {"id": "7001"}}, False),
        )
        for raw, valid in cases:
            with self.subTest(raw=raw):
                actual = canonical_skladbot_request_evidence_link({
                    "id": "7001",
                    "number": "WH-R-7001",
                    "raw": raw,
                })
                self.assertEqual(bool(all(actual)), valid)

    def test_invalid_create_response_id_never_gets_detail_or_persists_link(self):
        for response_id in ("7.001e3", "7001.9", "9" * 21, "07001"):
            with self.subTest(response_id=response_id), self.SessionLocal() as db:
                order, event = self.seed_create_event(db)
                client = CanonicalLinkClient(response_id)

                result = process_skladbot_create_event(db, event, client)

                self.assertEqual(result["status"], "ambiguous")
                self.assertEqual(client.create_calls, 1)
                self.assertEqual(client.detail_calls, [])
                self.assertNotIn("skladbot_request_id", order.raw_payload)
                self.assertNotEqual((event.payload or {}).get("create_status"), "created")
                db.rollback()

    def test_invalid_exact_detail_number_or_id_never_persists_link(self):
        cases = (
            (7002, "WH-R-7002"),
            (7001, "NOT-A-SKLADBOT-NUMBER"),
            (7001, "WH-R-" + "A" * 80),
        )
        for detail_id, detail_number in cases:
            with self.subTest(detail_id=detail_id, detail_number=detail_number), self.SessionLocal() as db:
                order, event = self.seed_create_event(db)
                client = CanonicalLinkClient(
                    "7001",
                    detail_id=detail_id,
                    detail_number=detail_number,
                )

                result = process_skladbot_create_event(db, event, client)

                self.assertEqual(result["status"], "ambiguous")
                self.assertEqual(client.create_calls, 1)
                self.assertEqual(client.detail_calls, [7001])
                self.assertNotIn("skladbot_request_id", order.raw_payload)
                db.rollback()

    def test_existing_main_link_requires_complete_canonical_pair(self):
        partial_links = (
            {"skladbot_request_id": "7.001e3"},
            {"skladbot_request_id": "7001"},
            {"skladbot_request_number": "NOT-A-SKLADBOT-NUMBER"},
        )
        for existing in partial_links:
            with self.subTest(existing=existing), self.SessionLocal() as db:
                order, event = self.seed_create_event(db)
                order.raw_payload = existing
                client = CanonicalLinkClient("unused")

                result = process_skladbot_create_event(db, event, client)

                self.assertEqual(result["status"], "ambiguous")
                self.assertEqual(client.create_calls, 0)
                self.assertEqual(client.detail_calls, [])
                self.assertNotEqual((event.payload or {}).get("create_status"), "already_linked")
                db.rollback()

    def test_existing_complete_pair_is_already_linked_without_remote_calls(self):
        with self.SessionLocal() as db:
            order, event = self.seed_create_event(db)
            order.raw_payload = {
                "skladbot_request_id": "7001",
                "skladbot_request_number": "WR-7001",
            }
            client = CanonicalLinkClient("unused")

            result = process_skladbot_create_event(db, event, client)

            self.assertEqual(result["status"], "already_linked")
            self.assertEqual((client.create_calls, client.detail_calls), (0, []))
            self.assertEqual((event.payload or {}).get("create_status"), "already_linked")

    def test_existing_id_only_recovers_only_from_matching_durable_exact_id(self):
        with self.SessionLocal() as db:
            order, event = self.seed_create_event(db)
            order.raw_payload = {"skladbot_request_id": "7001"}
            event.payload = {
                **(event.payload or {}),
                "post_state": "response_received",
                "post_response_request_id": "7001",
            }
            client = CanonicalLinkClient(
                "unused",
                detail_id="7001",
                detail_number="WH-R-7001",
            )

            result = process_skladbot_create_event(db, event, client)

            self.assertEqual(result["status"], "created_recovered")
            self.assertEqual(client.create_calls, 0)
            self.assertEqual(client.detail_calls, [7001])
            self.assertEqual(order.raw_payload["skladbot_request_number"], "WH-R-7001")

    def test_valid_wh_r_wr_and_trimmed_values_persist_canonical_pair(self):
        for response_id, detail_id, number in (
            (7001, 7001, "WH-R-7001"),
            ("7001", "7001", "WR-7001"),
            (" 7001 ", " 7001 ", " WH-R-7001 "),
        ):
            with self.subTest(number=number), self.SessionLocal() as db:
                order, event = self.seed_create_event(db)
                client = CanonicalLinkClient(
                    response_id,
                    detail_id=detail_id,
                    detail_number=number,
                )

                result = process_skladbot_create_event(db, event, client)

                self.assertEqual(result["status"], "created")
                self.assertEqual(order.raw_payload["skladbot_request_id"], "7001")
                self.assertEqual(
                    order.raw_payload["skladbot_request_number"],
                    canonical_skladbot_request_number(number),
                )
                db.rollback()

    def test_reconcile_invalid_stored_id_does_not_call_remote(self):
        with self.SessionLocal() as db:
            order, event = self.seed_create_event(db)
            event.payload = {
                **(event.payload or {}),
                "post_state": "response_received",
                "post_response_request_id": "7.001e3",
            }
            client = CanonicalLinkClient("unused")

            result = reconcile_ambiguous_skladbot_request(order, event, client, "")

            self.assertIsNone(result)
            self.assertEqual(client.detail_calls, [])
            self.assertEqual(client.list_calls, 0)

    def test_legacy_exact_marker_recovery_requires_equal_list_and_detail_id(self):
        marker = "TakSklad ref: TSF-AAAAAAAAAAAAAAAAAAAAAAAA"

        class LegacyListClient:
            def __init__(inner_self, detail_id):
                inner_self.detail_id = detail_id
                inner_self.detail_calls = []

            def list_requests(inner_self):
                return [{"id": "123"}]

            def get_request_detail(inner_self, request_id):
                inner_self.detail_calls.append(request_id)
                return {
                    "id": inner_self.detail_id,
                    "delivery_number": "WH-R-123",
                    "comment": marker,
                }

        with self.SessionLocal() as db:
            order, _event = self.seed_create_event(db)
            for detail_id in ("999", "1.23e2", "123.9", ""):
                with self.subTest(detail_id=detail_id):
                    client = LegacyListClient(detail_id)
                    self.assertIsNone(
                        find_existing_skladbot_request_for_order(order, client, marker=marker)
                    )
                    self.assertEqual(client.detail_calls, [123])

            valid_client = LegacyListClient("123")
            recovered = find_existing_skladbot_request_for_order(
                order,
                valid_client,
                marker=marker,
            )
            self.assertIsNotNone(recovered)
            self.assertEqual(canonical_remote_request_id(recovered["id"]), "123")
            self.assertEqual(recovered["number"], "WH-R-123")

    def test_save_entrypoint_requires_canonical_pair(self):
        for order_id, request in (
            (uuid.UUID("00000000-0000-0000-0000-000000007001"), {"id": "7.001e3", "number": "WH-R-7001"}),
            (uuid.UUID("00000000-0000-0000-0000-000000007002"), {"id": "7001", "number": "NOT-A-SKLADBOT-NUMBER"}),
        ):
            with self.subTest(request=request), self.SessionLocal() as db:
                order, event = self.seed_create_event(db, order_id=order_id)

                result = save_skladbot_create_result(
                    db,
                    order,
                    event,
                    {},
                    request,
                    status="created_recovered",
                )

                self.assertEqual(result["status"], "ambiguous")
                self.assertNotIn("skladbot_request_id", order.raw_payload)
                db.rollback()

    def test_worker_exact_recovery_rejects_noncanonical_stored_response_id(self):
        with self.SessionLocal() as db:
            order, event = self.seed_create_event(db)
            event.status = "blocked"
            event.payload = {
                **(event.payload or {}),
                "post_state": "ambiguous",
                "create_status": "ambiguous",
                "post_response_request_id": "7.001e3",
                "post_request_marker": "",
            }
            order.raw_payload = {
                "skladbot_status": "ambiguous",
                "skladbot_create_event_id": str(event.id),
            }
            db.commit()
            order_id = order.id

        candidate = {
            "id": 7001,
            "number": "WH-R-7001",
            "raw": {"detail": {"id": 7001}},
        }
        with mock.patch("backend.app.skladbot_worker.SessionLocal", self.SessionLocal), mock.patch(
            "backend.app.skladbot_worker.fetch_candidate_requests",
            return_value=CandidateRequests([candidate], complete=True),
        ):
            result = update_orders_from_skladbot()

        self.assertEqual(result["matched"], 0)
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            self.assertNotIn("skladbot_request_id", order.raw_payload)
            self.assertNotEqual(order.raw_payload.get("skladbot_status"), "created_recovered")

    def test_worker_and_processor_share_strict_create_event_ownership(self):
        with self.SessionLocal() as db:
            order, event = self.seed_create_event(db)
            payload = dict(event.payload or {})
            marker = order_taksklad_marker(order)
            valid_v2_key = skladbot_create_idempotency_key(str(order.id), marker=marker)
            valid_v1_key = skladbot_create_idempotency_key(str(order.id))
            foreign_v1_key = skladbot_create_idempotency_key(str(uuid.uuid4()))
            other_marker = "TakSklad ref: TSF-BBBBBBBBBBBBBBBBBBBBBBBB"
            scenarios = (
                ("valid_v2", valid_v2_key, event.action, payload, True),
                ("valid_v1", valid_v1_key, event.action, payload, True),
                ("foreign_v1", foreign_v1_key, event.action, payload, False),
                ("random_key", "synthetic-random-key", event.action, payload, False),
                ("wrong_action", valid_v2_key, "wrong_action", payload, False),
                (
                    "marker_mismatch",
                    valid_v2_key,
                    event.action,
                    {**payload, "taksklad_marker": other_marker},
                    False,
                ),
            )
            for name, key, action, candidate_payload, expected in scenarios:
                with self.subTest(name=name):
                    candidate = SimpleNamespace(
                        id=uuid.uuid4(),
                        event_type=event.event_type,
                        action=action,
                        aggregate_type=event.aggregate_type,
                        aggregate_id=event.aggregate_id,
                        idempotency_key=key,
                        payload=candidate_payload,
                    )
                    worker_decision = skladbot_create_event_belongs_to_order(candidate, order)
                    processor_decision = skladbot_create_event_ownership_is_valid(
                        candidate,
                        candidate_payload,
                    )
                    self.assertEqual(worker_decision, expected)
                    self.assertEqual(processor_decision, expected)

    def test_worker_rejects_foreign_v1_key_without_linking_or_completing_event(self):
        with self.SessionLocal() as db:
            order, event = self.seed_create_event(db)
            event.status = "blocked"
            event.idempotency_key = skladbot_create_idempotency_key(str(uuid.uuid4()))
            event.payload = {
                **(event.payload or {}),
                "post_state": "response_received",
                "create_status": "ambiguous",
                "post_response_request_id": "7001",
            }
            order.raw_payload = {
                "skladbot_status": "ambiguous",
                "skladbot_create_event_id": str(event.id),
            }
            db.commit()
            order_id = order.id
            event_id = event.id

        candidate = {
            "id": 7001,
            "number": "WH-R-7001",
            "raw": {"detail": {"id": 7001}},
        }
        with mock.patch("backend.app.skladbot_worker.SessionLocal", self.SessionLocal), mock.patch(
            "backend.app.skladbot_worker.fetch_candidate_requests",
            return_value=CandidateRequests([candidate], complete=True),
        ):
            result = update_orders_from_skladbot()

        self.assertEqual(result["matched"], 0)
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            event = db.get(PendingEvent, event_id)
            self.assertNotIn("skladbot_request_id", order.raw_payload)
            self.assertNotEqual(event.status, "completed")

    def test_fetch_candidates_rejects_noncanonical_list_and_mismatched_detail_ids(self):
        class FetchClient:
            configured = True
            request_delay = 0

            def __init__(inner_self, list_id, detail_id):
                inner_self.list_id = list_id
                inner_self.detail_id = detail_id
                inner_self.detail_calls = []

            def list_requests(inner_self):
                return [{
                    "id": inner_self.list_id,
                    "type": "Отгрузка 3PL",
                    "created_at": "2026-07-17",
                    "delivery_number": "WH-R-7001",
                }]

            def get_request_detail(inner_self, request_id):
                inner_self.detail_calls.append(request_id)
                return {
                    "id": inner_self.detail_id,
                    "type": "Отгрузка 3PL",
                    "delivery_number": "WH-R-7001",
                    "created_at": "2026-07-17",
                }

        for list_id in ("7.001e3", "7001.9"):
            with self.subTest(list_id=list_id):
                client = FetchClient(list_id, "7001")
                result = fetch_candidate_requests(today=date(2026, 7, 17), client=client)
                self.assertEqual(list(result), [])
                self.assertEqual(client.detail_calls, [])
        for detail_id in ("999", "7.001e3", "7001.9"):
            with self.subTest(detail_id=detail_id):
                client = FetchClient("7001", detail_id)
                result = fetch_candidate_requests(today=date(2026, 7, 17), client=client)
                self.assertEqual(list(result), [])
                self.assertFalse(result.complete)
                self.assertEqual(client.detail_calls, [7001])
        client = FetchClient("7001", "7001")
        result = fetch_candidate_requests(today=date(2026, 7, 17), client=client)
        self.assertEqual(len(result), 1)
        self.assertEqual(client.detail_calls, [7001])

    def test_worker_marker_recovery_revalidates_raw_list_detail_evidence(self):
        scenarios = (
            ("mismatch", "A", "123", "999", 0),
            ("scientific", "B", "7.001e3", "7001", 0),
            ("fractional", "C", "7001", "7001.9", 0),
            ("valid", "D", "7001", "7001", 1),
        )
        for scenario_index, (name, marker_char, list_id, detail_id, expected_matched) in enumerate(scenarios):
            if scenario_index:
                Base.metadata.drop_all(self.engine)
                Base.metadata.create_all(self.engine)
            with self.subTest(name=name), self.SessionLocal() as db:
                marker = f"TakSklad ref: TSF-{marker_char * 24}"
                order, event = self.seed_create_event(db)
                event.status = "blocked"
                event.idempotency_key = skladbot_create_idempotency_key(str(order.id))
                legacy_payload = dict(event.payload or {})
                legacy_payload.pop("taksklad_marker", None)
                event.payload = {
                    **legacy_payload,
                    "post_state": "ambiguous",
                    "create_status": "ambiguous",
                    "post_request_marker": marker,
                }
                order.raw_payload = {
                    "skladbot_status": "ambiguous",
                    "skladbot_create_event_id": str(event.id),
                }
                db.commit()
                order_id = order.id
                event_id = event.id

            candidate = self.candidate(list_id=list_id, detail_id=detail_id)
            candidate["comment"] = marker
            with mock.patch("backend.app.skladbot_worker.SessionLocal", self.SessionLocal), mock.patch(
                "backend.app.skladbot_worker.fetch_candidate_requests",
                return_value=CandidateRequests([candidate], complete=True),
            ):
                result = update_orders_from_skladbot()

            self.assertEqual(result["matched"], expected_matched)
            with self.SessionLocal() as db:
                order = db.get(Order, order_id)
                event = db.get(PendingEvent, event_id)
                if expected_matched:
                    self.assertEqual(order.raw_payload.get("skladbot_request_id"), "7001")
                    self.assertEqual(event.status, "completed")
                else:
                    self.assertNotIn("skladbot_request_id", order.raw_payload)
                    self.assertNotEqual(event.status, "completed")

    def assert_worker_rejects_untyped_recovery_result(self, save_status, marker_character):
        with self.SessionLocal() as db:
            marker = f"TakSklad ref: TSF-{marker_character * 24}"
            order, event = self.seed_create_event(db)
            event.status = "blocked"
            event.idempotency_key = skladbot_create_idempotency_key(str(order.id))
            legacy_payload = dict(event.payload or {})
            legacy_payload.pop("taksklad_marker", None)
            event.payload = {
                **legacy_payload,
                "post_state": "ambiguous",
                "create_status": "ambiguous",
                "post_request_marker": marker,
            }
            order.raw_payload = {
                "skladbot_status": "ambiguous",
                "skladbot_create_event_id": str(event.id),
            }
            db.commit()
            order_id = order.id
            event_id = event.id

        candidate = self.candidate()
        candidate["comment"] = marker
        save_result = {"status": save_status, "error": f"synthetic {save_status}"}
        with mock.patch("backend.app.skladbot_worker.SessionLocal", self.SessionLocal), mock.patch(
            "backend.app.skladbot_worker.fetch_candidate_requests",
            return_value=CandidateRequests([candidate], complete=True),
        ), mock.patch(
            "backend.app.skladbot_request_dry_run.save_skladbot_create_result",
            return_value=save_result,
        ) as save_mock:
            first = update_orders_from_skladbot()
            second = update_orders_from_skladbot()

        self.assertEqual(first["matched"], 0)
        self.assertEqual(second["matched"], 0)
        self.assertEqual(save_mock.call_count, 2)
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            event = db.get(PendingEvent, event_id)
            incidents = db.execute(select(Incident).where(
                Incident.pending_event_id == event_id
            )).scalars().all()
            self.assertNotIn("skladbot_request_id", order.raw_payload)
            self.assertEqual(event.status, "blocked")
            self.assertEqual(event.last_error, f"synthetic {save_status}")
            self.assertEqual(len(incidents), 1)

    def test_worker_completes_only_typed_created_recovered_save_result(self):
        self.assert_worker_rejects_untyped_recovery_result("ambiguous", "A")

    def test_worker_rejects_blocked_recovery_save_result(self):
        self.assert_worker_rejects_untyped_recovery_result("blocked", "B")

    def test_worker_rejects_conflict_recovery_save_result(self):
        self.assert_worker_rejects_untyped_recovery_result("conflict", "C")


if __name__ == "__main__":
    unittest.main()
