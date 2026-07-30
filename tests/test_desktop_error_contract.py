import uuid
import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app import orders_service
from backend.app.models import Base, Order, OrderItem
from backend.app.schemas import ScanCreate, ScanUndo
from taksklad import backend_events
from taksklad.backend_client import BackendApiError, format_backend_error


KNOWN_SCAN_ERROR_CODES = (
    "kiz_format_invalid",
    "kiz_already_owned",
    "order_item_fully_scanned_new_code",
    "order_closed",
    "transfer_order_irreversible",
    "legal_entity_unresolved",
    "aggregate_box_product_mismatch",
    "aggregate_box_exceeds_plan",
    "scan_product_mismatch",
    "shipment_manifest_mismatch",
)

LEGACY_NON_RETRYABLE_MARKERS = {
    "kiz_format_invalid": (),
    "kiz_already_owned": (
        "Code already scanned in another order item",
        "Code already scanned for another order item",
    ),
    "order_item_fully_scanned_new_code": (
        "order_item_fully_scanned_new_code",
        "Order item is already fully scanned",
    ),
    "order_closed": (),
    "transfer_order_irreversible": (),
    "legal_entity_unresolved": (),
    "aggregate_box_product_mismatch": (
        "Aggregate box product does not match order item",
    ),
    "aggregate_box_exceeds_plan": (
        "Aggregate box exceeds remaining order item blocks",
    ),
    "scan_product_mismatch": (
        "Scan product does not match order item",
    ),
    "shipment_manifest_mismatch": (),
}


def conflict_error(*, detail):
    return BackendApiError(
        "Backend HTTP 409",
        status_code=409,
        detail=detail,
    )


@contextmanager
def sqlite_session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            yield db
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


class DesktopErrorContractTests(unittest.TestCase):
    def sync_scan_error(self, exc):
        pending = [{
            "id": "scan-error-contract",
            "type": "scan",
            "payload": {
                "order_item_id": "item-1",
                "code": "01000000000000000001",
                "scanned_at": "2026-07-30T10:00:00+05:00",
            },
            "attempts": 0,
            "last_error": "",
        }]
        saved = []

        def reconcile(_section, _snapshot, remaining):
            saved.append(list(remaining))
            return list(remaining)

        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "load_pending_backend_events", return_value=pending),
            mock.patch.object(backend_events, "create_scan", side_effect=exc),
            mock.patch.object(backend_events, "reconcile_queue_section", side_effect=reconcile),
            mock.patch.object(backend_events, "record_blocked_backend_events") as record,
        ):
            result = backend_events.sync_pending_backend_events()

        return result, saved, record

    def assert_error_message(self, exc, *, code, message):
        self.assertIsInstance(exc.detail, dict)
        self.assertEqual(exc.detail["code"], code)
        self.assertEqual(exc.detail["message"], message)
        self.assertEqual(
            format_backend_error(exc.status_code, exc.detail),
            f"Backend HTTP {exc.status_code}: {message}",
        )

    def test_known_machine_codes_are_non_retryable(self):
        self.assertEqual(
            backend_events.KNOWN_NON_RETRYABLE_SCAN_ERROR_CODES,
            frozenset(KNOWN_SCAN_ERROR_CODES),
        )
        for code in KNOWN_SCAN_ERROR_CODES:
            with self.subTest(code=code):
                exc = conflict_error(detail={"code": code, "message": "New backend wording"})
                self.assertTrue(backend_events.is_non_retryable_scan_conflict(exc))

    def test_known_codes_keep_existing_text_fallbacks_where_available(self):
        for code, markers in LEGACY_NON_RETRYABLE_MARKERS.items():
            for marker in markers:
                with self.subTest(code=code, marker=marker):
                    exc = conflict_error(detail={"message": marker})
                    self.assertTrue(backend_events.is_non_retryable_scan_conflict(exc))

    def test_unknown_machine_code_is_non_retryable_and_terminal_in_queue(self):
        exc = conflict_error(detail={
            "code": "future_backend_guard",
            "message": "A future conflict with unfamiliar wording",
        })
        result, saved, record = self.sync_scan_error(exc)

        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(saved, [[]])
        self.assertEqual(result["blocked_events"][0]["attempts"], 1)
        self.assertEqual(
            result["blocked_events"][0]["last_error_detail"]["code"],
            "future_backend_guard",
        )
        record.assert_called_once_with(result["blocked_events"])

    def test_ack_code_without_legacy_marker_is_synced_and_not_blocked(self):
        exc = conflict_error(detail={
            "code": "scan_duplicate_ack",
            "message": "The scan was accepted earlier",
        })
        result, saved, record = self.sync_scan_error(exc)

        self.assertTrue(backend_events.is_duplicate_scan_ack(exc))
        self.assertFalse(backend_events.is_non_retryable_scan_conflict(exc))
        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["blocked"], 0)
        self.assertEqual(result["dropped"], 0)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(saved, [[]])
        record.assert_called_once_with([])

    def test_legacy_ack_without_code_is_synced_and_not_blocked(self):
        exc = conflict_error(detail={"message": "Already scanned for this order item"})
        result, saved, record = self.sync_scan_error(exc)

        self.assertTrue(backend_events.is_duplicate_scan_ack(exc))
        self.assertFalse(backend_events.is_non_retryable_scan_conflict(exc))
        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["blocked"], 0)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(saved, [[]])
        record.assert_called_once_with([])

    def test_legacy_ack_marker_wins_before_unknown_machine_conflict(self):
        exc = conflict_error(detail={
            "code": "future_backend_guard",
            "message": "Already scanned for this order item",
        })
        result, _saved, _record = self.sync_scan_error(exc)

        self.assertTrue(backend_events.is_duplicate_scan_ack(exc))
        self.assertTrue(backend_events.is_non_retryable_scan_conflict(exc))
        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["blocked"], 0)

    def test_backend_response_without_code_uses_legacy_substring(self):
        exc = conflict_error(detail={"message": "Scan product does not match order item"})

        self.assertEqual(backend_events.backend_error_code(exc), "")
        self.assertTrue(backend_events.is_non_retryable_scan_conflict(exc))

    def test_backend_duplicate_returns_same_success_with_and_without_scanned_at(self):
        scanned_at = datetime(2026, 7, 30, 5, 0)
        with sqlite_session() as db:
            order = Order(
                payment_type="cash",
                client="Contract Client",
                address="Contract Address",
                status="not_completed",
                raw_payload={"source": "test"},
            )
            item = OrderItem(
                order=order,
                product="Test Product",
                quantity_pieces=20,
                quantity_blocks=2,
                pieces_per_block=10,
                scanned_blocks=0,
                requires_kiz=True,
                status="not_completed",
                raw_payload={"source": "test"},
            )
            db.add_all([order, item])
            db.commit()

            payload = ScanCreate(
                order_item_id=str(item.id),
                code="010123456789",
                workstation_id="contract-test",
                scanned_at=scanned_at,
            )
            first = orders_service.create_scan(db, payload)
            duplicate_with_scanned_at = orders_service.create_scan(db, payload)
            duplicate_without_scanned_at = orders_service.create_scan(
                db,
                ScanCreate(
                    order_item_id=str(item.id),
                    code=payload.code,
                    workstation_id="contract-test",
                ),
            )

        self.assertEqual(duplicate_with_scanned_at, first)
        self.assertEqual(duplicate_without_scanned_at, first)

    def test_scan_time_mismatches_use_specific_machine_codes(self):
        with sqlite_session() as db:
            def add_item(*, product, quantity_blocks):
                order = Order(
                    payment_type="cash",
                    client="Mismatch Client",
                    address="Mismatch Address",
                    status="not_completed",
                    raw_payload={"source": "test"},
                )
                item = OrderItem(
                    order=order,
                    product=product,
                    quantity_pieces=quantity_blocks * 10,
                    quantity_blocks=quantity_blocks,
                    pieces_per_block=10,
                    scanned_blocks=0,
                    requires_kiz=True,
                    status="not_completed",
                    raw_payload={"source": "test"},
                )
                db.add_all([order, item])
                return item

            aggregate_product_item = add_item(product="Chapman RED OP 20", quantity_blocks=150)
            aggregate_capacity_item = add_item(product="Chapman Gold SSL 100`20", quantity_blocks=30)
            unit_product_item = add_item(product="Chapman Gold SSL 100`20", quantity_blocks=1)
            db.commit()

            cases = (
                (
                    aggregate_product_item,
                    "010400639605401221UZ1112022525522513824013040046110ZIG1218229310000",
                    "aggregate_box_product_mismatch",
                    "Aggregate box product does not match order item",
                ),
                (
                    aggregate_capacity_item,
                    "010400639605401221UZ1112022525522513824013040046110ZIG1218229310000",
                    "aggregate_box_exceeds_plan",
                    "Aggregate box exceeds remaining order item blocks",
                ),
                (
                    unit_product_item,
                    "0104006396053947217p-30o933ZXHZKjx",
                    "scan_product_mismatch",
                    "Scan product does not match order item",
                ),
            )
            for item, scan_code, error_code, message in cases:
                with self.subTest(code=error_code):
                    with self.assertRaises(orders_service.ApiError) as raised:
                        orders_service.create_scan(
                            db,
                            ScanCreate(order_item_id=str(item.id), code=scan_code),
                        )
                    self.assert_error_message(
                        raised.exception,
                        code=error_code,
                        message=message,
                    )

    def test_changed_detail_dicts_preserve_exact_messages_for_web_and_desktop(self):
        empty_create = ScanCreate.model_construct(
            order_item_id=str(uuid.uuid4()),
            code="",
            workstation_id=None,
            scanned_by=None,
            scanned_at=None,
            raw_payload={},
        )
        empty_undo = ScanUndo.model_construct(
            order_item_id=str(uuid.uuid4()),
            code="",
            workstation_id=None,
            actor="desktop",
        )
        direct_cases = (
            (
                lambda: orders_service.lookup_kiz_availability(None, ""),
                "kiz_format_invalid",
                "Code must not be empty",
            ),
            (
                lambda: orders_service.create_scan(None, empty_create),
                "kiz_format_invalid",
                "Code must not be empty",
            ),
            (
                lambda: orders_service.undo_scan(None, empty_undo),
                "kiz_format_invalid",
                "Code must not be empty",
            ),
        )
        for invoke, code, message in direct_cases:
            with self.subTest(code=code, message=message):
                with self.assertRaises(orders_service.ApiError) as raised:
                    invoke()
                self.assert_error_message(raised.exception, code=code, message=message)

        with sqlite_session() as db:
            inactive_order = Order(
                payment_type="cash",
                client="Inactive Client",
                address="Inactive Address",
                status="returned",
                raw_payload={"source": "test"},
            )
            inactive_item = OrderItem(
                order=inactive_order,
                product="Test Product",
                quantity_pieces=20,
                quantity_blocks=2,
                pieces_per_block=10,
                scanned_blocks=0,
                requires_kiz=True,
                status="not_completed",
                raw_payload={"source": "test"},
            )
            active_order = Order(
                payment_type="cash",
                client="Active Client",
                address="Active Address",
                status="not_completed",
                raw_payload={"source": "test"},
            )
            active_item = OrderItem(
                order=active_order,
                product="Test Product",
                quantity_pieces=20,
                quantity_blocks=2,
                pieces_per_block=10,
                scanned_blocks=0,
                requires_kiz=True,
                status="not_completed",
                raw_payload={"source": "test"},
            )
            db.add_all([inactive_order, inactive_item, active_order, active_item])
            db.commit()

            with self.assertRaises(orders_service.ApiError) as inactive:
                orders_service.undo_scan(
                    db,
                    ScanUndo(order_item_id=str(inactive_item.id), code="010123456780"),
                )
            self.assert_error_message(
                inactive.exception,
                code="order_closed",
                message="Cannot undo scan for inactive order",
            )

            commit_error = IntegrityError("INSERT", {}, RuntimeError("forced conflict"))
            with (
                mock.patch.object(db, "commit", side_effect=commit_error),
                self.assertRaises(orders_service.ApiError) as duplicate,
            ):
                orders_service.create_scan(
                    db,
                    ScanCreate(order_item_id=str(active_item.id), code="010123456789"),
                )
            self.assert_error_message(
                duplicate.exception,
                code="kiz_already_owned",
                message="Code already scanned",
            )


if __name__ == "__main__":
    unittest.main()
