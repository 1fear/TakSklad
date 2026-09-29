"""Повтор уже принятого скана в закрытый заказ должен получать сам скан, а не 409.

Станция 2.0.54 повторяет событие скана, пока сервер не ответит успехом или
одним из пяти текстов отказа. `create_scan` проверял «заказ неактивен» раньше,
чем искал уже записанный скан того же кода в той же позиции, поэтому код,
который сервер уже принял, при повторной отправке после завершения заказа
получал 409 `order_closed` и станция повторяла его вечно.
"""

import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import orders_service
from backend.app.kiz_blocklist import BLOCKED_KIZ_CODES
from backend.app.models import AuditLog, Base, KizMovement, Order, OrderItem, ScanCode
from backend.app.orders_service import ApiError, create_scan
from backend.app.schemas import ScanCreate

BLOCKED_CODE = next(iter(BLOCKED_KIZ_CODES))
PRODUCT = "Chapman RED OP 20"
CODE = "0104006396053947217CLOSEDREPLAY9312"
OTHER_CODE = "0104006396053947217NEVERSCANNED9312"


class ScanIdempotentAfterOrderClosedTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def seed_order(self, *, status="not_completed", quantity_blocks=1):
        with self.SessionLocal() as db:
            order = Order(
                payment_type="cash",
                client="Replay Client",
                address="Replay Address",
                status=status,
                raw_payload={"source": "test"},
            )
            item = OrderItem(
                order=order,
                product=PRODUCT,
                quantity_pieces=quantity_blocks * 10,
                quantity_blocks=quantity_blocks,
                pieces_per_block=10,
                scanned_blocks=0,
                requires_kiz=True,
                status="not_completed",
                raw_payload={"source": "test"},
            )
            db.add_all([order, item])
            db.commit()
            return order.id, item.id

    def close_order(self, order_id, status="completed"):
        with self.SessionLocal() as db:
            order = db.execute(select(Order).where(Order.id == order_id)).scalar_one()
            order.status = status
            for item in order.items:
                item.status = "completed" if status == "completed" else item.status
            db.commit()

    def counts(self):
        with self.SessionLocal() as db:
            return {
                "scans": db.execute(select(ScanCode)).scalars().all(),
                "movements": db.execute(select(KizMovement)).scalars().all(),
                "audit": db.execute(
                    select(AuditLog).where(AuditLog.action == "scan_code_created")
                ).scalars().all(),
            }

    def test_repeat_of_accepted_code_into_closed_order_returns_same_scan(self):
        order_id, item_id = self.seed_order()
        with self.SessionLocal() as db:
            first = create_scan(db, ScanCreate(order_item_id=str(item_id), code=CODE))
        self.close_order(order_id)

        before = self.counts()
        self.assertEqual(len(before["scans"]), 1)
        self.assertEqual(len(before["movements"]), 1)
        self.assertEqual(len(before["audit"]), 1)

        with self.SessionLocal() as db:
            second = create_scan(db, ScanCreate(order_item_id=str(item_id), code=CODE))

        self.assertEqual(second.id, first.id)

        after = self.counts()
        self.assertEqual(len(after["scans"]), 1)
        self.assertEqual(len(after["movements"]), 1)
        self.assertEqual(len(after["audit"]), 1)

    def test_new_code_into_closed_order_still_rejected(self):
        order_id, item_id = self.seed_order()
        with self.SessionLocal() as db:
            create_scan(db, ScanCreate(order_item_id=str(item_id), code=CODE))
        self.close_order(order_id)

        with self.SessionLocal() as db:
            with self.assertRaises(ApiError) as raised:
                create_scan(db, ScanCreate(order_item_id=str(item_id), code=OTHER_CODE))
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual((raised.exception.detail or {}).get("code"), "order_closed")

    def test_code_recorded_in_another_item_into_closed_order_still_rejected(self):
        donor_order_id, donor_item_id = self.seed_order()
        target_order_id, target_item_id = self.seed_order()
        with self.SessionLocal() as db:
            create_scan(db, ScanCreate(order_item_id=str(donor_item_id), code=CODE))
        self.close_order(target_order_id)

        with self.SessionLocal() as db:
            with self.assertRaises(ApiError) as raised:
                create_scan(db, ScanCreate(order_item_id=str(target_item_id), code=CODE))
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual((raised.exception.detail or {}).get("code"), "order_closed")

    def test_blocked_code_into_closed_order_still_rejected_as_blocked(self):
        order_id, item_id = self.seed_order()
        self.close_order(order_id)

        with self.SessionLocal() as db:
            with self.assertRaises(ApiError) as raised:
                create_scan(db, ScanCreate(order_item_id=str(item_id), code=BLOCKED_CODE))
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual((raised.exception.detail or {}).get("code"), "kiz_blocked")


if __name__ == "__main__":
    unittest.main()
