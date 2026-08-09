import os
import threading
import time
import unittest
import uuid
from unittest import mock
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from backend.app import order_actions_service, orders_service
from backend.app.models import KizMovement, Order, OrderItem, ScanCode
from backend.app.order_actions_service import cancel_order, delete_active_order, reset_order_for_rescan
from backend.app.orders_service import ApiError, create_scan
from backend.app.schemas import AdminOrderActionRequest, ScanCreate
from tests.postgres_support import create_database, drop_database, run_alembic


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresOrderKizRaceTests(unittest.TestCase):
    database_name = "taksklad_order_kiz_races"
    thread_timeout = 15

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
            connection.execute(text("""
                TRUNCATE pending_events,audit_log,kiz_movements,kiz_codes,scan_codes,
                         order_items,orders,import_files,imports,incidents,client_points
                RESTART IDENTITY CASCADE
            """))

    def seed_order(self, *, quantity_blocks=2, status="not_completed"):
        with self.SessionLocal() as session:
            order = Order(
                source="synthetic_order_kiz_race",
                external_id=f"order-kiz-race-{uuid.uuid4()}",
                payment_type="synthetic",
                client="SYNTHETIC ORDER KIZ RACE CLIENT",
                address="SYNTHETIC ORDER KIZ RACE ADDRESS",
                status=status,
                raw_payload={},
            )
            item = OrderItem(
                order=order,
                product="Synthetic Product",
                quantity_pieces=quantity_blocks * 10,
                quantity_blocks=quantity_blocks,
                pieces_per_block=10,
                scanned_blocks=0,
                requires_kiz=True,
                status=status,
                raw_payload={},
            )
            session.add_all([order, item])
            session.commit()
            return order.id, item.id

    def configure_timeouts(self, session):
        session.execute(text("SET LOCAL lock_timeout = '5s'"))
        session.execute(text("SET LOCAL statement_timeout = '10s'"))

    def run_worker(self, name, target, outcomes, errors):
        try:
            with self.SessionLocal() as session:
                self.configure_timeouts(session)
                outcomes[name] = ("completed", target(session))
        except ApiError as exc:
            outcomes[name] = ("api_error", exc.status_code, exc.detail)
        except Exception as exc:
            errors.append((name, exc))

    def join_threads(self, *threads):
        for thread in threads:
            thread.join(timeout=self.thread_timeout)
            self.assertFalse(thread.is_alive(), f"thread did not finish: {thread.name}")

    def test_delete_and_scan_finish_without_orphaned_outbound_movement(self):
        order_id, item_id = self.seed_order()
        delete_checked = threading.Event()
        release_delete = threading.Event()
        outcomes = {}
        errors = []
        original_ensure = order_actions_service.ensure_order_has_no_scans

        def delayed_ensure(order):
            original_ensure(order)
            if threading.current_thread().name == "delete-before-scan":
                delete_checked.set()
                if not release_delete.wait(timeout=5):
                    raise AssertionError("delete race release timeout")

        delete_payload = AdminOrderActionRequest(reason="delete race", actor="postgres-test")
        scan_payload = ScanCreate(
            order_item_id=str(item_id),
            code="0104006396053947217DELETEORPHAN93S1",
            scanned_by="postgres-test",
        )

        with mock.patch(
            "backend.app.order_actions_service.ensure_order_has_no_scans",
            side_effect=delayed_ensure,
        ):
            delete_thread = threading.Thread(
                target=self.run_worker,
                args=(
                    "delete",
                    lambda session: delete_active_order(session, str(order_id), delete_payload),
                    outcomes,
                    errors,
                ),
                name="delete-before-scan",
                daemon=True,
            )
            scan_thread = threading.Thread(
                target=self.run_worker,
                args=("scan", lambda session: create_scan(session, scan_payload), outcomes, errors),
                name="scan-after-delete-lock",
                daemon=True,
            )
            delete_thread.start()
            self.assertTrue(delete_checked.wait(5))
            scan_thread.start()
            time.sleep(0.3)
            release_delete.set()
            self.join_threads(delete_thread, scan_thread)

        self.assertEqual(errors, [])
        self.assertEqual(outcomes.get("delete", (None,))[0], "completed")
        self.assertEqual(outcomes.get("scan", (None, None))[0:2], ("api_error", 404))
        with self.SessionLocal() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Order)), 0)
            self.assertEqual(session.scalar(select(func.count()).select_from(ScanCode)), 0)
            self.assertEqual(session.scalar(select(func.count()).select_from(KizMovement)), 0)

    def test_delete_cannot_remove_scan_created_before_its_movement(self):
        order_id, item_id = self.seed_order()
        scan_row_created = threading.Event()
        release_scan = threading.Event()
        outcomes = {}
        errors = []
        original_record = orders_service.record_kiz_movement

        def delayed_record(db, **kwargs):
            if threading.current_thread().name == "scan-before-movement":
                scan_row_created.set()
                if not release_scan.wait(timeout=5):
                    raise AssertionError("scan race release timeout")
            return original_record(db, **kwargs)

        scan_payload = ScanCreate(
            order_item_id=str(item_id),
            code="0104006396053947217DELETENOMOVE93S1",
            scanned_by="postgres-test",
        )
        delete_payload = AdminOrderActionRequest(reason="delete scan window", actor="postgres-test")

        with mock.patch("backend.app.orders_service.record_kiz_movement", side_effect=delayed_record):
            scan_thread = threading.Thread(
                target=self.run_worker,
                args=("scan", lambda session: create_scan(session, scan_payload), outcomes, errors),
                name="scan-before-movement",
                daemon=True,
            )
            delete_thread = threading.Thread(
                target=self.run_worker,
                args=(
                    "delete",
                    lambda session: delete_active_order(session, str(order_id), delete_payload),
                    outcomes,
                    errors,
                ),
                name="delete-during-scan",
                daemon=True,
            )
            scan_thread.start()
            self.assertTrue(scan_row_created.wait(5))
            delete_thread.start()
            time.sleep(0.3)
            release_scan.set()
            self.join_threads(scan_thread, delete_thread)

        self.assertEqual(errors, [])
        self.assertEqual(outcomes.get("scan", (None,))[0], "completed")
        self.assertEqual(outcomes.get("delete", (None, None))[0:2], ("api_error", 409))
        with self.SessionLocal() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Order)), 1)
            self.assertEqual(session.scalar(select(func.count()).select_from(ScanCode)), 1)
            movements = session.execute(select(KizMovement)).scalars().all()
            self.assertEqual([movement.movement_type for movement in movements], ["outbound"])

    def test_cancel_serializes_with_scan_and_leaves_no_scan_in_closed_order(self):
        order_id, item_id = self.seed_order()
        cancel_checked = threading.Event()
        release_cancel = threading.Event()
        outcomes = {}
        errors = []
        original_ensure = order_actions_service.ensure_order_has_no_scans

        def delayed_ensure(order):
            original_ensure(order)
            if threading.current_thread().name == "cancel-before-scan":
                cancel_checked.set()
                if not release_cancel.wait(timeout=5):
                    raise AssertionError("cancel race release timeout")

        cancel_payload = AdminOrderActionRequest(reason="cancel race", actor="postgres-test")
        scan_payload = ScanCreate(
            order_item_id=str(item_id),
            code="0104006396053947217CANCELRACE93SYN1",
            scanned_by="postgres-test",
        )

        with mock.patch(
            "backend.app.order_actions_service.ensure_order_has_no_scans",
            side_effect=delayed_ensure,
        ):
            cancel_thread = threading.Thread(
                target=self.run_worker,
                args=("cancel", lambda session: cancel_order(session, str(order_id), cancel_payload), outcomes, errors),
                name="cancel-before-scan",
                daemon=True,
            )
            scan_thread = threading.Thread(
                target=self.run_worker,
                args=("scan", lambda session: create_scan(session, scan_payload), outcomes, errors),
                name="scan-after-cancel-lock",
                daemon=True,
            )
            cancel_thread.start()
            self.assertTrue(cancel_checked.wait(5))
            scan_thread.start()
            time.sleep(0.3)
            release_cancel.set()
            self.join_threads(cancel_thread, scan_thread)

        self.assertEqual(errors, [])
        self.assertEqual(outcomes.get("cancel", (None,))[0], "completed")
        self.assertEqual(outcomes.get("scan", (None, None))[0:2], ("api_error", 409))
        with self.SessionLocal() as session:
            order = session.get(Order, order_id)
            self.assertEqual(order.status, "cancelled")
            self.assertEqual(session.scalar(select(func.count()).select_from(ScanCode)), 0)
            self.assertEqual(session.scalar(select(func.count()).select_from(KizMovement)), 0)

    def test_stale_scan_captured_before_reset_cannot_recomplete_item(self):
        order_id, item_id = self.seed_order(quantity_blocks=1)
        code = "0104006396053947217RESETFENCE93SYN1"
        captured_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        with self.SessionLocal() as session:
            create_scan(session, ScanCreate(
                order_item_id=str(item_id),
                code=code,
                scanned_by="postgres-test",
                scanned_at=captured_at,
            ))
        with self.SessionLocal() as session:
            reset_order_for_rescan(
                session,
                str(order_id),
                AdminOrderActionRequest(reason="stale queue fence", actor="postgres-test"),
            )

        with self.SessionLocal() as session:
            with self.assertRaises(ApiError) as raised:
                create_scan(session, ScanCreate(
                    order_item_id=str(item_id),
                    code=code,
                    scanned_by="postgres-test",
                    scanned_at=captured_at,
                ))
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual((raised.exception.detail or {}).get("code"), "order_closed")
            session.rollback()

        with self.SessionLocal() as session:
            item = session.get(OrderItem, item_id)
            scans = session.execute(select(ScanCode).where(ScanCode.order_item_id == item_id)).scalars().all()
            movements = session.execute(
                select(KizMovement).order_by(KizMovement.occurred_at.asc(), KizMovement.id.asc())
            ).scalars().all()
            self.assertEqual(item.scanned_blocks, 0)
            self.assertEqual(scans, [])
            self.assertEqual([movement.movement_type for movement in movements], ["outbound", "reset"])


if __name__ == "__main__":
    unittest.main()
