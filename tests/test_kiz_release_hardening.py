"""Аудит 06.08.2026 по release_kiz: три дефекта, каждый закреплён тестом.

1. Освобождение короба объяснялось одним блоком вместо пятидесяти, и файл-источник
   донора продолжал отдавать 409.
2. Счётчик разрыва инкрементился даже когда undo_scan падал, то есть позиция
   объясняла разрыв, которого не было.
3. Донор выбирался по максимальному scanned_at, а метка приходит от клиента:
   позднее offline-событие со старой меткой уводило release на исторический скан,
   и один КИЗ оказывался сразу в двух заказах.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import orders_service
from backend.app.kiz_movements_service import MOVEMENT_OUTBOUND, latest_kiz_movement
from backend.app.kiz_reports_service import item_kiz_is_completed
from backend.app.models import AuditLog, Base, KizCode, KizMovement, Order, OrderItem, ScanCode
from backend.app.schemas import KizRelease, ScanCreate

BOX_CODE = "010400639605401221UZ1112022525522513824013040046110ZIG1218229310000"
BOX_PRODUCT = "Chapman Gold SSL 100`20"
UNIT_CODE = "0104006396053947217RELEASE93HARDEN1"
UNIT_PRODUCT = "Chapman RED OP 20"


class KizReleaseHardeningTests(unittest.TestCase):
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

    def seed_order(self, *, product, quantity_blocks, status="not_completed"):
        with self.SessionLocal() as db:
            order = Order(
                payment_type="cash",
                client="Hardening Client",
                address="Hardening Address",
                status=status,
                raw_payload={"source": "test", "skladbot_request_number": "WH-R-000001"},
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
            db.commit()
            return order.id, item.id

    def scan(self, item_id, code):
        with self.SessionLocal() as db:
            return orders_service.create_scan(db, ScanCreate(order_item_id=str(item_id), code=code))

    def close_order(self, order_id):
        with self.SessionLocal() as db:
            order = db.execute(select(Order).where(Order.id == order_id)).scalar_one()
            order.status = "completed"
            for item in order.items:
                item.status = "completed"
            db.commit()

    def release(self, code, reason="returned_to_shelf"):
        with self.SessionLocal() as db:
            return orders_service.release_kiz(db, KizRelease(code=code, reason=reason))

    def item_snapshot(self, item_id):
        with self.SessionLocal() as db:
            item = db.execute(select(OrderItem).where(OrderItem.id == item_id)).scalar_one()
            return {
                "status": item.status,
                "scanned_blocks": item.scanned_blocks,
                "quantity_blocks": item.quantity_blocks,
                "released": (item.raw_payload or {}).get("kiz_released_blocks"),
                "completed": item_kiz_is_completed(item),
            }

    def test_released_box_is_counted_in_blocks_not_in_calls(self):
        order_id, item_id = self.seed_order(product=BOX_PRODUCT, quantity_blocks=50)
        self.scan(item_id, BOX_CODE)
        self.close_order(order_id)
        self.assertEqual(self.item_snapshot(item_id)["scanned_blocks"], 50)

        result = self.release(BOX_CODE)

        self.assertTrue(result.released)
        after = self.item_snapshot(item_id)
        # Короб уносит 50 блоков, счётчик обязан объяснить ровно столько же,
        # иначе позиция остаётся неполной и файл-источник отдаёт 409
        self.assertEqual(after["released"], 50)
        self.assertEqual(after["scanned_blocks"], 0)
        self.assertEqual(after["status"], "completed")
        self.assertTrue(after["completed"])

    def test_released_unit_is_counted_as_one_block(self):
        order_id, item_id = self.seed_order(product=UNIT_PRODUCT, quantity_blocks=1)
        self.scan(item_id, UNIT_CODE)
        self.close_order(order_id)

        self.release(UNIT_CODE)

        after = self.item_snapshot(item_id)
        self.assertEqual(after["released"], 1)
        self.assertTrue(after["completed"])

    def test_failed_undo_leaves_no_counter_and_no_audit(self):
        order_id, item_id = self.seed_order(product=UNIT_PRODUCT, quantity_blocks=1)
        self.scan(item_id, UNIT_CODE)
        self.close_order(order_id)

        with mock.patch.object(
            orders_service, "undo_scan", side_effect=orders_service.ApiError(409, {"code": "kiz_already_owned"})
        ):
            with self.assertRaises(orders_service.ApiError):
                self.release(UNIT_CODE)

        after = self.item_snapshot(item_id)
        # Разрыва не было, значит и объяснять нечего
        self.assertIsNone(after["released"])
        # Заказ должен вернуться в исходный закрытый статус, а не остаться открытым
        with self.SessionLocal() as db:
            order = db.execute(select(Order).where(Order.id == order_id)).scalar_one()
            self.assertEqual(order.status, "completed")
            released_audit = db.execute(
                select(AuditLog).where(AuditLog.action == "kiz_released")
            ).scalars().all()
            self.assertEqual(released_audit, [])

    def test_release_follows_the_movement_not_the_latest_scanned_at(self):
        """Поздний offline-скан со старой меткой не должен уводить release на чужой заказ."""
        first_order_id, first_item_id = self.seed_order(product=UNIT_PRODUCT, quantity_blocks=1)
        self.scan(first_item_id, UNIT_CODE)
        self.close_order(first_order_id)

        # Первый заказ вернули: КИЗ снова свободен, исторический скан остаётся.
        # Историю сдвигаем в прошлое, чтобы следующий скан стал последним движением
        now = datetime.now(timezone.utc)
        with self.SessionLocal() as db:
            kiz = db.execute(select(KizCode).where(KizCode.code == UNIT_CODE)).scalar_one()
            outbound = db.execute(
                select(KizMovement)
                .where(KizMovement.kiz_id == kiz.id)
                .where(KizMovement.movement_type == MOVEMENT_OUTBOUND)
            ).scalar_one()
            outbound.occurred_at = now - timedelta(minutes=10)
            db.add(KizMovement(
                id=uuid.uuid4(),
                kiz_id=kiz.id,
                movement_type="return",
                order_id=first_order_id,
                order_item_id=first_item_id,
                scan_code_id=outbound.scan_code_id,
                occurred_at=now - timedelta(minutes=5),
                raw_payload={},
            ))
            db.commit()

        # Второй заказ забирает тот же КИЗ, но клиент прислал старую метку времени
        _second_order_id, second_item_id = self.seed_order(product=UNIT_PRODUCT, quantity_blocks=1)
        with self.SessionLocal() as db:
            orders_service.create_scan(db, ScanCreate(
                order_item_id=str(second_item_id),
                code=UNIT_CODE,
                scanned_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
            ))

        result = self.release(UNIT_CODE)

        self.assertTrue(result.released)
        # Освобождён должен быть текущий владелец, а не исторический скан
        self.assertEqual(result.donor_order_item_id, str(second_item_id))
        with self.SessionLocal() as db:
            remaining = db.execute(select(ScanCode).where(ScanCode.code == UNIT_CODE)).scalars().all()
            self.assertEqual([str(scan.order_item_id) for scan in remaining], [str(first_item_id)])
            movement = latest_kiz_movement(db, UNIT_CODE)
            self.assertEqual(movement.movement_type, "undo")

    def test_counter_survives_junk_in_payload(self):
        order_id, item_id = self.seed_order(product=UNIT_PRODUCT, quantity_blocks=1)
        self.scan(item_id, UNIT_CODE)
        self.close_order(order_id)
        with self.SessionLocal() as db:
            item = db.execute(select(OrderItem).where(OrderItem.id == item_id)).scalar_one()
            item.raw_payload = dict(item.raw_payload or {}, kiz_released_blocks="мусор")
            db.commit()

        self.release(UNIT_CODE)

        self.assertEqual(self.item_snapshot(item_id)["released"], 1)


class ReleasedBlocksParsingTests(unittest.TestCase):
    def test_parse_released_blocks_is_total(self):
        self.assertEqual(orders_service.parse_released_blocks(None), 0)
        self.assertEqual(orders_service.parse_released_blocks(""), 0)
        self.assertEqual(orders_service.parse_released_blocks("7"), 7)
        self.assertEqual(orders_service.parse_released_blocks(-3), 0)
        self.assertEqual(orders_service.parse_released_blocks("мусор"), 0)


if __name__ == "__main__":
    unittest.main()
