import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.imports_service import create_import
from backend.app.models import Base, ImportJob, Order, OrderItem
from backend.app.schemas import ImportCreate


class TelegramImportIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()

    def test_same_telegram_event_returns_existing_import_job(self):
        event_id = str(uuid.uuid4())
        payload = ImportCreate(
            source="telegram",
            filename="synthetic-orders.xlsx",
            sha256="e" * 64,
            telegram_event_id=event_id,
            rows=[{
                "Дата отгрузки": "21.07.2026",
                "Тип оплаты": "SYNTHETIC",
                "Клиент": "SYNTHETIC CLIENT",
                "Адрес": "SYNTHETIC ADDRESS",
                "Товары": "SYNTHETIC PRODUCT",
                "Кол-во ШТ": 20,
                "Кол-во блок": 2,
                "ID заказа": "synthetic-order",
                "ID импорта": "synthetic-row",
            }],
        )
        skladbot_result = {
            "status": "synthetic_stub",
            "ready": 0,
            "blocked": 0,
            "already_linked": 0,
            "linked_mismatch": 0,
            "event_id": "",
        }
        with patch(
            "backend.app.imports_service.create_skladbot_dry_run_for_import",
            return_value=skladbot_result,
        ):
            with self.Session() as db:
                first = create_import(db, payload)
            with self.Session() as db:
                second = create_import(db, payload)

        self.assertEqual(second.id, first.id)
        self.assertEqual(second.orders_created, first.orders_created)
        self.assertEqual(second.items_created, first.items_created)
        with self.Session() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ImportJob)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(Order)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(OrderItem)), 1)


if __name__ == "__main__":
    unittest.main()
