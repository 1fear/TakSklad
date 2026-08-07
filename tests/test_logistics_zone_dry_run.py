import unittest
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import Base, LogisticsRegionPoint, Order, OrderItem
from tools.logistics_zone_dry_run import summarize


SHIPMENT_DATE = date(2030, 1, 2)


class LogisticsZoneDryRunTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.SessionLocal()
        self.db.add(LogisticsRegionPoint(
            client_name="Тест Клиент Область",
            normalized_client="тестклиентобласть",
            latitude=41.018778,
            longitude=70.083423,
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add_order(self, client, coordinates):
        order = Order(
            payment_type="Наличные",
            client=client,
            address="Тестовый адрес",
            status="not_completed",
            order_date=SHIPMENT_DATE,
            raw_payload={"coordinates": coordinates},
        )
        order.items.append(OrderItem(product="Тестовый товар", quantity_blocks=1))
        self.db.add(order)
        self.db.commit()

    def test_summary_counts_rows_and_keeps_unknown_suburb_in_region(self):
        self.add_order("Тест Клиент Область", "41.018778,70.083423")
        self.add_order("Тест Клиент Город", "41.3200,69.2400")
        self.add_order("Незнакомый Загород", "41.4700,69.5800")
        summary = summarize(self.db, SHIPMENT_DATE.isoformat())
        self.assertEqual(summary["city_rows"], 1)
        self.assertEqual(summary["region_rows"], 2)
        self.assertEqual(summary["unassigned"], 0)
        self.assertEqual(summary["unassigned_clients"], [])

    def test_summary_flags_empty_region_directory(self):
        self.db.query(LogisticsRegionPoint).delete()
        self.db.commit()
        self.add_order("Тест Клиент Область", "41.018778,70.083423")
        summary = summarize(self.db, SHIPMENT_DATE.isoformat())
        self.assertTrue(summary["region_directory_empty"])
        self.assertEqual(summary["city_rows"], 1)
        self.assertEqual(summary["region_rows"], 0)

    def test_summary_reports_zeros_for_missing_zone(self):
        self.add_order("Тест Клиент Город", "41.3200,69.2400")
        summary = summarize(self.db, SHIPMENT_DATE.isoformat())
        self.assertEqual(summary["region_rows"], 0)
        self.assertEqual(summary["unassigned"], 0)


if __name__ == "__main__":
    unittest.main()
