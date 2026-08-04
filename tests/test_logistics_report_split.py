import unittest
from datetime import date
from io import BytesIO

from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.logistics_service import build_logistics_report_xlsx, build_logistics_reports
from backend.app.models import Base, LogisticsRegionPoint, Order, OrderItem
from backend.app.orders_service import ApiError


SHIPMENT_DATE = date(2030, 1, 2)


class LogisticsReportSplitTests(unittest.TestCase):
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

    def add_order(self, client, coordinates, address="Тестовый адрес"):
        order = Order(
            payment_type="Наличные",
            client=client,
            address=address,
            status="not_completed",
            order_date=SHIPMENT_DATE,
            raw_payload={"coordinates": coordinates},
        )
        order.items.append(OrderItem(product="Тестовый товар", quantity_blocks=3))
        self.db.add(order)
        self.db.commit()
        return order

    def sheet_rows(self, payload):
        workbook = load_workbook(BytesIO(payload))
        return [
            [cell for cell in row]
            for row in workbook["Orders"].iter_rows(min_row=2, values_only=True)
        ]

    def test_orders_split_between_city_and_region_files(self):
        self.add_order("Тест Клиент Область", "41.018778,70.083423")
        self.add_order("Тест Клиент Город", "41.3200,69.2400")
        reports = build_logistics_reports(self.db, SHIPMENT_DATE.isoformat())

        self.assertIsNotNone(reports["city"])
        self.assertIsNotNone(reports["region"])
        self.assertEqual(reports["unassigned"], [])

        city_payload, city_filename = reports["city"]
        region_payload, region_filename = reports["region"]
        self.assertEqual(city_filename, "TakSklad_логистика_город_02.01.2030.xlsx")
        self.assertEqual(region_filename, "TakSklad_логистика_область_02.01.2030.xlsx")

        city_clients = {row[3] for row in self.sheet_rows(city_payload)}
        region_clients = {row[3] for row in self.sheet_rows(region_payload)}
        self.assertEqual(city_clients, {"Тест Клиент Город"})
        self.assertEqual(region_clients, {"Тест Клиент Область"})

    def test_unknown_client_outside_city_is_unassigned_and_absent_from_both_files(self):
        self.add_order("Тест Клиент Город", "41.3200,69.2400")
        self.add_order("Незнакомый Загород", "41.4700,69.5800")
        reports = build_logistics_reports(self.db, SHIPMENT_DATE.isoformat())

        self.assertIsNone(reports["region"])
        self.assertEqual(len(reports["unassigned"]), 1)
        self.assertEqual(reports["unassigned"][0].client, "Незнакомый Загород")

        city_clients = {row[3] for row in self.sheet_rows(reports["city"][0])}
        self.assertEqual(city_clients, {"Тест Клиент Город"})

    def test_zone_without_orders_produces_no_file(self):
        self.add_order("Тест Клиент Город", "41.3200,69.2400")
        reports = build_logistics_reports(self.db, SHIPMENT_DATE.isoformat())
        self.assertIsNone(reports["region"])
        self.assertIsNotNone(reports["city"])

    def test_unknown_client_without_coordinates_lands_in_city_problem_sheet(self):
        self.add_order("Незнакомый Без Координат", "")
        reports = build_logistics_reports(self.db, SHIPMENT_DATE.isoformat())
        payload, _filename = reports["city"]
        workbook = load_workbook(BytesIO(payload))
        self.assertIn("Требуют координаты", workbook.sheetnames)
        problem_clients = [
            row[0] for row in workbook["Требуют координаты"].iter_rows(min_row=2, values_only=True)
        ]
        self.assertEqual(problem_clients, ["Незнакомый Без Координат"])

    def test_known_client_without_coordinates_lands_in_region_problem_sheet(self):
        self.add_order("Тест Клиент Область", "")
        reports = build_logistics_reports(self.db, SHIPMENT_DATE.isoformat())
        self.assertIsNone(reports["city"])
        payload, _filename = reports["region"]
        workbook = load_workbook(BytesIO(payload))
        problem_clients = [
            row[0] for row in workbook["Требуют координаты"].iter_rows(min_row=2, values_only=True)
        ]
        self.assertEqual(problem_clients, ["Тест Клиент Область"])

    def test_single_zone_builder_raises_404_for_empty_zone(self):
        self.add_order("Тест Клиент Город", "41.3200,69.2400")
        payload, filename = build_logistics_report_xlsx(self.db, SHIPMENT_DATE.isoformat(), "city")
        self.assertTrue(payload)
        self.assertEqual(filename, "TakSklad_логистика_город_02.01.2030.xlsx")
        with self.assertRaises(ApiError) as raised:
            build_logistics_report_xlsx(self.db, SHIPMENT_DATE.isoformat(), "region")
        self.assertEqual(raised.exception.status_code, 404)

    def test_unknown_zone_is_rejected_with_422(self):
        self.add_order("Тест Клиент Город", "41.3200,69.2400")
        with self.assertRaises(ApiError) as raised:
            build_logistics_report_xlsx(self.db, SHIPMENT_DATE.isoformat(), "moon")
        self.assertEqual(raised.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
