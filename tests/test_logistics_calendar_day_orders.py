import unittest
import uuid
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.logistics_calendar_orders_service import list_logistics_calendar_day_orders
from backend.app.logistics_zone_service import normalize_client_key
from backend.app.models import Base, LogisticsRegionPoint, Order, OrderItem


class LogisticsCalendarDayOrdersTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def test_orders_carry_zone_products_and_blocks(self):
        with self.Session() as db:
            db.add(LogisticsRegionPoint(
                id=uuid.uuid4(),
                client_name="Тест Клиент 2",
                normalized_client=normalize_client_key("Тест Клиент 2"),
                latitude=41.2,
                longitude=69.9,
                is_active=True,
            ))
            city = Order(
                id=uuid.uuid4(),
                source="test",
                order_date=date(2026, 8, 7),
                payment_type="Наличные",
                client="Тест Клиент 1",
                address="Ташкент, дом 1",
                representative="Тест Представитель",
                status="not_completed",
                raw_payload={"coordinates": "41.3200,69.2400", "skladbot_request_number": "WH-R-1"},
            )
            city.items = [OrderItem(
                id=uuid.uuid4(),
                product="Тест Товар А",
                quantity_blocks=10,
                scanned_blocks=4,
                raw_payload={"line_total": 240000},
            )]
            region = Order(
                id=uuid.uuid4(),
                source="test",
                order_date=date(2026, 8, 7),
                payment_type="Наличные",
                client="Тест Клиент 2",
                address="Область, дом 2",
                status="not_completed",
                raw_payload={"coordinates": "41.3200,69.2400"},
            )
            region.items = [OrderItem(id=uuid.uuid4(), product="Тест Товар Б", quantity_blocks=3)]
            db.add_all([city, region])
            db.commit()

            payload = list_logistics_calendar_day_orders(db, date(2026, 8, 7))
            by_client = {row["client"]: row for row in payload["orders"]}

            self.assertEqual(payload["date"], date(2026, 8, 7))
            self.assertFalse(payload["region_directory_empty"])
            self.assertEqual(by_client["Тест Клиент 1"]["zone"], "city")
            self.assertEqual(by_client["Тест Клиент 1"]["products"], "Тест Товар А")
            self.assertEqual(by_client["Тест Клиент 1"]["quantity_blocks"], 10)
            self.assertEqual(by_client["Тест Клиент 1"]["scanned_blocks"], 4)
            self.assertEqual(by_client["Тест Клиент 1"]["remaining_blocks"], 6)
            self.assertEqual(by_client["Тест Клиент 1"]["representative"], "Тест Представитель")
            self.assertEqual(by_client["Тест Клиент 1"]["skladbot_request_number"], "WH-R-1")
            self.assertEqual(by_client["Тест Клиент 1"]["line_total"], 240000)
            self.assertFalse(by_client["Тест Клиент 1"]["is_returned"])
            self.assertEqual(by_client["Тест Клиент 2"]["zone"], "region")


if __name__ == "__main__":
    unittest.main()
