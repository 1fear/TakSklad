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
                item.status = "completed"
            db.commit()

    def snapshot(self, item_id):
        """Всё, что повтор скана не имеет права менять, одним словарём для сравнения до и после."""
        with self.SessionLocal() as db:
            item = db.execute(select(OrderItem).where(OrderItem.id == item_id)).scalar_one()
            return {
                "scans": sorted(str(row) for row in db.execute(select(ScanCode.id)).scalars()),
                "movements": sorted(str(row) for row in db.execute(select(KizMovement.id)).scalars()),
                "audit": sorted(str(row) for row in db.execute(select(AuditLog.id)).scalars()),
                "scanned_blocks": item.scanned_blocks,
                "item_status": item.status,
            }

    def assert_rejected(self, item_id, code, *, error_code, message=None):
        with self.SessionLocal() as db:
            with self.assertRaises(ApiError) as raised:
                create_scan(db, ScanCreate(order_item_id=str(item_id), code=code))
        self.assertEqual(raised.exception.status_code, 409)
        detail = raised.exception.detail or {}
        self.assertEqual(detail.get("code"), error_code)
        if message is not None:
            self.assertEqual(detail.get("message"), message)

    def assert_repeat_returns_same_scan(self, status):
        order_id, item_id = self.seed_order()
        with self.SessionLocal() as db:
            first = create_scan(db, ScanCreate(order_item_id=str(item_id), code=CODE))
        self.close_order(order_id, status)

        before = self.snapshot(item_id)
        self.assertEqual(len(before["scans"]), 1)
        self.assertEqual(len(before["movements"]), 1)
        self.assertGreaterEqual(len(before["audit"]), 1)

        with self.SessionLocal() as db:
            second = create_scan(db, ScanCreate(order_item_id=str(item_id), code=CODE))

        self.assertEqual(second.id, first.id)
        self.assertEqual(self.snapshot(item_id), before)

    def test_repeat_of_accepted_code_into_closed_order_returns_same_scan(self):
        self.assert_repeat_returns_same_scan("completed")

    def test_repeat_of_accepted_code_into_returned_order_returns_same_scan(self):
        self.assert_repeat_returns_same_scan("returned")

    def test_new_code_into_closed_order_still_rejected(self):
        order_id, item_id = self.seed_order()
        with self.SessionLocal() as db:
            create_scan(db, ScanCreate(order_item_id=str(item_id), code=CODE))
        self.close_order(order_id)

        self.assert_rejected(
            item_id, OTHER_CODE, error_code="order_closed", message="Cannot scan inactive order"
        )

    def test_code_recorded_in_another_item_into_closed_order_still_rejected(self):
        donor_order_id, donor_item_id = self.seed_order()
        target_order_id, target_item_id = self.seed_order()
        with self.SessionLocal() as db:
            create_scan(db, ScanCreate(order_item_id=str(donor_item_id), code=CODE))
        self.close_order(target_order_id)

        self.assert_rejected(
            target_item_id, CODE, error_code="order_closed", message="Cannot scan inactive order"
        )

    def test_blocked_code_into_closed_order_still_rejected_as_blocked(self):
        order_id, item_id = self.seed_order()
        # Скан этого кода уже лежит в той же позиции, как будто код попал в блок-лист позже.
        # Без этой строки тест зелёный при любом порядке проверок: искать в позиции было бы нечего.
        with self.SessionLocal() as db:
            db.add(ScanCode(order_item_id=item_id, code=BLOCKED_CODE, raw_payload={}))
            db.commit()
        self.close_order(order_id)

        self.assert_rejected(item_id, BLOCKED_CODE, error_code="kiz_blocked")


if __name__ == "__main__":
    unittest.main()
