import unittest
import uuid
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.logistics_calendar_orders_service import (
    list_logistics_calendar_day_orders,
    order_lifecycle_status,
)
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
            # 4 из 10 отсканировано, независимо от даты доставки это "в сборке"
            self.assertEqual(by_client["Тест Клиент 1"]["lifecycle_status"], "assembling")
            self.assertEqual(by_client["Тест Клиент 2"]["zone"], "region")
            self.assertEqual(by_client["Тест Клиент 2"]["lifecycle_status"], "assembling")

    def test_return_order_keeps_own_zone_and_pickup_order_is_excluded(self):
        with self.Session() as db:
            db.add(LogisticsRegionPoint(
                id=uuid.uuid4(),
                client_name="Тест Клиент 4",
                normalized_client=normalize_client_key("Тест Клиент 4"),
                latitude=41.2,
                longitude=69.9,
                is_active=True,
            ))
            normal = Order(
                id=uuid.uuid4(),
                source="test",
                order_date=date(2026, 8, 7),
                payment_type="Наличные",
                client="Тест Клиент 3",
                address="Ташкент, дом 3",
                status="not_completed",
                raw_payload={"coordinates": "41.3200,69.2400"},
            )
            normal.items = [OrderItem(id=uuid.uuid4(), product="Тест Товар В", quantity_blocks=1)]
            returned = Order(
                id=uuid.uuid4(),
                source="test",
                order_date=date(2026, 8, 7),
                payment_type="Наличные",
                client="Тест Клиент 4",
                address="Область, дом 4",
                status="returned",
                raw_payload={"coordinates": "41.3200,69.2400", "return_status": "returned"},
            )
            returned.items = [OrderItem(id=uuid.uuid4(), product="Тест Товар Г", quantity_blocks=1)]
            pickup = Order(
                id=uuid.uuid4(),
                source="test",
                order_date=date(2026, 8, 7),
                payment_type="Наличные",
                client="Тест Клиент 5",
                address="Самовывоз со склада",
                status="not_completed",
                raw_payload={"coordinates": "41.3200,69.2400"},
            )
            pickup.items = [OrderItem(id=uuid.uuid4(), product="Тест Товар Д", quantity_blocks=1)]
            db.add_all([normal, returned, pickup])
            db.commit()

            payload = list_logistics_calendar_day_orders(db, date(2026, 8, 7))
            by_client = {row["client"]: row for row in payload["orders"]}

            self.assertEqual(len(payload["orders"]), 2)
            self.assertNotIn("Тест Клиент 5", by_client)
            self.assertFalse(by_client["Тест Клиент 3"]["is_returned"])
            self.assertEqual(by_client["Тест Клиент 3"]["zone"], "city")
            # не собран (0 из 1), статус "в сборке" вне зависимости от даты доставки
            self.assertEqual(by_client["Тест Клиент 3"]["lifecycle_status"], "assembling")
            self.assertTrue(by_client["Тест Клиент 4"]["is_returned"])
            self.assertEqual(by_client["Тест Клиент 4"]["zone"], "region")
            # возврат перекрывает собранность и дату доставки
            self.assertEqual(by_client["Тест Клиент 4"]["lifecycle_status"], "returned")

    def test_source_file_comes_from_order_item_raw_payload(self):
        with self.Session() as db:
            order = Order(
                id=uuid.uuid4(),
                source="test",
                order_date=date(2026, 8, 7),
                payment_type="Наличные",
                client="Тест Клиент 6",
                address="Ташкент, дом 6",
                status="not_completed",
                raw_payload={"coordinates": "41.3200,69.2400"},
            )
            order.items = [OrderItem(
                id=uuid.uuid4(),
                product="Тест Товар Е",
                quantity_blocks=2,
                raw_payload={"source_file": "zakaz-06-08.xlsx"},
            )]
            db.add(order)
            db.commit()

            payload = list_logistics_calendar_day_orders(db, date(2026, 8, 7))

            self.assertEqual(len(payload["orders"]), 1)
            self.assertEqual(payload["orders"][0]["source_file"], "zakaz-06-08.xlsx")

    def test_empty_region_directory_keeps_everything_in_city(self):
        with self.Session() as db:
            order = Order(
                id=uuid.uuid4(),
                source="test",
                order_date=date(2026, 8, 7),
                payment_type="Наличные",
                client="Тест Клиент 7",
                address="Область, дом 7",
                status="not_completed",
                raw_payload={"coordinates": "41.0180,70.0830"},
            )
            order.items = [OrderItem(id=uuid.uuid4(), product="Тест Товар Ж", quantity_blocks=5)]
            db.add(order)
            db.commit()

            payload = list_logistics_calendar_day_orders(db, date(2026, 8, 7))

            self.assertTrue(payload["region_directory_empty"])
            self.assertEqual(len(payload["orders"]), 1)
            self.assertEqual(payload["orders"][0]["zone"], "city")


class OrderLifecycleStatusTests(unittest.TestCase):
    """По одному тесту на каждую из пяти веток order_lifecycle_status

    Дата доставки в каждом тесте задаётся относительно явно переданного today,
    а не текущей даты, чтобы тесты не начали падать завтра
    """

    def test_returned_order_is_returned_regardless_of_blocks_and_date(self):
        today = date(2026, 8, 10)
        order = Order(
            id=uuid.uuid4(),
            source="test",
            order_date=today,
            payment_type="Наличные",
            client="Тест Клиент 8",
            address="Область, дом 8",
            status="returned",
            raw_payload={"return_status": "returned"},
        )
        order.items = [OrderItem(
            id=uuid.uuid4(),
            product="Тест Товар З",
            quantity_blocks=2,
            scanned_blocks=2,
        )]

        # заказ полностью собран и дата доставки ровно сегодня, но возврат важнее
        self.assertEqual(order_lifecycle_status(order, today), "returned")

    def test_assembling_when_scanned_blocks_are_fewer_than_planned(self):
        today = date(2026, 8, 10)
        order = Order(
            id=uuid.uuid4(),
            source="test",
            order_date=today - timedelta(days=1),
            payment_type="Наличные",
            client="Тест Клиент 9",
            address="Ташкент, дом 9",
            status="not_completed",
            raw_payload={},
        )
        order.items = [OrderItem(
            id=uuid.uuid4(),
            product="Тест Товар И",
            quantity_blocks=5,
            scanned_blocks=2,
        )]

        # день доставки уже прошёл, но недособранный заказ остаётся "в сборке"
        self.assertEqual(order_lifecycle_status(order, today), "assembling")

    def test_assembled_when_delivery_date_is_after_today(self):
        today = date(2026, 8, 10)
        order = Order(
            id=uuid.uuid4(),
            source="test",
            order_date=today + timedelta(days=2),
            payment_type="Наличные",
            client="Тест Клиент 10",
            address="Ташкент, дом 10",
            status="not_completed",
            raw_payload={},
        )
        order.items = [OrderItem(
            id=uuid.uuid4(),
            product="Тест Товар К",
            quantity_blocks=4,
            scanned_blocks=4,
        )]

        self.assertEqual(order_lifecycle_status(order, today), "assembled")

    def test_shipped_when_delivery_date_is_today(self):
        today = date(2026, 8, 10)
        order = Order(
            id=uuid.uuid4(),
            source="test",
            order_date=today,
            payment_type="Наличные",
            client="Тест Клиент 11",
            address="Ташкент, дом 11",
            status="not_completed",
            raw_payload={},
        )
        order.items = [OrderItem(
            id=uuid.uuid4(),
            product="Тест Товар Л",
            quantity_blocks=4,
            scanned_blocks=4,
        )]

        self.assertEqual(order_lifecycle_status(order, today), "shipped")

    def test_delivered_when_delivery_date_is_before_today(self):
        today = date(2026, 8, 10)
        order = Order(
            id=uuid.uuid4(),
            source="test",
            order_date=today - timedelta(days=3),
            payment_type="Наличные",
            client="Тест Клиент 12",
            address="Ташкент, дом 12",
            status="not_completed",
            raw_payload={},
        )
        order.items = [OrderItem(
            id=uuid.uuid4(),
            product="Тест Товар М",
            quantity_blocks=4,
            scanned_blocks=4,
        )]

        self.assertEqual(order_lifecycle_status(order, today), "delivered")

    def test_order_without_items_counts_as_assembled(self):
        today = date(2026, 8, 10)
        order = Order(
            id=uuid.uuid4(),
            source="test",
            order_date=today + timedelta(days=1),
            payment_type="Наличные",
            client="Тест Клиент 13",
            address="Ташкент, дом 13",
            status="not_completed",
            raw_payload={},
        )
        order.items = []

        # 0 отсканировано из 0 запланировано, это не меньше плана
        self.assertEqual(order_lifecycle_status(order, today), "assembled")

    def test_completed_status_with_zero_scans_and_past_delivery_is_delivered(self):
        today = date(2026, 8, 10)
        order = Order(
            id=uuid.uuid4(),
            source="test",
            order_date=today - timedelta(days=3),
            payment_type="Наличные",
            client="Тест Клиент 15",
            address="Ташкент, дом 15",
            status="completed",
            raw_payload={"completed_without_kiz": True},
        )
        order.items = [OrderItem(
            id=uuid.uuid4(),
            product="Тест Товар О",
            quantity_blocks=5,
            scanned_blocks=0,
        )]

        # заказ завершили в веб-админке без сканирования КИЗ: статус заказа
        # перекрывает нулевые сканы, дата доставки в прошлом даёт "доставлен"
        self.assertEqual(order_lifecycle_status(order, today), "delivered")

    def test_completed_status_with_zero_scans_and_delivery_today_is_shipped(self):
        today = date(2026, 8, 10)
        order = Order(
            id=uuid.uuid4(),
            source="test",
            order_date=today,
            payment_type="Наличные",
            client="Тест Клиент 16",
            address="Ташкент, дом 16",
            status="completed",
            raw_payload={"completed_without_kiz": True},
        )
        order.items = [OrderItem(
            id=uuid.uuid4(),
            product="Тест Товар П",
            quantity_blocks=5,
            scanned_blocks=0,
        )]

        # тот же случай, но дата доставки ровно сегодня: "отгружен"
        self.assertEqual(order_lifecycle_status(order, today), "shipped")

    def test_not_completed_status_with_partial_scans_stays_assembling(self):
        today = date(2026, 8, 10)
        order = Order(
            id=uuid.uuid4(),
            source="test",
            order_date=today - timedelta(days=1),
            payment_type="Наличные",
            client="Тест Клиент 17",
            address="Ташкент, дом 17",
            status="not_completed",
            raw_payload={},
        )
        order.items = [OrderItem(
            id=uuid.uuid4(),
            product="Тест Товар Р",
            quantity_blocks=6,
            scanned_blocks=2,
        )]

        # статус не входит в завершающие, проверка блоков работает как раньше
        self.assertEqual(order_lifecycle_status(order, today), "assembling")

    def test_returned_order_wins_over_completed_status(self):
        today = date(2026, 8, 10)
        order = Order(
            id=uuid.uuid4(),
            source="test",
            order_date=today - timedelta(days=1),
            payment_type="Наличные",
            client="Тест Клиент 18",
            address="Область, дом 18",
            status="completed",
            raw_payload={"return_status": "returned"},
        )
        order.items = [OrderItem(
            id=uuid.uuid4(),
            product="Тест Товар С",
            quantity_blocks=3,
            scanned_blocks=0,
        )]

        # возврат остаётся первой веткой, даже когда статус заказа "завершён"
        self.assertEqual(order_lifecycle_status(order, today), "returned")


class LogisticsCalendarDayOrdersTimezoneTests(unittest.TestCase):
    """Проверяет, что today реально читается через report_timezone, а не через
    date.today() или UTC

    Pacific/Kiritimati (UTC+14) и Pacific/Midway (UTC-11) разнесены на 25 часов,
    больше суток, поэтому календарная дата в этих зонах отличается в любой момент
    времени: Киритимати всегда на 1-2 дня впереди Мидуэя. Наивная реализация,
    игнорирующая переданный пояс, дала бы одинаковый статус под обеими зонами,
    флакать тест не может именно по этой причине
    """

    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def test_today_follows_patched_report_timezone_not_local_clock(self):
        kiritimati = ZoneInfo("Pacific/Kiritimati")
        midway = ZoneInfo("Pacific/Midway")
        # дата доставки равна сегодняшней дате в первой зоне (Киритимати)
        delivery_date = datetime.now(kiritimati).date()

        with self.Session() as db:
            order = Order(
                id=uuid.uuid4(),
                source="test",
                order_date=delivery_date,
                payment_type="Наличные",
                client="Тест Клиент 14",
                address="Ташкент, дом 14",
                status="not_completed",
                raw_payload={"coordinates": "41.3200,69.2400"},
            )
            order.items = [OrderItem(
                id=uuid.uuid4(),
                product="Тест Товар Н",
                quantity_blocks=2,
                scanned_blocks=2,
            )]
            db.add(order)
            db.commit()

            with mock.patch(
                "backend.app.logistics_calendar_orders_service.report_timezone",
                return_value=kiritimati,
            ):
                payload_kiritimati = list_logistics_calendar_day_orders(db, delivery_date)

            with mock.patch(
                "backend.app.logistics_calendar_orders_service.report_timezone",
                return_value=midway,
            ):
                payload_midway = list_logistics_calendar_day_orders(db, delivery_date)

        status_kiritimati = payload_kiritimati["orders"][0]["lifecycle_status"]
        status_midway = payload_midway["orders"][0]["lifecycle_status"]

        # под Киритимати сегодня равно дате доставки: отгружен
        self.assertEqual(status_kiritimati, "shipped")
        # под Мидуэем сегодня всегда раньше даты доставки: собран, ещё не отгружен
        self.assertEqual(status_midway, "assembled")
        # разные пояса обязаны дать разный статус, иначе report_timezone не читается
        self.assertNotEqual(status_kiritimati, status_midway)


if __name__ == "__main__":
    unittest.main()
