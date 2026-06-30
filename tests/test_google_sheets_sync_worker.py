import unittest
import uuid
from datetime import date
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.google_sheets_exporter import build_import_record_row, build_import_sheet_header
from backend.app.google_sheets_sync_worker import (
    RETURN_DATE_COLUMN,
    RETURN_REFERENCE_COLUMN,
    RETURN_STATUS_COLUMN,
    RETURNED_BY_COLUMN,
    split_codes,
    sync_google_sheet_to_backend,
)
from backend.app.models import AuditLog, Base, Order, OrderItem, ScanCode
from backend.app.orders_service import list_active_orders


class FakeSheet:
    def __init__(self, rows):
        self.rows = rows
        self.batch_updates = []

    def get_all_values(self):
        return self.rows

    def batch_update(self, updates, value_input_option=None):
        self.batch_updates.extend(updates)


class GoogleSheetsSyncWorkerTests(unittest.TestCase):
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

    def seed_order(
        self,
        *,
        order_status="not_completed",
        item_status="not_completed",
        quantity_blocks=15,
        scanned_blocks=0,
    ):
        with self.SessionLocal() as db:
            order = Order(
                payment_type="Перечисление",
                client="Old Client",
                address="Old Address",
                representative="Old Rep",
                order_date=date(2026, 5, 31),
                status=order_status,
                raw_payload={"source": "test"},
            )
            item = OrderItem(
                order=order,
                product="Chapman Brown OP 20",
                quantity_pieces=150,
                quantity_blocks=quantity_blocks,
                pieces_per_block=10,
                scanned_blocks=scanned_blocks,
                status=item_status,
                raw_payload={
                    "source_import_id": "import-1",
                    "source_order_id": "order-1",
                    "block_price": 240000,
                },
            )
            db.add_all([order, item])
            db.commit()
            return str(order.id), str(item.id)

    def make_sheet(self, **overrides):
        record = {
            "Дата отгрузки": "01.06.2026",
            "Тип оплаты": "Терминал",
            "Клиент": "New Client",
            "Адрес": "New Address",
            "Торговый представитель": "New Rep",
            "Товары": "Chapman Red OP 20",
            "Кол-во ШТ": 110,
            "Кол-во блок": 11,
            "Статус": "Не выполнено",
            "ID заказа": "order-1",
            "ID импорта": "import-1",
            "Источник файла": "orders.xlsx",
            "Строка файла": "2",
            "Номер заявки SkladBot": "SB-100",
            "ID заявки SkladBot": "100",
            "Статус SkladBot": "found",
        }
        record.update(overrides)
        return FakeSheet([build_import_sheet_header(), build_import_record_row(record)])

    def make_return_sheet(self):
        header = build_import_sheet_header()
        row = build_import_record_row({
            "Дата отгрузки": "01.06.2026",
            "Тип оплаты": "Перечисление",
            "Клиент": "Old Client",
            "Адрес": "Old Address",
            "Торговый представитель": "Old Rep",
            "Товары": "Chapman Brown OP 20",
            "Кол-во ШТ": 150,
            "Кол-во блок": 15,
            "Отсканированные коды": "0101\n0102",
            "Статус": "Выполнено",
            "ID заказа": "order-1",
            "ID импорта": "import-1",
            "Номер заявки SkladBot": "SB-100",
            "ID заявки SkladBot": "100",
            "Статус SkladBot": "found",
        })
        for column, value in (
            (RETURN_STATUS_COLUMN, "Возврат"),
            (RETURN_DATE_COLUMN, "31.05.2026 23:30:00"),
            (RETURN_REFERENCE_COLUMN, "SB-100"),
            (RETURNED_BY_COLUMN, "desktop-test"),
        ):
            header.append(column)
            row.append(value)
        return FakeSheet([header, row])

    def add_second_item(self, order_id, *, scanned_blocks=0):
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            item = OrderItem(
                order=order,
                product="Chapman Gold SSL 100`20",
                quantity_pieces=20,
                quantity_blocks=2,
                pieces_per_block=10,
                scanned_blocks=scanned_blocks,
                status="not_completed",
                raw_payload={
                    "source_import_id": "import-2",
                    "source_order_id": "order-1",
                    "block_price": 240000,
                },
            )
            db.add(item)
            db.commit()
            return str(item.id)

    def test_sync_updates_active_backend_order_from_google_sheet_by_import_id(self):
        order_id, item_id = self.seed_order()

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(db, sheet=self.make_sheet())

        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["orders_updated"], 1)
        self.assertEqual(result["items_updated"], 1)
        self.assertEqual(result["conflicts"], 0)

        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(order.order_date, date(2026, 6, 1))
            self.assertEqual(order.payment_type, "Терминал")
            self.assertEqual(order.client, "New Client")
            self.assertEqual(order.address, "New Address")
            self.assertEqual(order.representative, "New Rep")
            self.assertEqual(order.raw_payload["skladbot_request_number"], "SB-100")
            self.assertEqual(order.raw_payload["skladbot_request_id"], "100")
            self.assertEqual(item.product, "Chapman Red OP 20")
            self.assertEqual(item.quantity_pieces, 110)
            self.assertEqual(item.quantity_blocks, 11)
            self.assertEqual(item.raw_payload["source_file"], "orders.xlsx")
            self.assertTrue(item.raw_payload["google_sheet_synced_at"])

    def test_sync_does_not_replace_real_backend_address_with_gps_from_google_sheet(self):
        order_id, _item_id = self.seed_order()

        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            order.address = "Ташкент, геокодированный адрес 1"
            db.commit()

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(
                db,
                sheet=self.make_sheet(
                    **{
                        "Адрес": "GPS: 41.311081,69.240562",
                        "Товары": "Chapman Brown OP 20",
                        "Кол-во ШТ": 150,
                        "Кол-во блок": 15,
                    }
                ),
            )

        self.assertEqual(result["matched"], 1)
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            self.assertEqual(order.address, "Ташкент, геокодированный адрес 1")

    def test_sync_does_not_downgrade_existing_skladbot_link_from_stale_google_mirror(self):
        order_id, _item_id = self.seed_order()

        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            order.raw_payload = {
                **order.raw_payload,
                "skladbot_request_number": "WH-R-199186",
                "skladbot_request_id": "199186",
                "skladbot_status": "created",
                "skladbot_created_by_taksklad": True,
            }
            db.commit()

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(
                db,
                sheet=self.make_sheet(
                    **{
                        "Товары": "Chapman Brown OP 20",
                        "Кол-во ШТ": 150,
                        "Кол-во блок": 15,
                        "Номер заявки SkladBot": "",
                        "ID заявки SkladBot": "",
                        "Статус SkladBot": "Проверяется",
                    }
                ),
            )

        self.assertEqual(result["matched"], 1)
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            self.assertEqual(order.raw_payload["skladbot_request_number"], "WH-R-199186")
            self.assertEqual(order.raw_payload["skladbot_request_id"], "199186")
            self.assertEqual(order.raw_payload["skladbot_status"], "created")

    def test_sync_recalculates_line_total_when_google_blocks_change(self):
        _, item_id = self.seed_order()

        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            item.raw_payload = {
                **item.raw_payload,
                "line_total": 3_600_000,
                "calculated_line_total": 3_600_000,
            }
            db.commit()

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(
                db,
                sheet=self.make_sheet(
                    **{
                        "Товары": "Chapman Brown OP 20",
                        "Кол-во ШТ": 10,
                        "Кол-во блок": 1,
                    }
                ),
            )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["items_updated"], 1)
        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.quantity_blocks, 1)
            self.assertEqual(item.raw_payload["block_price"], 240000)
            self.assertEqual(item.raw_payload["line_total"], 240000)
            self.assertEqual(item.raw_payload["calculated_line_total"], 240000)

    def test_sync_keeps_backend_item_removed_from_google_sheet_when_unscanned(self):
        order_id, _ = self.seed_order()
        removed_item_id = self.add_second_item(order_id)

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(db, sheet=self.make_sheet())

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["conflicts"], 1)
        with self.SessionLocal() as db:
            removed_item = db.get(OrderItem, uuid.UUID(removed_item_id))
            self.assertEqual(removed_item.status, "not_completed")
            active_orders = list_active_orders(db)
            self.assertEqual(len(active_orders), 1)
            self.assertEqual(len(active_orders[0].items), 2)

    def test_sync_keeps_backend_item_missing_from_google_sheet_when_scanned(self):
        order_id, _ = self.seed_order()
        scanned_item_id = self.add_second_item(order_id, scanned_blocks=1)

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(db, sheet=self.make_sheet())

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["conflicts"], 1)
        with self.SessionLocal() as db:
            scanned_item = db.get(OrderItem, uuid.UUID(scanned_item_id))
            self.assertEqual(scanned_item.status, "not_completed")
            audit = db.execute(
                select(AuditLog).where(AuditLog.action == "google_sheets_backend_sync_conflict")
            ).scalars().all()
            self.assertEqual(len(audit), 1)

    def test_sync_imports_scanned_codes_and_completes_item_from_google_sheet(self):
        order_id, item_id = self.seed_order(quantity_blocks=2)

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(
                db,
                sheet=self.make_sheet(
                    **{
                        "Товары": "Chapman Brown OP 20",
                        "Кол-во ШТ": 20,
                        "Кол-во блок": 2,
                        "Отсканированные коды": "0101\n0102",
                    }
                ),
            )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["items_updated"], 1)

        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            item = db.get(OrderItem, uuid.UUID(item_id))
            codes = db.execute(select(ScanCode).order_by(ScanCode.code)).scalars().all()
            self.assertEqual([scan.code for scan in codes], ["0101", "0102"])
            self.assertEqual(codes[0].source, "google_sheets")
            self.assertEqual(item.scanned_blocks, 2)
            self.assertEqual(item.status, "completed")
            self.assertEqual(order.status, "completed")

    def test_split_codes_keeps_comma_inside_kiz(self):
        first = "01012345678901234567ABC,DEF"
        second = "01012345678901234567XYZ"

        self.assertEqual(split_codes(f"{first}\n{second}"), [first, second])

    def test_sync_does_not_complete_incomplete_item_with_stale_completed_status(self):
        order_id, item_id = self.seed_order(quantity_blocks=2)

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(
                db,
                sheet=self.make_sheet(
                    **{
                        "Товары": "Chapman Brown OP 20",
                        "Кол-во ШТ": 20,
                        "Кол-во блок": 2,
                        "Отсканированные коды": "01012345678901234567ABC",
                        "Статус": "Выполнено",
                    }
                ),
            )

        self.assertEqual(result["matched"], 1)
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.scanned_blocks, 1)
            self.assertEqual(item.status, "not_completed")
            self.assertEqual(order.status, "not_completed")

    def test_sync_rejects_quantity_lower_than_already_scanned_blocks(self):
        _, item_id = self.seed_order(scanned_blocks=12)

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(
                db,
                sheet=self.make_sheet(
                    **{
                        "Дата отгрузки": "31.05.2026",
                        "Тип оплаты": "Перечисление",
                        "Клиент": "Old Client",
                        "Адрес": "Old Address",
                        "Торговый представитель": "Old Rep",
                        "Товары": "Chapman Brown OP 20",
                        "Кол-во ШТ": 150,
                        "Кол-во блок": 11,
                    }
                ),
            )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["items_updated"], 0)
        self.assertEqual(result["conflicts"], 1)

        with self.SessionLocal() as db:
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(item.quantity_blocks, 15)
            conflicts = db.execute(
                select(AuditLog).where(AuditLog.action == "google_sheets_backend_sync_conflict")
            ).scalars().all()
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0].payload["conflicts"][0]["field"], "quantity_blocks")

    def test_sync_keeps_completed_orders_in_sync_from_google_sheet(self):
        order_id, item_id = self.seed_order(order_status="completed", item_status="completed")

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(db, sheet=self.make_sheet())

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["missing"], 0)
        self.assertEqual(result["orders_updated"], 1)
        self.assertEqual(result["items_updated"], 1)

        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(order.client, "New Client")
            self.assertEqual(item.quantity_blocks, 11)

    def test_sync_archives_completed_backend_order_still_present_in_data_sheet(self):
        order_id, _ = self.seed_order(order_status="completed", item_status="completed")
        archived_ids = []

        with mock.patch(
            "backend.app.google_sheets_sync_worker.archive_backend_order_to_google_sheets",
            side_effect=lambda order: archived_ids.append(str(order.id)) or {"status": "completed", "updated": 1},
        ):
            with self.SessionLocal() as db:
                result = sync_google_sheet_to_backend(
                    db,
                    sheet=self.make_sheet(),
                    archive_completed_data_rows=True,
                )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["archived"], 1)
        self.assertEqual(archived_ids, [order_id])
        with self.SessionLocal() as db:
            audit = db.execute(
                select(AuditLog).where(AuditLog.action == "google_sheets_sync_archive_export")
            ).scalar_one()
            self.assertEqual(audit.entity_id, order_id)
            self.assertEqual(audit.payload["status"], "completed")

    def test_sync_ignores_manual_google_return_columns_and_keeps_backend_source_of_truth(self):
        order_id, item_id = self.seed_order(order_status="completed", item_status="completed")

        with self.SessionLocal() as db:
            result = sync_google_sheet_to_backend(db, sheet=self.make_return_sheet())

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["conflicts"], 1)

        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            item = db.get(OrderItem, uuid.UUID(item_id))
            self.assertEqual(order.status, "completed")
            self.assertEqual(item.status, "completed")
            self.assertNotIn("return_status", order.raw_payload)
            self.assertNotIn("returned_at", order.raw_payload)
            self.assertNotIn("return_reference", order.raw_payload)
            self.assertNotIn("returned_by", order.raw_payload)
            audit = db.execute(
                select(AuditLog).where(AuditLog.action == "google_sheets_backend_sync_conflict")
            ).scalar_one()
            self.assertEqual(audit.payload["conflicts"][0]["field"], "return_status")
            self.assertIn("backend return endpoint", audit.payload["conflicts"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
