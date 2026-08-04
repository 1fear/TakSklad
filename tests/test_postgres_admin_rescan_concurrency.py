import os
import threading
import unittest
import uuid
from unittest import mock

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from backend.app import order_actions_service
from backend.app.models import KizMovement, Order, OrderItem, ScanCode
from backend.app.order_actions_service import reset_order_for_rescan
from backend.app.orders_service import ApiError, create_scan, undo_scan
from backend.app.schemas import AdminOrderActionRequest, ScanCreate, ScanUndo
from tests.postgres_support import create_database, drop_database, run_alembic


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresAdminRescanConcurrencyTests(unittest.TestCase):
    database_name = "taksklad_admin_rescan_concurrency"

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

    def seed_scanned_item(self, *, quantity_blocks, code):
        with self.SessionLocal() as session:
            order = Order(
                source="synthetic_admin_rescan",
                external_id=f"admin-rescan-{uuid.uuid4()}",
                payment_type="synthetic",
                client="SYNTHETIC ADMIN RESCAN CLIENT",
                address="SYNTHETIC ADMIN RESCAN ADDRESS",
                status="not_completed",
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
                status="not_completed",
                raw_payload={},
            )
            session.add_all([order, item])
            session.flush()
            scan = create_scan(session, ScanCreate(order_item_id=str(item.id), code=code, scanned_by="seed"))
            return order.id, item.id, uuid.UUID(scan.id)

    def _blocking_reset_helper(self, order_id, locked, release):
        original = order_actions_service.lock_reset_order_state

        def wrapper(db, locked_order_id):
            items, scans_by_item_id = original(db, locked_order_id)
            if locked_order_id == order_id:
                locked.set()
                if not release.wait(timeout=5):
                    raise AssertionError("timed out while holding reset locks")
            return items, scans_by_item_id

        return wrapper

    def test_reset_blocks_concurrent_scan_until_commit_and_keeps_state_consistent(self):
        order_id, item_id, _scan_id = self.seed_scanned_item(
            quantity_blocks=2,
            code="0104006396053947217RESETOLD93SYNTH1",
        )
        locked = threading.Event()
        release = threading.Event()
        scan_finished = threading.Event()
        errors = []
        outcomes = {}

        def run_reset():
            try:
                with self.SessionLocal() as session:
                    outcomes["reset"] = reset_order_for_rescan(
                        session,
                        str(order_id),
                        AdminOrderActionRequest(reason="concurrency reset", actor="postgres-test"),
                    )
            except Exception as exc:  # pragma: no cover - assertion handled below
                errors.append(exc)

        def run_scan():
            try:
                with self.SessionLocal() as session:
                    outcomes["scan"] = create_scan(
                        session,
                        ScanCreate(
                            order_item_id=str(item_id),
                            code="0104006396053947217RESETNEW93SYNTH1",
                            scanned_by="postgres-test",
                        ),
                    )
            except Exception as exc:  # pragma: no cover - assertion handled below
                errors.append(exc)
            finally:
                scan_finished.set()

        with mock.patch(
            "backend.app.order_actions_service.lock_reset_order_state",
            side_effect=self._blocking_reset_helper(order_id, locked, release),
        ):
            reset_thread = threading.Thread(target=run_reset, daemon=True)
            reset_thread.start()
            self.assertTrue(locked.wait(5))

            scan_thread = threading.Thread(target=run_scan, daemon=True)
            scan_thread.start()
            self.assertFalse(scan_finished.wait(0.3))

            release.set()
            reset_thread.join(timeout=15)
            scan_thread.join(timeout=15)

        self.assertEqual(errors, [])
        self.assertIn("reset", outcomes)
        self.assertEqual(outcomes["scan"].code, "0104006396053947217RESETNEW93SYNTH1")

        with self.SessionLocal() as session:
            item = session.get(OrderItem, item_id)
            order = session.get(Order, order_id)
            scans = session.execute(
                select(ScanCode).where(ScanCode.order_item_id == item_id).order_by(ScanCode.id.asc())
            ).scalars().all()
            movements = session.execute(
                select(KizMovement).order_by(KizMovement.occurred_at.asc(), KizMovement.id.asc())
            ).scalars().all()

        self.assertEqual(order.status, "not_completed")
        self.assertEqual(item.scanned_blocks, 1)
        self.assertEqual(item.status, "not_completed")
        self.assertEqual([scan.code for scan in scans], ["0104006396053947217RESETNEW93SYNTH1"])
        self.assertEqual([movement.movement_type for movement in movements], ["outbound", "reset", "outbound"])
        self.assertEqual(sum(1 for movement in movements if movement.movement_type == "reset"), 1)

    def test_reset_locks_current_scan_rows_and_undo_fails_cleanly_after_commit(self):
        order_id, item_id, scan_id = self.seed_scanned_item(
            quantity_blocks=1,
            code="0104006396053947217RESETUNDO93SYNT1",
        )
        locked = threading.Event()
        release = threading.Event()
        undo_finished = threading.Event()
        errors = []
        outcomes = {}

        def run_reset():
            try:
                with self.SessionLocal() as session:
                    outcomes["reset"] = reset_order_for_rescan(
                        session,
                        str(order_id),
                        AdminOrderActionRequest(reason="undo race reset", actor="postgres-test"),
                    )
            except Exception as exc:  # pragma: no cover - assertion handled below
                errors.append(exc)

        def run_undo():
            try:
                with self.SessionLocal() as session:
                    undo_scan(
                        session,
                        ScanUndo(
                            order_item_id=str(item_id),
                            code="0104006396053947217RESETUNDO93SYNT1",
                            actor="postgres-test",
                        ),
                    )
                    outcomes["undo"] = "unexpected-success"
            except ApiError as exc:
                outcomes["undo_error"] = (exc.status_code, exc.detail)
            except Exception as exc:  # pragma: no cover - assertion handled below
                errors.append(exc)
            finally:
                undo_finished.set()

        with mock.patch(
            "backend.app.order_actions_service.lock_reset_order_state",
            side_effect=self._blocking_reset_helper(order_id, locked, release),
        ):
            reset_thread = threading.Thread(target=run_reset, daemon=True)
            reset_thread.start()
            self.assertTrue(locked.wait(5))

            with self.SessionLocal() as observer, observer.begin():
                skipped = observer.execute(
                    select(ScanCode.id)
                    .where(ScanCode.id == scan_id)
                    .with_for_update(skip_locked=True)
                ).scalars().all()
            self.assertEqual(skipped, [])

            undo_thread = threading.Thread(target=run_undo, daemon=True)
            undo_thread.start()
            self.assertFalse(undo_finished.wait(0.3))

            release.set()
            reset_thread.join(timeout=15)
            undo_thread.join(timeout=15)

        self.assertEqual(errors, [])
        self.assertIn("reset", outcomes)
        self.assertEqual(outcomes.get("undo_error", (None,))[0], 404)
        self.assertNotIn("unexpected-success", outcomes.values())

        with self.SessionLocal() as session:
            item = session.get(OrderItem, item_id)
            order = session.get(Order, order_id)
            scans = session.execute(select(ScanCode).where(ScanCode.order_item_id == item_id)).scalars().all()
            movements = session.execute(
                select(KizMovement).order_by(KizMovement.occurred_at.asc(), KizMovement.id.asc())
            ).scalars().all()

        self.assertEqual(order.status, "not_completed")
        self.assertEqual(item.scanned_blocks, 0)
        self.assertEqual(item.status, "not_completed")
        self.assertEqual(scans, [])
        self.assertEqual([movement.movement_type for movement in movements], ["outbound", "reset"])
        self.assertEqual(sum(1 for movement in movements if movement.movement_type == "undo"), 0)


if __name__ == "__main__":
    unittest.main()
