import copy
import tempfile
import threading
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine, event as sqlalchemy_event, select, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import AuditLog, Base, Incident, Order, OrderItem, PendingEvent, ScanCode
from backend.app.skladbot_client import SkladBotApiError, SkladBotErrorKind
from backend.app.skladbot_contracts import taksklad_marker_from_comment
from backend.app.skladbot_request_dry_run import (
    SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
    claim_next_pending_event_without_lease,
    process_pending_skladbot_request_creates,
    queue_skladbot_create_events,
    reset_stale_skladbot_create_events,
    skladbot_create_idempotency_key,
    stable_payload_hash,
)
from backend.app.skladbot_return_requests import (
    SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE,
    markerless_skladbot_return_comment,
    process_pending_skladbot_return_request_creates,
    process_skladbot_return_create_event,
    queue_skladbot_return_request_create,
    reset_stale_skladbot_return_create_events,
)


class RecordingClient:
    configured = True

    def __init__(self, *, create_result=None, create_error=None, detail_results=None):
        self.create_result = create_result or {"data": {"id": 7001}}
        self.create_error = create_error
        self.detail_results = list(detail_results or [{"id": 7001, "delivery_number": "WH-R-7001"}])
        self.create_calls = 0
        self.list_calls = 0
        self.detail_calls = []
        self.created_payloads = []

    def create_request(self, payload):
        self.create_calls += 1
        self.created_payloads.append(copy.deepcopy(payload))
        if self.create_error is not None:
            raise self.create_error
        return copy.deepcopy(self.create_result)

    def list_requests(self, type_id=None):
        self.list_calls += 1
        return [{"id": 9999, "delivery_number": "WH-R-LOOKALIKE"}]

    def get_request_detail(self, request_id):
        self.detail_calls.append(request_id)
        if not self.detail_results:
            raise AssertionError("unexpected detail call")
        result = self.detail_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)


class SkladBotReturnHardeningTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(
            "os.environ",
            {
                "TAKSKLAD_EVENT_LEASES_ENABLED": "0",
                "SKLADBOT_CREATE_REQUESTS_MODE": "enabled",
                "SKLADBOT_API_MAX_RETRIES": "2",
            },
            clear=False,
        )
        self.env.start()
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.env.stop()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def seed_return_event(self, *, payment_type="Перечисление", with_scan=False):
        with self.SessionLocal() as db:
            order = Order(
                source="test",
                external_id=f"synthetic-{uuid.uuid4()}",
                order_date=date(2026, 7, 17),
                payment_type=payment_type,
                client="Synthetic client",
                address="Synthetic address",
                representative="ТП1",
                status="returned",
                raw_payload={"skladbot_request_id": "6001", "skladbot_request_number": "WH-R-OUT"},
            )
            db.add(order)
            db.flush()
            item = OrderItem(
                order_id=order.id,
                product="Chapman RED OP 20",
                quantity_pieces=20,
                quantity_blocks=2,
                pieces_per_block=10,
                scanned_blocks=1 if with_scan else 0,
                status="not_completed",
                raw_payload={"synthetic": True},
            )
            db.add(item)
            db.flush()
            if with_scan:
                db.add(ScanCode(order_item_id=item.id, code="SYNTHETIC-KIZ", source="test", raw_payload={}))
            confirmed = [{
                "item_id": str(item.id),
                "product": item.product,
                "sku": item.product,
                "quantity_blocks": 2,
                "quantity_pieces": 20,
            }]
            event = queue_skladbot_return_request_create(db, order, confirmed)
            db.commit()
            return order.id, event.id

    def load_event(self, event_id):
        with self.SessionLocal() as db:
            return db.get(PendingEvent, event_id)

    def add_open_return_incident(self, order_id, event_id):
        with self.SessionLocal() as db:
            incident = Incident(
                source="skladbot_return_create",
                severity="critical",
                status="open",
                title="Synthetic return incident",
                entity_type="order",
                entity_id=str(order_id),
                pending_event_id=event_id,
                order_id=order_id,
                raw_payload={"synthetic": True},
            )
            db.add(incident)
            db.commit()
            return incident.id

    def test_return_success_is_markerless_one_post_and_preserves_correlation_audit(self):
        marker = "TakSklad ref: TSF-AAAAAAAAAAAAAAAAAAAAAAAA"
        order_id, event_id = self.seed_return_event(payment_type=f"Перечисление\n{marker}")
        durable_states = {}

        class DurabilityClient(RecordingClient):
            def create_request(inner_self, payload):
                with self.SessionLocal() as observer:
                    durable_event = observer.get(PendingEvent, event_id)
                    durable_states["before_post"] = (
                        durable_event.payload.get("post_state"),
                        durable_event.payload.get("request_payload_hash"),
                    )
                return super().create_request(payload)

            def get_request_detail(inner_self, request_id):
                with self.SessionLocal() as observer:
                    durable_event = observer.get(PendingEvent, event_id)
                    durable_states["before_detail"] = (
                        durable_event.payload.get("post_state"),
                        durable_event.payload.get("post_response_request_id"),
                    )
                return super().get_request_detail(request_id)

        client = DurabilityClient(
            create_result={"data": {"id": 7001}},
            detail_results=[{"id": 7001, "delivery_number": "WH-R-7001"}],
        )

        result = self._process(client)

        self.assertEqual(result["created"], 1)
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(client.list_calls, 0)
        payload = client.created_payloads[0]
        self.assertEqual(payload["comment"], "Перечисление\nТП1")
        self.assertEqual(payload["comment"], payload["fields"]["comment"]["value"])
        self.assertEqual(taksklad_marker_from_comment(payload["comment"]), "")
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            order = db.get(Order, order_id)
            started = db.execute(select(AuditLog).where(
                AuditLog.action == "skladbot_return_request_post_started"
            )).scalar_one()
        self.assertEqual(event.status, "completed")
        self.assertEqual(event.payload["post_state"], "completed")
        self.assertEqual(order.raw_payload["skladbot_return_request_id"], "7001")
        self.assertEqual(durable_states["before_post"], ("started", event.payload["request_payload_hash"]))
        self.assertEqual(durable_states["before_detail"], ("response_received", 7001))
        self.assertEqual(started.payload["idempotency_key"], event.idempotency_key)
        self.assertEqual(started.payload["request_payload_hash"], event.payload["request_payload_hash"])

    def test_markerlike_business_substring_is_preserved(self):
        business = "Примечание TakSklad ref: TSF-AAAAAAAAAAAAAAAAAAAAAAAA внутри текста"
        self.assertEqual(markerless_skladbot_return_comment(business), business)
        self.assertEqual(taksklad_marker_from_comment(business), "")

    def test_stored_queued_and_retry_payloads_are_sanitized_durably_before_post(self):
        technical_smartup = "Smartup ID: smartup:731"
        technical_marker = "TakSklad ref: TSF-AAAAAAAAAAAAAAAAAAAAAAAA"
        business = "Перечисление\nТП Smartup Team"
        legacy_comment = f"{business}\n{technical_smartup}\n{technical_marker}"
        for post_state in ("queued", "retry_scheduled"):
            with self.subTest(post_state=post_state):
                _order_id, event_id = self.seed_return_event()
                stored_payload = {
                    "comment": legacy_comment,
                    "fields": {"comment": {"value": legacy_comment}},
                    "products": [],
                }
                with self.SessionLocal() as db:
                    event = db.get(PendingEvent, event_id)
                    event.payload = {
                        **(event.payload or {}),
                        "post_state": post_state,
                        "request_payload": stored_payload,
                        "request_payload_hash": "stale-hash",
                    }
                    db.commit()

                durable_before_post = {}

                class DurablePayloadClient(RecordingClient):
                    def create_request(inner_self, payload):
                        with self.SessionLocal() as observer:
                            durable = observer.get(PendingEvent, event_id)
                            durable_before_post["payload"] = copy.deepcopy(
                                durable.payload.get("request_payload")
                            )
                            durable_before_post["hash"] = durable.payload.get("request_payload_hash")
                        return super().create_request(payload)

                client = DurablePayloadClient()
                first = self._process(client)
                second = self._process(client)

                self.assertEqual(first["created"], 1)
                self.assertEqual(second["checked"], 0)
                self.assertEqual(client.create_calls, 1)
                sent = client.created_payloads[0]
                self.assertEqual(sent["comment"], business)
                self.assertEqual(sent["fields"]["comment"]["value"], business)
                self.assertNotIn(technical_smartup, sent["comment"])
                self.assertEqual(taksklad_marker_from_comment(sent["comment"]), "")
                self.assertEqual(durable_before_post["payload"], sent)
                self.assertEqual(durable_before_post["hash"], stable_payload_hash(sent))

    def test_timeout_network_and_5xx_two_cycles_total_one_post_and_no_list(self):
        errors = (
            TimeoutError("synthetic timeout"),
            ConnectionError("synthetic network error"),
            SkladBotApiError(
                "synthetic HTTP 503",
                kind=SkladBotErrorKind.SERVER,
                status_code=503,
                ambiguous=True,
            ),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                _order_id, event_id = self.seed_return_event()
                client = RecordingClient(create_error=error)
                first = self._process(client)
                second = self._process(client)
                self.assertEqual(first["ambiguous"], 1)
                self.assertEqual(second["checked"], 0)
                self.assertEqual(client.create_calls, 1)
                self.assertEqual(client.list_calls, 0)
                self.assertEqual(client.detail_calls, [])
                self.assertEqual(self.load_event(event_id).status, "blocked")

    def test_malformed_create_id_blocks_without_lookup_or_repost(self):
        _order_id, event_id = self.seed_return_event()
        client = RecordingClient(create_result={"data": {}}, detail_results=[])
        first = self._process(client)
        second = self._process(client)
        self.assertEqual(first["ambiguous"], 1)
        self.assertEqual(second["checked"], 0)
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(client.list_calls, 0)
        self.assertEqual(client.detail_calls, [])
        self.assertEqual(self.load_event(event_id).payload["manual_review_reason"], "malformed_create_response")

    def test_response_id_detail_timeout_recovers_next_cycle_by_exact_get_only(self):
        order_id, event_id = self.seed_return_event()
        client = RecordingClient(
            create_result={"data": {"id": 7002}},
            detail_results=[TimeoutError("synthetic detail timeout"), {"id": 7002, "delivery_number": "WH-R-7002"}],
        )
        first = self._process(client)
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            self.assertEqual(event.status, "pending")
            self.assertGreater(event.available_at.replace(tzinfo=timezone.utc), datetime.now(timezone.utc))
            event.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
        second = self._process(client)
        self.assertEqual(first["created"], 0)
        self.assertEqual(second["recovered"], 1)
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(client.list_calls, 0)
        self.assertEqual(client.detail_calls, [7002, 7002])
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            incident = db.execute(select(Incident).where(Incident.pending_event_id == event_id)).scalar_one()
        self.assertEqual(order.raw_payload["skladbot_return_request_id"], "7002")
        self.assertEqual(incident.status, "resolved")
        self.assertIsNotNone(incident.resolved_at)

    def test_exact_detail_retry_limit_blocks_without_repost(self):
        _order_id, event_id = self.seed_return_event()
        client = RecordingClient(
            create_result={"data": {"id": 7004}},
            detail_results=[TimeoutError("first detail timeout"), TimeoutError("second detail timeout")],
        )
        with mock.patch.dict("os.environ", {"SKLADBOT_API_MAX_RETRIES": "1"}, clear=False):
            first = self._process(client)
            with self.SessionLocal() as db:
                event = db.get(PendingEvent, event_id)
                event.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                db.commit()
            second = self._process(client)
            third = self._process(client)
        self.assertEqual(first["checked"], 1)
        self.assertEqual(second["ambiguous"], 1)
        self.assertEqual(third["checked"], 0)
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(client.list_calls, 0)
        self.assertEqual(client.detail_calls, [7004, 7004])
        event = self.load_event(event_id)
        self.assertEqual(event.payload["manual_review_reason"], "exact_detail_retry_exhausted")
        self.assertEqual(event.payload["detail_recovery_attempts"], 2)
        self.assertTrue(event.payload["detail_retry_exhausted_at"])

    def test_exact_detail_deterministic_4xx_is_terminal_without_retry(self):
        _order_id, event_id = self.seed_return_event()
        client = RecordingClient(
            create_result={"data": {"id": 7005}},
            detail_results=[SkladBotApiError(
                "synthetic detail auth",
                kind=SkladBotErrorKind.AUTH,
                status_code=401,
            )],
        )
        first = self._process(client)
        second = self._process(client)
        self.assertEqual(first["blocked"], 1)
        self.assertEqual(second["checked"], 0)
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(client.list_calls, 0)
        self.assertEqual(client.detail_calls, [7005])
        self.assertEqual(
            self.load_event(event_id).payload["manual_review_reason"],
            "exact_detail_rejected_auth",
        )

    def test_detail_id_mismatch_and_empty_wh_r_never_link(self):
        details = (
            ({"id": 7999, "delivery_number": "WH-R-7999"}, "canonical_detail_id_mismatch"),
            ({"id": 7003, "delivery_number": ""}, "canonical_wh_r_empty"),
        )
        for detail, reason in details:
            with self.subTest(reason=reason):
                order_id, event_id = self.seed_return_event()
                client = RecordingClient(create_result={"data": {"id": 7003}}, detail_results=[detail])
                result = self._process(client)
                with self.SessionLocal() as db:
                    order = db.get(Order, order_id)
                    event = db.get(PendingEvent, event_id)
                self.assertEqual(result["ambiguous"], 1)
                self.assertNotIn("skladbot_return_request_id", order.raw_payload)
                self.assertEqual(event.payload["manual_review_reason"], reason)
                self.assertEqual(client.list_calls, 0)

    def test_429_lease_finalize_preserves_future_backoff_and_immediate_cycle_skips(self):
        _order_id, event_id = self.seed_return_event()
        client = RecordingClient(create_error=SkladBotApiError(
            "synthetic HTTP 429",
            kind=SkladBotErrorKind.RATE_LIMIT,
            status_code=429,
            ambiguous=False,
        ))
        with mock.patch.dict("os.environ", {"TAKSKLAD_EVENT_LEASES_ENABLED": "1"}, clear=False):
            first = self._process(client)
            with self.SessionLocal() as db:
                event = db.get(PendingEvent, event_id)
                retry_at = datetime.fromisoformat(event.payload["retry_at"])
                available_at = event.available_at.replace(tzinfo=timezone.utc)
            second = self._process(client)
        self.assertEqual(first["checked"], 1)
        self.assertEqual(second["checked"], 0)
        self.assertEqual(client.create_calls, 1)
        self.assertLess(abs((available_at - retry_at).total_seconds()), 1)
        self.assertGreater(available_at, datetime.now(timezone.utc) + timedelta(minutes=4))

    def test_deterministic_auth_client_and_stock_errors_are_terminal_without_mutations(self):
        errors = (
            SkladBotApiError("synthetic auth", kind=SkladBotErrorKind.AUTH, status_code=401),
            SkladBotApiError("synthetic client", kind=SkladBotErrorKind.CLIENT, status_code=422),
            SkladBotApiError("synthetic stock", kind=SkladBotErrorKind.STOCK_SHORTAGE, status_code=409),
        )
        for error in errors:
            with self.subTest(kind=error.kind):
                order_id, event_id = self.seed_return_event(with_scan=True)
                before = self._order_snapshot(order_id)
                client = RecordingClient(create_error=error)
                first = self._process(client)
                second = self._process(client)
                self.assertEqual(first["blocked"], 1)
                self.assertEqual(second["checked"], 0)
                self.assertEqual(client.create_calls, 1)
                self.assertEqual(client.list_calls, 0)
                self.assertEqual(self._order_snapshot(order_id), before)
                with self.SessionLocal() as db:
                    incident = db.execute(select(Incident).where(Incident.pending_event_id == event_id)).scalar_one()
                self.assertEqual(incident.status, "manual_review")

    def test_stale_started_is_manual_review_without_post_with_leases_off_and_on(self):
        for leases_enabled in ("0", "1"):
            with self.subTest(leases_enabled=leases_enabled):
                _order_id, event_id = self.seed_return_event()
                old = datetime.now(timezone.utc) - timedelta(minutes=30)
                with self.SessionLocal() as db:
                    event = db.get(PendingEvent, event_id)
                    event.status = "processing"
                    event.attempts = 1
                    event.payload = {**(event.payload or {}), "post_state": "started"}
                    db.commit()
                    db.execute(update(PendingEvent).where(PendingEvent.id == event_id).values(updated_at=old))
                    db.commit()
                client = RecordingClient()
                with mock.patch.dict("os.environ", {"TAKSKLAD_EVENT_LEASES_ENABLED": leases_enabled}, clear=False):
                    result = self._process(client)
                self.assertEqual(result["ambiguous"], 1)
                self.assertEqual(client.create_calls, 0)
                self.assertEqual(client.list_calls, 0)
                self.assertEqual(client.detail_calls, [])
                self.assertEqual(self.load_event(event_id).payload["post_state"], "ambiguous")

    def test_first_queued_claim_posts_but_legacy_attempt_without_state_does_not(self):
        _order_id, first_event_id = self.seed_return_event()
        first_client = RecordingClient()
        first = self._process(first_client)
        self.assertEqual(first["created"], 1)
        self.assertEqual(first_client.create_calls, 1)
        self.assertEqual(self.load_event(first_event_id).attempts, 1)

        _order_id, legacy_event_id = self.seed_return_event()
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, legacy_event_id)
            event.status = "failed"
            event.attempts = 1
            event.payload = {key: value for key, value in (event.payload or {}).items() if key != "post_state"}
            db.commit()
        legacy_client = RecordingClient()
        legacy = self._process(legacy_client)
        self.assertEqual(legacy["ambiguous"], 1)
        self.assertEqual(legacy_client.create_calls, 0)
        self.assertEqual(legacy_client.list_calls, 0)

    def test_preloaded_legacy_event_refreshes_after_cas_and_never_posts(self):
        _order_id, event_id = self.seed_return_event()
        client = RecordingClient()
        with self.SessionLocal() as db:
            preloaded = db.get(PendingEvent, event_id)
            preloaded.status = "failed"
            preloaded.attempts = 1
            preloaded.payload = {
                key: value
                for key, value in (preloaded.payload or {}).items()
                if key != "post_state"
            }
            db.commit()

            claimed = claim_next_pending_event_without_lease(
                db,
                event_type=SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE,
            )
            self.assertIs(claimed, preloaded)
            self.assertEqual(claimed.status, "processing")
            self.assertEqual(claimed.attempts, 2)

            result = process_skladbot_return_create_event(db, claimed, client)
            db.commit()
            incident = db.execute(
                select(Incident).where(Incident.pending_event_id == event_id)
            ).scalar_one()

        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(client.list_calls, 0)
        self.assertEqual(client.detail_calls, [])
        self.assertEqual(incident.status, "manual_review")
        self.assertEqual(self.load_event(event_id).status, "blocked")

    def test_foreign_event_is_blocked_before_remote_or_order_mutation(self):
        order_id, event_id = self.seed_return_event(with_scan=True)
        before = self._order_snapshot(order_id)
        client = RecordingClient()
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            event.aggregate_id = str(uuid.uuid4())
            result = process_skladbot_return_create_event(db, event, client)
            db.commit()
            incident = db.execute(select(Incident).where(Incident.pending_event_id == event_id)).scalar_one()
            audit = db.execute(select(AuditLog).where(
                AuditLog.action == "skladbot_return_create_manual_review"
            )).scalar_one()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(client.list_calls, 0)
        self.assertEqual(client.detail_calls, [])
        self.assertEqual(self._order_snapshot(order_id), before)
        self.assertEqual(self.load_event(event_id).status, "blocked")
        self.assertEqual(incident.status, "manual_review")
        self.assertEqual(audit.payload["reason"], "ownership_invalid")

    def test_fuzzy_lookalike_is_never_automatic_proof_and_already_linked_never_posts(self):
        _order_id, fuzzy_event_id = self.seed_return_event()
        with self.SessionLocal() as db:
            event = db.get(PendingEvent, fuzzy_event_id)
            event.status = "failed"
            event.attempts = 1
            db.commit()
        fuzzy_client = RecordingClient()
        fuzzy = self._process(fuzzy_client)
        self.assertEqual(fuzzy["ambiguous"], 1)
        self.assertEqual(fuzzy_client.create_calls, 0)
        self.assertEqual(fuzzy_client.list_calls, 0)

        order_id, _event_id = self.seed_return_event()
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            order.raw_payload = {
                **(order.raw_payload or {}),
                "skladbot_return_request_id": "7100",
                "skladbot_return_request_number": "WH-R-7100",
            }
            db.commit()
        linked_client = RecordingClient()
        linked = self._process(linked_client)
        self.assertEqual(linked["already_linked"], 1)
        self.assertEqual(linked_client.create_calls, 0)
        self.assertEqual(linked_client.list_calls, 0)
        self.assertEqual(linked_client.detail_calls, [])

    def test_already_linked_wr_number_resolves_existing_incident_without_remote_calls(self):
        order_id, event_id = self.seed_return_event()
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            order.raw_payload = {
                **(order.raw_payload or {}),
                "skladbot_return_request_id": "7101",
                "skladbot_return_request_number": "WR-7101",
            }
            incident = Incident(
                source="skladbot_return_create",
                severity="critical",
                status="open",
                title="Synthetic return incident",
                entity_type="order",
                entity_id=str(order_id),
                pending_event_id=event_id,
                order_id=order_id,
                raw_payload={"synthetic": True},
            )
            db.add(incident)
            db.commit()
            incident_id = incident.id

        client = RecordingClient()
        result = self._process(client)

        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            incident = db.get(Incident, incident_id)
            audit = db.execute(select(AuditLog).where(
                AuditLog.action == "skladbot_return_create_incidents_resolved"
            )).scalar_one()
        self.assertEqual(result["already_linked"], 1)
        self.assertEqual(event.status, "completed")
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(client.list_calls, 0)
        self.assertEqual(client.detail_calls, [])
        self.assertEqual(incident.status, "resolved")
        self.assertIsNotNone(incident.resolved_at)
        self.assertEqual(audit.payload["reason"], "already_linked")
        self.assertEqual(audit.payload["resolved_count"], 1)

    def test_id_only_link_is_manual_and_queue_guard_does_not_skip_it(self):
        order_id, event_id = self.seed_return_event()
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            event = db.get(PendingEvent, event_id)
            order.raw_payload = {
                **(order.raw_payload or {}),
                "skladbot_return_request_id": "7102",
            }
            confirmed = list((event.payload or {}).get("confirmed_items") or [])
            queued = queue_skladbot_return_request_create(db, order, confirmed)
            db.commit()
            self.assertIsNotNone(queued)
            self.assertEqual(queued.id, event_id)
        incident_id = self.add_open_return_incident(order_id, event_id)

        client = RecordingClient()
        result = self._process(client)

        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            incident = db.get(Incident, incident_id)
        self.assertEqual(result["ambiguous"], 1)
        self.assertEqual(event.status, "blocked")
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(client.list_calls, 0)
        self.assertEqual(client.detail_calls, [])
        self.assertEqual(incident.status, "manual_review")
        self.assertIsNone(incident.resolved_at)

    def test_number_only_link_is_manual_and_queue_guard_does_not_skip_it(self):
        order_id, event_id = self.seed_return_event()
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            event = db.get(PendingEvent, event_id)
            order.raw_payload = {
                **(order.raw_payload or {}),
                "skladbot_return_request_number": "WH-R-7102",
            }
            confirmed = list((event.payload or {}).get("confirmed_items") or [])
            queued = queue_skladbot_return_request_create(db, order, confirmed)
            db.commit()
            self.assertIsNotNone(queued)
            self.assertEqual(queued.id, event_id)

        client = RecordingClient()
        self._process(client)

        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            incident = db.execute(select(Incident).where(Incident.pending_event_id == event_id)).scalar_one()
        self.assertEqual(event.status, "blocked")
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(client.list_calls, 0)
        self.assertEqual(client.detail_calls, [])
        self.assertEqual(incident.status, "manual_review")

    def test_complete_link_conflicting_with_durable_response_id_is_manual(self):
        order_id, event_id = self.seed_return_event()
        incident_id = self.add_open_return_incident(order_id, event_id)
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            event = db.get(PendingEvent, event_id)
            order.raw_payload = {
                **(order.raw_payload or {}),
                "skladbot_return_request_id": "7102",
                "skladbot_return_request_number": "WH-R-7102",
            }
            event.payload = {
                **(event.payload or {}),
                "post_state": "response_received",
                "post_response_request_id": 7103,
            }
            db.commit()

        client = RecordingClient()
        self._process(client)

        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            incident = db.get(Incident, incident_id)
            audit = db.execute(select(AuditLog).where(
                AuditLog.action == "skladbot_return_create_manual_review"
            )).scalar_one()
        self.assertEqual(event.status, "blocked")
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(client.list_calls, 0)
        self.assertEqual(client.detail_calls, [])
        self.assertEqual(incident.status, "manual_review")
        self.assertIsNone(incident.resolved_at)
        self.assertEqual(audit.payload["reason"], "existing_link_response_id_mismatch")

    def test_complete_link_requires_missing_or_strict_equal_durable_response_id(self):
        scenarios = (
            ("scientific", "7.001e3", False, "existing_link_response_id_malformed"),
            ("fractional", "7001.9", False, "existing_link_response_id_malformed"),
            ("whitespace_only", "   ", False, "existing_link_response_id_malformed"),
            ("unequal", "7002", False, "existing_link_response_id_mismatch"),
            ("equal", "7001", True, ""),
            ("missing", None, True, ""),
        )
        for name, durable_id, succeeds, expected_reason in scenarios:
            with self.subTest(name=name):
                order_id, event_id = self.seed_return_event()
                with self.SessionLocal() as db:
                    order = db.get(Order, order_id)
                    event = db.get(PendingEvent, event_id)
                    order.raw_payload = {
                        **(order.raw_payload or {}),
                        "skladbot_return_request_id": "7001",
                        "skladbot_return_request_number": "WH-R-7001",
                    }
                    payload = {**(event.payload or {}), "post_state": "response_received"}
                    if durable_id is None:
                        payload.pop("post_response_request_id", None)
                    else:
                        payload["post_response_request_id"] = durable_id
                    event.payload = payload
                    db.commit()

                client = RecordingClient()
                self._process(client)

                with self.SessionLocal() as db:
                    event = db.get(PendingEvent, event_id)
                    audits = db.execute(select(AuditLog).where(
                        AuditLog.entity_id == str(event_id),
                        AuditLog.action == "skladbot_return_create_manual_review",
                    )).scalars().all()
                self.assertEqual(event.status, "completed" if succeeds else "blocked")
                self.assertEqual(client.create_calls, 0)
                self.assertEqual(client.list_calls, 0)
                self.assertEqual(client.detail_calls, [])
                if succeeds:
                    self.assertEqual(audits, [])
                else:
                    self.assertEqual(audits[-1].payload["reason"], expected_reason)

    def test_id_only_matching_durable_response_recovers_wr_by_exact_detail(self):
        order_id, event_id = self.seed_return_event()
        incident_id = self.add_open_return_incident(order_id, event_id)
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            event = db.get(PendingEvent, event_id)
            order.raw_payload = {
                **(order.raw_payload or {}),
                "skladbot_return_request_id": "7102",
            }
            event.payload = {
                **(event.payload or {}),
                "post_state": "response_received",
                "post_response_request_id": 7102,
            }
            db.commit()

        client = RecordingClient(detail_results=[{"id": 7102, "delivery_number": "WR-7102"}])
        result = self._process(client)

        with self.SessionLocal() as db:
            event = db.get(PendingEvent, event_id)
            order = db.get(Order, order_id)
            incident = db.get(Incident, incident_id)
            audit = db.execute(select(AuditLog).where(
                AuditLog.action == "skladbot_return_create_incidents_resolved"
            )).scalar_one()
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(event.status, "completed")
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(client.list_calls, 0)
        self.assertEqual(client.detail_calls, [7102])
        self.assertEqual(order.raw_payload["skladbot_return_request_id"], "7102")
        self.assertEqual(order.raw_payload["skladbot_return_request_number"], "WR-7102")
        self.assertEqual(incident.status, "resolved")
        self.assertIsNotNone(incident.resolved_at)
        self.assertEqual(audit.payload["reason"], "created_recovered")

    def test_invalid_return_number_near_misses_are_manual_without_remote_calls(self):
        invalid_numbers = (
            "W-R-7104",
            "WHR-7104",
            "wr-7104",
            "WR-",
            "WR--7104",
            "WR-7104 bad",
            "WR-7104/extra",
        )
        event_ids = []
        for invalid_number in invalid_numbers:
            order_id, event_id = self.seed_return_event()
            event_ids.append(event_id)
            with self.SessionLocal() as db:
                order = db.get(Order, order_id)
                order.raw_payload = {
                    **(order.raw_payload or {}),
                    "skladbot_return_request_id": "7104",
                    "skladbot_return_request_number": invalid_number,
                }
                db.commit()

        client = RecordingClient()
        result = self._process(client)

        with self.SessionLocal() as db:
            events = [db.get(PendingEvent, event_id) for event_id in event_ids]
            incidents = db.execute(select(Incident).where(Incident.pending_event_id.in_(event_ids))).scalars().all()
        self.assertEqual(result["ambiguous"], len(invalid_numbers))
        self.assertTrue(all(event.status == "blocked" for event in events))
        self.assertEqual(client.create_calls, 0)
        self.assertEqual(client.list_calls, 0)
        self.assertEqual(client.detail_calls, [])
        self.assertEqual(len(incidents), len(invalid_numbers))
        self.assertTrue(all(incident.status == "manual_review" for incident in incidents))
        self.assertTrue(all(incident.resolved_at is None for incident in incidents))

    def _process(self, client):
        with self.SessionLocal() as db:
            return process_pending_skladbot_return_request_creates(db, client=client)

    def _order_snapshot(self, order_id):
        with self.SessionLocal() as db:
            order = db.get(Order, order_id)
            items = db.execute(select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.id)).scalars().all()
            scans = db.execute(
                select(ScanCode).join(OrderItem).where(OrderItem.order_id == order_id).order_by(ScanCode.id)
            ).scalars().all()
            return (
                copy.deepcopy(order.raw_payload),
                tuple((item.id, item.status, item.scanned_blocks, copy.deepcopy(item.raw_payload)) for item in items),
                tuple((scan.id, scan.code, copy.deepcopy(scan.raw_payload)) for scan in scans),
            )


class NonLeaseProcessorOverlapTests(unittest.TestCase):
    def test_order_retry_scheduled_two_workers_make_one_post(self):
        self._assert_two_workers_make_one_post(return_create=False)

    def test_return_retry_scheduled_two_workers_make_one_post(self):
        self._assert_two_workers_make_one_post(return_create=True)

    def test_order_stale_reset_two_workers_make_one_post(self):
        self._assert_stale_reset_two_workers_make_one_post(return_create=False)

    def test_return_stale_reset_two_workers_make_one_post(self):
        self._assert_stale_reset_two_workers_make_one_post(return_create=True)

    def _assert_stale_reset_two_workers_make_one_post(self, *, return_create):
        with tempfile.TemporaryDirectory(prefix="taksklad-stale-reset-") as temp_dir:
            engine = create_engine(
                f"sqlite+pysqlite:///{Path(temp_dir) / 'events.db'}",
                connect_args={"check_same_thread": False, "timeout": 5},
            )
            Base.metadata.create_all(engine)
            SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
            with SessionLocal() as db:
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
                order.items.append(OrderItem(
                    product="Chapman RED OP 20",
                    quantity_pieces=20,
                    quantity_blocks=2,
                    pieces_per_block=10,
                    status="not_completed",
                    raw_payload={},
                ))
                db.add(order)
                db.flush()
                if return_create:
                    item = order.items[0]
                    event = queue_skladbot_return_request_create(db, order, [{
                        "item_id": str(item.id),
                        "product": item.product,
                        "sku": item.product,
                        "quantity_blocks": 2,
                        "quantity_pieces": 20,
                    }])
                else:
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
                event.status = "processing"
                event.attempts = 1
                event.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
                event.payload = {**(event.payload or {}), "post_state": "retry_scheduled"}
                db.commit()

            barrier = threading.Barrier(2)
            errors = []
            results = []
            lock = threading.Lock()

            class RacingClient(RecordingClient):
                def create_request(inner_self, payload):
                    with lock:
                        return super(RacingClient, inner_self).create_request(payload)

            client = RacingClient()

            def synchronize_reset(_conn, _cursor, _statement, _parameters, context, _executemany):
                if context.execution_options.get("taksklad_stale_reset_candidate"):
                    barrier.wait(timeout=5)

            sqlalchemy_event.listen(engine, "after_cursor_execute", synchronize_reset)

            def worker():
                try:
                    with SessionLocal() as db:
                        result = (
                            process_pending_skladbot_return_request_creates(db, client=client, limit=1)
                            if return_create
                            else process_pending_skladbot_request_creates(db, client=client, limit=1)
                        )
                    with lock:
                        results.append(result)
                except Exception as exc:  # pragma: no cover - asserted below
                    with lock:
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
                    thread.join(timeout=15)
            sqlalchemy_event.remove(engine, "after_cursor_execute", synchronize_reset)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(client.create_calls, 1)
            self.assertEqual(sum(result["created"] for result in results), 1)
            with SessionLocal() as db:
                event = db.execute(select(PendingEvent).where(
                    PendingEvent.event_type == (
                        SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE
                        if return_create
                        else SKLADBOT_REQUEST_CREATE_EVENT_TYPE
                    )
                )).scalar_one()
                self.assertEqual(event.status, "completed")
                self.assertEqual(event.attempts, 2)
            Base.metadata.drop_all(engine)
            engine.dispose()

    def _assert_two_workers_make_one_post(self, *, return_create):
        with tempfile.TemporaryDirectory(prefix="taksklad-nonlease-") as temp_dir:
            engine = create_engine(
                f"sqlite+pysqlite:///{Path(temp_dir) / 'events.db'}",
                connect_args={"check_same_thread": False, "timeout": 5},
            )
            Base.metadata.create_all(engine)
            SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
            with SessionLocal() as db:
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
                    raise AssertionError("atomic claim must not require fuzzy lookup")

                def get_request_detail(self, request_id):
                    return {"id": request_id, "delivery_number": f"WH-R-{request_id}"}

            client = RacingClient()

            def synchronize_atomic_claim(_conn, _cursor, _statement, _parameters, context, _executemany):
                if context.execution_options.get("taksklad_nonlease_claim"):
                    claim_barrier.wait(timeout=5)

            sqlalchemy_event.listen(engine, "before_cursor_execute", synchronize_atomic_claim)

            def worker():
                try:
                    with SessionLocal() as db:
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
                    thread.join(timeout=10)
            sqlalchemy_event.remove(engine, "before_cursor_execute", synchronize_atomic_claim)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(sorted(result["checked"] for result in results), [0, 1])
            self.assertEqual(sum(result["created"] for result in results), 1)
            loser = next(result for result in results if result["checked"] == 0)
            self.assertEqual(loser["created"], 0)
            self.assertEqual(client.create_calls, 1)
            with SessionLocal() as db:
                event = db.get(PendingEvent, event_id)
            self.assertEqual(event.attempts, 2)
            self.assertEqual(event.status, "completed")
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
