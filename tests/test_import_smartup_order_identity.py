import unittest
from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.imports_service import create_import, normalize_import_row
from backend.app.models import Base, Order, OrderItem, PendingEvent
from backend.app.schemas import ImportCreate
from backend.app.skladbot_request_dry_run import SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE

FIRST_DEAL = "272382350"
SECOND_DEAL = "283500698"
SHARED_SOURCE_ORDER_ID = "synthetic-shared-order-id"


def import_row(smartup_order_id, row_id, blocks=1):
    """Строка шаблона отправки: «ID заказа» одинаковый, сделка Smartup разная."""
    return {
        "Дата отгрузки": "27.08.2026",
        "Тип оплаты": "Перечисление",
        "Клиент": "SYNTHETIC IDENTITY CLIENT",
        "Адрес": "SYNTHETIC IDENTITY ADDRESS",
        "Координаты": "41.0000000000, 69.0000000000",
        "Торговый представитель": "SYNTHETIC REP",
        "Товары": "SYNTHETIC PRODUCT",
        "Кол-во ШТ": blocks * 10,
        "Кол-во блок": blocks,
        "Цена за блок": 240000,
        "ID заказа": SHARED_SOURCE_ORDER_ID,
        "ID импорта": f"synthetic-import-row-{row_id}",
        "Smartup ИД заказа": smartup_order_id,
    }


class SmartupDealSplitImportTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()

    def run_import(self, rows, *, sha):
        with self.SessionLocal() as db:
            return create_import(db, ImportCreate(
                source="telegram",
                filename=f"synthetic-{sha[:6]}.xlsx",
                sha256=sha,
                rows=rows,
            ))

    def orders(self):
        with self.SessionLocal() as db:
            return list(db.execute(select(Order).order_by(Order.created_at, Order.id)).scalars())

    def items(self, order_id):
        with self.SessionLocal() as db:
            return list(db.execute(
                select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.product)
            ).scalars())

    def dry_run_order_ids(self, import_id):
        with self.SessionLocal() as db:
            events = list(db.execute(
                select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE)
            ).scalars())
        for event in events:
            payload = event.payload or {}
            if payload.get("import_id") == import_id:
                return [
                    (row.get("order_id"), row.get("status"))
                    for row in payload.get("dry_runs") or []
                ]
        return []

    def seed_legacy_order(self, smartup_order_id, row_id):
        """Заказ, заведённый прежним ключом, до разделения по сделке Smartup."""
        row = normalize_import_row(import_row(smartup_order_id, row_id))
        legacy_key = row["legacy_order_key"]
        with self.SessionLocal() as db:
            order = Order(
                source="telegram",
                external_id=legacy_key,
                import_order_key=legacy_key,
                import_source_order_key=legacy_key,
                order_date=date(2026, 8, 27),
                payment_type=row["payment_type"],
                client=row["client"],
                address=row["address"],
                representative=row["representative"],
                status="not_completed",
                raw_payload={
                    "order_key": legacy_key,
                    "source_order_id": f"smartup:{smartup_order_id}",
                    "coordinates": row["coordinates"],
                    "source": "telegram",
                },
            )
            db.add(order)
            db.flush()
            db.add(OrderItem(
                order_id=order.id,
                product=row["product"],
                import_item_key=row["item_key"],
                source_import_key=row["source_import_id"],
                source_import_id=row["source_import_id"],
                quantity_pieces=row["quantity_pieces"],
                quantity_blocks=row["quantity_blocks"],
                pieces_per_block=row["pieces_per_block"],
                scanned_blocks=0,
                requires_kiz=True,
                status="not_completed",
                raw_payload={
                    "item_key": row["item_key"],
                    "source_order_id": row["source_order_id"],
                    "smartup_order_ids": [smartup_order_id],
                    "source_import_id": row["source_import_id"],
                    "source_import_ids": [row["source_import_id"]],
                },
            ))
            db.commit()
            return str(order.id), legacy_key

    def test_two_smartup_deals_stay_two_orders_with_own_skladbot_requests(self):
        first = self.run_import([import_row(FIRST_DEAL, "first", blocks=1)], sha="a" * 64)
        second = self.run_import([import_row(SECOND_DEAL, "second", blocks=2)], sha="b" * 64)

        orders = self.orders()
        self.assertEqual(len(orders), 2, "две сделки Smartup обязаны остаться двумя заказами")
        self.assertEqual(first.orders_created, 1)
        self.assertEqual(second.orders_created, 1)
        self.assertEqual(second.merged_position_rows, 0, "дозаказ другой сделки не сливается в старый заказ")

        by_deal = {
            (order.raw_payload or {}).get("source_order_id"): order
            for order in orders
        }
        self.assertEqual(set(by_deal), {f"smartup:{FIRST_DEAL}", f"smartup:{SECOND_DEAL}"})
        self.assertEqual(
            [item.quantity_blocks for item in self.items(by_deal[f"smartup:{FIRST_DEAL}"].id)],
            [1],
        )
        self.assertEqual(
            [item.quantity_blocks for item in self.items(by_deal[f"smartup:{SECOND_DEAL}"].id)],
            [2],
        )

        second_dry_run = self.dry_run_order_ids(second.id)
        self.assertEqual(len(second_dry_run), 1, "второй импорт получает собственную заявку SkladBot")
        second_order_id, second_status = second_dry_run[0]
        self.assertEqual(second_order_id, str(by_deal[f"smartup:{SECOND_DEAL}"].id))
        self.assertNotEqual(second_status, "already_linked")

    def test_same_smartup_deal_addon_merges_into_one_order(self):
        self.run_import([import_row(FIRST_DEAL, "first", blocks=1)], sha="c" * 64)
        second = self.run_import([import_row(FIRST_DEAL, "addon", blocks=2)], sha="d" * 64)

        orders = self.orders()
        self.assertEqual(len(orders), 1, "та же сделка остаётся одним заказом")
        self.assertEqual(second.merged_position_rows, 1)
        items = self.items(orders[0].id)
        self.assertEqual([item.quantity_blocks for item in items], [3])
        self.assertEqual(len((items[0].raw_payload or {}).get("merged_source_rows") or []), 1)

    def test_legacy_key_order_of_same_deal_absorbs_addon(self):
        order_id, _legacy_key = self.seed_legacy_order(FIRST_DEAL, "legacy")
        result = self.run_import([import_row(FIRST_DEAL, "addon", blocks=2)], sha="e" * 64)

        orders = self.orders()
        self.assertEqual(len(orders), 1, "дозаказ той же сделки не заводит второй заказ")
        self.assertEqual(str(orders[0].id), order_id)
        self.assertEqual(result.orders_created, 0)
        self.assertEqual([item.quantity_blocks for item in self.items(orders[0].id)], [3])

    def test_legacy_key_order_of_other_deal_does_not_absorb_new_deal(self):
        order_id, _legacy_key = self.seed_legacy_order(FIRST_DEAL, "legacy")
        result = self.run_import([import_row(SECOND_DEAL, "second", blocks=2)], sha="f" * 64)

        orders = self.orders()
        self.assertEqual(len(orders), 2, "другая сделка Smartup заводит свой заказ")
        self.assertEqual(result.orders_created, 1)
        other = [order for order in orders if str(order.id) != order_id][0]
        self.assertEqual((other.raw_payload or {}).get("source_order_id"), f"smartup:{SECOND_DEAL}")

    def test_repeated_file_adds_nothing(self):
        rows = [import_row(FIRST_DEAL, "first", blocks=1)]
        self.run_import(rows, sha="1" * 64)
        repeat = self.run_import(rows, sha="2" * 64)

        orders = self.orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(repeat.items_created, 0)
        self.assertEqual(repeat.duplicate_rows, 1)
        self.assertEqual([item.quantity_blocks for item in self.items(orders[0].id)], [1])

    def test_row_without_smartup_deal_keeps_previous_order_key(self):
        row = import_row(FIRST_DEAL, "first")
        row.pop("Smartup ИД заказа")
        normalized = normalize_import_row(row)
        self.assertEqual(normalized["order_key"], normalized["legacy_order_key"])

    def test_row_with_smartup_deal_changes_order_key_only(self):
        normalized = normalize_import_row(import_row(FIRST_DEAL, "first"))
        other = normalize_import_row(import_row(SECOND_DEAL, "second"))
        self.assertNotEqual(normalized["order_key"], normalized["legacy_order_key"])
        self.assertEqual(normalized["legacy_order_key"], other["legacy_order_key"])
        self.assertNotEqual(normalized["order_key"], other["order_key"])


if __name__ == "__main__":
    unittest.main()
