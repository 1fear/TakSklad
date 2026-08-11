import unittest
import uuid
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.logistics_calendar_service import list_logistics_calendar
from backend.app.logistics_zone_service import normalize_client_key
from backend.app.models import Base, LogisticsRegionPoint, Order, OrderItem


def make_order(db, *, client, address, coordinates, blocks, status="not_completed", returned=False):
    order = Order(
        id=uuid.uuid4(),
        source="test",
        order_date=date(2026, 8, 7),
        payment_type="Наличные",
        client=client,
        address=address,
        status="returned" if returned else status,
        raw_payload={"coordinates": coordinates},
    )
    order.items = [OrderItem(id=uuid.uuid4(), product="Тест Товар", quantity_blocks=blocks)]
    db.add(order)
    return order


class LogisticsCalendarZoneSummaryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def test_zone_counters_split_orders_returns_and_blocks(self):
        with self.Session() as db:
            db.add(LogisticsRegionPoint(
                id=uuid.uuid4(),
                client_name="Тест Клиент 2",
                normalized_client=normalize_client_key("Тест Клиент 2"),
                latitude=41.2,
                longitude=69.9,
                is_active=True,
            ))
            make_order(db, client="Тест Клиент 1", address="Ташкент, дом 1", coordinates="41.3200,69.2400", blocks=10)
            make_order(db, client="Тест Клиент 2", address="Область, дом 2", coordinates="41.3200,69.2400", blocks=4)
            make_order(db, client="Тест Клиент 3", address="Ташкент, дом 3", coordinates="41.3200,69.2400", blocks=6, returned=True)
            db.commit()

            calendar = list_logistics_calendar(db, "2026-08")
            day = next(item for item in calendar["days"] if item["date"] == date(2026, 8, 7))

            self.assertEqual(day["city_orders"], 1)
            self.assertEqual(day["region_orders"], 1)
            self.assertEqual(day["city_returns"], 1)
            self.assertEqual(day["region_returns"], 0)
            self.assertEqual(day["city_blocks"], 10)
            self.assertEqual(day["region_blocks"], 4)
            self.assertEqual(day["city_orders"] + day["region_orders"], day["orders_count"])
            self.assertEqual(day["city_returns"] + day["region_returns"], day["returned_orders"])
            self.assertEqual(day["city_blocks"] + day["region_blocks"], day["planned_blocks"])
            self.assertFalse(calendar["region_directory_empty"])

    def test_empty_region_directory_keeps_everything_in_city(self):
        with self.Session() as db:
            make_order(db, client="Тест Клиент 4", address="Область, дом 4", coordinates="41.0180,70.0830", blocks=5)
            db.commit()

            calendar = list_logistics_calendar(db, "2026-08")
            day = next(item for item in calendar["days"] if item["date"] == date(2026, 8, 7))

            self.assertTrue(calendar["region_directory_empty"])
            self.assertEqual(day["city_orders"], 1)
            self.assertEqual(day["region_orders"], 0)
            self.assertEqual(day["city_blocks"], 5)

    def test_pickup_order_counts_as_excluded_and_not_in_zones(self):
        with self.Session() as db:
            db.add(LogisticsRegionPoint(
                id=uuid.uuid4(),
                client_name="Тест Клиент 5",
                normalized_client=normalize_client_key("Тест Клиент 5"),
                latitude=41.2,
                longitude=69.9,
                is_active=True,
            ))
            make_order(db, client="Тест Клиент 6", address="Самовывоз со склада", coordinates="41.3200,69.2400", blocks=3)
            db.commit()

            day = next(
                item for item in list_logistics_calendar(db, "2026-08")["days"]
                if item["date"] == date(2026, 8, 7)
            )

            self.assertEqual(day["excluded_orders"], 1)
            self.assertEqual(day["orders_count"], 0)
            self.assertEqual(day["city_orders"], 0)
            self.assertEqual(day["region_orders"], 0)


if __name__ == "__main__":
    unittest.main()
