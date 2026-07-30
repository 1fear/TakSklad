import os
import threading
import unittest
import uuid
from unittest import mock

from psycopg.errors import DeadlockDetected
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from backend.app import orders_service
from backend.app.models import Order, OrderItem
from backend.app.orders_service import (
    ApiError,
    complete_order,
    create_scan,
    mark_order_returned,
    undo_scan,
)
from backend.app.schemas import ScanCreate, ScanUndo
from tests.postgres_support import TwoSessionBarrier, create_database, drop_database, run_alembic


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


def _exception_chain(exc):
    pending = [exc]
    seen = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending.extend([
            getattr(current, "orig", None),
            getattr(current, "__cause__", None),
            getattr(current, "__context__", None),
        ])


def _sqlstate(exc):
    for current in _exception_chain(exc):
        state = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if state:
            return str(state)
    return ""


def _is_deadlock(exc):
    return any(
        isinstance(current, DeadlockDetected)
        or getattr(current, "sqlstate", None) == "40P01"
        or getattr(current, "pgcode", None) == "40P01"
        for current in _exception_chain(exc)
    )


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresKizLockOrderTests(unittest.TestCase):
    database_name = "taksklad_kiz_lock_order"
    interleave_timeout = 2
    thread_timeout = 12

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

    def seed_scanned_order(self, *, code):
        with self.SessionLocal() as session:
            order = Order(
                source="synthetic_kiz_lock_order",
                external_id=f"kiz-lock-order-{uuid.uuid4()}",
                payment_type="synthetic",
                client="SYNTHETIC KIZ LOCK ORDER CLIENT",
                address="SYNTHETIC KIZ LOCK ORDER ADDRESS",
                status="not_completed",
                raw_payload={},
            )
            item = OrderItem(
                order=order,
                product="Synthetic Product",
                quantity_pieces=10,
                quantity_blocks=1,
                pieces_per_block=10,
                scanned_blocks=0,
                requires_kiz=True,
                status="not_completed",
                raw_payload={},
            )
            session.add_all([order, item])
            session.flush()
            order_id = order.id
            item_id = item.id
            create_scan(
                session,
                ScanCreate(order_item_id=str(item_id), code=code, scanned_by="postgres-test"),
            )
            return order_id, item_id

    def configure_timeouts(self, session):
        session.execute(text("SET LOCAL lock_timeout = '4s'"))
        session.execute(text("SET LOCAL statement_timeout = '8s'"))

    def run_lock_order_pair(self, *, primary_name, primary_operation, undo_operation):
        barrier = TwoSessionBarrier()
        undo_advisory_acquired = threading.Event()
        outcomes = {}
        outcomes_lock = threading.Lock()
        primary_thread_name = f"kiz-lock-order-{primary_name}"
        undo_thread_name = f"{primary_thread_name}-undo"
        original_lock = orders_service.lock_kiz_code_for_transaction

        def observed_undo_advisory_lock(db, code):
            acquired = original_lock(db, code)
            if threading.current_thread().name == undo_thread_name:
                loaded_order = next(
                    (value for value in db.identity_map.values() if isinstance(value, Order)),
                    None,
                )
                if loaded_order is None:
                    raise AssertionError("undo_scan did not load its order before the advisory lock")
                # undo_scan assigns not_completed to the already not_completed synthetic order.
                # Keep the documented orders UPDATE in the lock graph instead of letting
                # SQLAlchemy optimize the same-value assignment away.
                flag_modified(loaded_order, "status")
                undo_advisory_acquired.set()
            return acquired

        def record_success(name):
            with outcomes_lock:
                outcomes[name] = ("completed", None)

        def record_conflict(name, exc):
            with outcomes_lock:
                outcomes[name] = ("application_conflict", exc.status_code)

        def run_primary():
            try:
                with self.SessionLocal() as session:
                    self.configure_timeouts(session)
                    primary_operation(session, barrier, undo_advisory_acquired)
                    record_success(primary_name)
            except ApiError as exc:
                if exc.status_code == 409:
                    record_conflict(primary_name, exc)
                else:
                    barrier.capture_error((primary_name, exc))
            except Exception as exc:  # pragma: no cover - asserted below
                barrier.capture_error((primary_name, exc))
            finally:
                barrier.mark_completed()

        def run_undo():
            try:
                with self.SessionLocal() as session:
                    self.configure_timeouts(session)
                    barrier.wait_for_worker()
                    undo_operation(session)
                    record_success("undo_scan")
            except ApiError as exc:
                if exc.status_code == 409:
                    record_conflict("undo_scan", exc)
                else:
                    barrier.capture_error(("undo_scan", exc))
            except Exception as exc:  # pragma: no cover - asserted below
                barrier.capture_error(("undo_scan", exc))
            finally:
                barrier.mark_completed()

        primary_thread = threading.Thread(
            target=run_primary,
            name=primary_thread_name,
            daemon=True,
        )
        undo_thread = threading.Thread(
            target=run_undo,
            name=undo_thread_name,
            daemon=True,
        )

        with mock.patch(
            "backend.app.orders_service.lock_kiz_code_for_transaction",
            side_effect=observed_undo_advisory_lock,
        ):
            primary_thread.start()
            undo_thread.start()
            barrier.completed.wait(self.thread_timeout)
            primary_thread.join(timeout=self.thread_timeout)
            undo_thread.join(timeout=self.thread_timeout)

        self.assertFalse(primary_thread.is_alive(), f"{primary_name} did not finish")
        self.assertFalse(undo_thread.is_alive(), "undo_scan did not finish")

        errors = list(barrier.errors.queue)
        deadlocks = [
            (name, type(exc).__name__, _sqlstate(exc))
            for name, exc in errors
            if _is_deadlock(exc)
        ]
        self.assertEqual(
            deadlocks,
            [],
            "PostgreSQL deadlock must not be an allowed concurrency outcome",
        )
        self.assertEqual(
            [(name, type(exc).__name__, _sqlstate(exc)) for name, exc in errors],
            [],
            "operations must finish or return a deterministic application conflict",
        )
        self.assertEqual(set(outcomes), {primary_name, "undo_scan"})
        self.assertTrue(
            all(result in {"completed", "application_conflict"} for result, _status in outcomes.values())
        )
        self.assertIn("completed", [result for result, _status in outcomes.values()])
        self.assertLessEqual(
            sum(result == "application_conflict" for result, _status in outcomes.values()),
            1,
        )
        self.assertTrue(
            all(status in {None, 409} for _result, status in outcomes.values())
        )

    # Expected until the inversion in §6.18 is removed by §23 step 4.
    @unittest.expectedFailure
    def test_complete_order_and_undo_scan_do_not_deadlock(self):
        code = "SYNTHETIC-COMPLETE-UNDO-LOCK-ORDER"
        order_id, item_id = self.seed_scanned_order(code=code)

        def run_complete(session, barrier, undo_advisory_acquired):
            session.execute(
                select(Order.id).where(Order.id == order_id).with_for_update()
            ).scalar_one()
            barrier.worker_started()
            undo_advisory_acquired.wait(self.interleave_timeout)
            return complete_order(session, str(order_id))

        def run_undo(session):
            return undo_scan(
                session,
                ScanUndo(order_item_id=str(item_id), code=code, actor="postgres-test"),
            )

        self.run_lock_order_pair(
            primary_name="complete_order",
            primary_operation=run_complete,
            undo_operation=run_undo,
        )

    # Expected until the inversion in §6.18 is removed by §23 step 4.
    @unittest.expectedFailure
    def test_mark_order_returned_and_undo_scan_do_not_deadlock(self):
        code = "SYNTHETIC-RETURN-UNDO-LOCK-ORDER"
        order_id, item_id = self.seed_scanned_order(code=code)
        confirmed_items = [{
            "item_id": str(item_id),
            "product": "Synthetic Product",
            "quantity_blocks": 1,
            "quantity_pieces": 10,
        }]

        def run_return(session, barrier, undo_advisory_acquired):
            order = session.execute(
                select(Order).where(Order.id == order_id).with_for_update()
            ).scalar_one()
            order.status = "completed"
            session.flush()
            barrier.worker_started()
            undo_advisory_acquired.wait(self.interleave_timeout)
            return mark_order_returned(
                session,
                str(order_id),
                return_reference="SYNTHETIC-LOCK-ORDER-RETURN",
                returned_by="postgres-test",
                confirmed_items=confirmed_items,
            )

        def run_undo(session):
            return undo_scan(
                session,
                ScanUndo(order_item_id=str(item_id), code=code, actor="postgres-test"),
            )

        self.run_lock_order_pair(
            primary_name="mark_order_returned",
            primary_operation=run_return,
            undo_operation=run_undo,
        )


if __name__ == "__main__":
    unittest.main()
