import unittest
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.imports_service import source_import_lookup_key
from backend.app.models import AuditLog, Base, KizCode, KizMovement, Order, OrderItem, ScanCode
from tools import merge_duplicate_order_positions as repair


class MergeDuplicateOrderPositionsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._original_session = repair.SessionLocal
        repair.SessionLocal = self.SessionLocal

    def tearDown(self):
        repair.SessionLocal = self._original_session
        self.engine.dispose()

    def make_order(self, status="not_completed", order_date=date(2026, 8, 3)):
        order = Order(
            id=uuid.uuid4(),
            source="telegram",
            order_date=order_date,
            payment_type="Перечисление",
            client="MERGE CLIENT",
            address="MERGE ADDRESS",
            status=status,
            raw_payload={},
        )
        return order

    def make_item(self, order, *, blocks, scanned, source_row, status="not_completed", product="Chapman RED OP 20"):
        return OrderItem(
            id=uuid.uuid4(),
            order_id=order.id,
            product=product,
            quantity_pieces=blocks * 10,
            quantity_blocks=blocks,
            pieces_per_block=10,
            scanned_blocks=scanned,
            requires_kiz=True,
            status=status,
            source_import_id=f"import-row-{source_row}",
            source_import_key=source_import_lookup_key(f"import-row-{source_row}"),
            raw_payload={
                "source_row": str(source_row),
                "source_import_id": f"import-row-{source_row}",
                "source_order_id": f"order-{source_row}",
                "line_total": blocks * 240000,
            },
        )

    def test_plan_does_not_change_anything(self):
        with self.SessionLocal() as db:
            order = self.make_order()
            db.add(order)
            db.add(self.make_item(order, blocks=1, scanned=0, source_row=37))
            db.add(self.make_item(order, blocks=1, scanned=1, source_row=54, status="completed"))
            db.commit()

        result = repair.run(apply_changes=False, only_open=False)

        self.assertEqual(result["mode"], "plan")
        self.assertEqual(result["groups"], 1)
        self.assertEqual(result["entries"][0]["blocks_after"], 2)
        with self.SessionLocal() as db:
            self.assertEqual(len(db.execute(select(OrderItem)).scalars().all()), 2)
            self.assertEqual(len(db.execute(select(AuditLog)).scalars().all()), 0)

    def test_apply_merges_open_order_and_keeps_scans(self):
        with self.SessionLocal() as db:
            order = self.make_order()
            db.add(order)
            unscanned = self.make_item(order, blocks=1, scanned=0, source_row=37)
            scanned = self.make_item(order, blocks=1, scanned=1, source_row=54, status="completed")
            db.add_all([unscanned, scanned])
            kiz = KizCode(id=uuid.uuid4(), code="0104006396053947XXXXXXXXXXXXXXXXXXX")
            db.add(kiz)
            db.add(ScanCode(
                id=uuid.uuid4(),
                order_item_id=scanned.id,
                code="0104006396053947XXXXXXXXXXXXXXXXXXX",
                scanned_at=datetime.now(timezone.utc),
                raw_payload={},
            ))
            db.add(KizMovement(
                id=uuid.uuid4(),
                kiz_id=kiz.id,
                movement_type="scan",
                order_id=order.id,
                order_item_id=scanned.id,
                raw_payload={},
            ))
            db.commit()
            scanned_id = scanned.id

        result = repair.run(apply_changes=True, only_open=False)

        self.assertEqual(result["mode"], "apply")
        self.assertEqual(result["groups"], 1)
        with self.SessionLocal() as db:
            items = db.execute(select(OrderItem)).scalars().all()
            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(item.id, scanned_id, "хранителем обязана стать отсканированная позиция")
            self.assertEqual(item.quantity_blocks, 2)
            self.assertEqual(item.quantity_pieces, 20)
            self.assertEqual(item.scanned_blocks, 1)
            self.assertEqual(item.status, "not_completed")
            self.assertEqual(
                sorted(item.raw_payload["source_import_ids"]),
                ["import-row-37", "import-row-54"],
            )
            self.assertEqual(item.raw_payload["line_total"], 480000)
            self.assertEqual(len(item.raw_payload["merged_source_rows"]), 1)

            scan_codes = db.execute(select(ScanCode)).scalars().all()
            self.assertEqual([code.order_item_id for code in scan_codes], [scanned_id])
            movements = db.execute(select(KizMovement)).scalars().all()
            self.assertEqual([movement.order_item_id for movement in movements], [scanned_id])

            actions = {row.action for row in db.execute(select(AuditLog)).scalars().all()}
            self.assertEqual(actions, {repair.REPAIR_ACTION, repair.BATCH_ACTION})

    def test_completed_order_keeps_completed_status(self):
        with self.SessionLocal() as db:
            order = self.make_order(status="completed", order_date=date(2026, 6, 2))
            db.add(order)
            db.add(self.make_item(order, blocks=4, scanned=0, source_row=26, status="completed"))
            db.add(self.make_item(order, blocks=2, scanned=0, source_row=79, status="completed"))
            db.commit()

        repair.run(apply_changes=True, only_open=False)

        with self.SessionLocal() as db:
            items = db.execute(select(OrderItem)).scalars().all()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].quantity_blocks, 6)
            self.assertEqual(items[0].status, "completed", "отгруженный заказ не переоткрывается")

    def test_only_open_skips_completed_orders(self):
        with self.SessionLocal() as db:
            closed = self.make_order(status="completed", order_date=date(2026, 6, 2))
            db.add(closed)
            db.add(self.make_item(closed, blocks=4, scanned=0, source_row=26, status="completed"))
            db.add(self.make_item(closed, blocks=2, scanned=0, source_row=79, status="completed"))
            open_order = self.make_order()
            db.add(open_order)
            db.add(self.make_item(open_order, blocks=1, scanned=0, source_row=37))
            db.add(self.make_item(open_order, blocks=1, scanned=1, source_row=54))
            db.commit()

        result = repair.run(apply_changes=True, only_open=True)

        self.assertEqual(result["groups"], 1)
        with self.SessionLocal() as db:
            items = db.execute(select(OrderItem)).scalars().all()
            self.assertEqual(len(items), 3)

    def test_different_products_are_not_merged(self):
        with self.SessionLocal() as db:
            order = self.make_order()
            db.add(order)
            db.add(self.make_item(order, blocks=1, scanned=0, source_row=36, product="Chapman Brown OP 20"))
            db.add(self.make_item(order, blocks=2, scanned=0, source_row=53, product="Chapman Green OP 20"))
            db.commit()

        result = repair.run(apply_changes=False, only_open=False)

        self.assertEqual(result["groups"], 0)


if __name__ == "__main__":
    unittest.main()
