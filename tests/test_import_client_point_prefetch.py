import unittest
from datetime import date
from unittest.mock import patch

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import selectinload, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.client_points_service import (
    canonical_search_identifiers,
    list_client_points,
    prefetch_client_points_for_import,
    sync_client_point_from_import_row_cached,
)
from backend.app.imports_service import create_import, normalize_smartup_order_id
from backend.app.logistics_service import logistics_external_id
from backend.app.models import Base, ClientPoint, Order, OrderItem
from backend.app.schemas import ImportCreate


def synthetic_import_row(index):
    return {
        "Дата отгрузки": "10.07.2026",
        "Тип оплаты": "SYNTHETIC",
        "Клиент": f"SYNTHETIC CLIENT {index:04d}",
        "Адрес": f"SYNTHETIC ADDRESS {index:04d}",
        "Координаты": f"0.{index:04d}, 0.{index:04d}",
        "Торговый представитель": "SYNTHETIC REP",
        "Товары": f"SYNTHETIC PRODUCT {index % 10:02d}",
        "Кол-во ШТ": 20,
        "Кол-во блок": 2,
        "ID импорта": f"synthetic-row-{index:04d}",
    }


class ImportClientPointPrefetchTests(unittest.TestCase):
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

    def test_cached_sync_preserves_client_identity_timeslot_and_last_row_values(self):
        with self.SessionLocal() as db:
            point = ClientPoint(
                client_name="Client",
                address="Old Address",
                normalized_client="client",
                normalized_address="oldaddress",
                coordinates="old coordinates",
                representative="Old Rep",
                delivery_from="08:30",
                delivery_to="11:45",
                raw_payload={"source": "web"},
            )
            db.add(point)
            db.commit()
            point_id = point.id

            rows = [
                {
                    "client": "CLIENT",
                    "address": "First New Address",
                    "coordinates": "new coordinates",
                    "representative": "New Rep",
                },
                {
                    "client": "Client",
                    "address": "Last New Address",
                    "coordinates": "",
                    "representative": "",
                },
            ]
            cache = prefetch_client_points_for_import(db, rows)
            for row in rows:
                sync_client_point_from_import_row_cached(db, row, cache)
            db.commit()

            saved = db.execute(select(ClientPoint)).scalar_one()
            self.assertEqual(saved.id, point_id)
            self.assertEqual(saved.client_name, "Client")
            self.assertEqual(saved.address, "Last New Address")
            self.assertEqual(saved.normalized_address, "lastnewaddress")
            self.assertEqual(saved.coordinates, "new coordinates")
            self.assertEqual(saved.representative, "New Rep")
            self.assertEqual((saved.delivery_from, saved.delivery_to), ("08:30", "11:45"))

    def test_1000_row_import_uses_bounded_statement_count_and_preserves_rows(self):
        payload = ImportCreate(
            source="synthetic_query_count",
            filename="synthetic-query-count.xlsx",
            rows=[synthetic_import_row(index) for index in range(1000)],
        )
        counter = {"enabled": False, "statements": 0}

        @event.listens_for(self.engine, "before_cursor_execute")
        def count_statements(_connection, _cursor, _statement, _parameters, _context, _executemany):
            if counter["enabled"]:
                counter["statements"] += 1

        skladbot_result = {
            "status": "synthetic_stub",
            "ready": 0,
            "blocked": 0,
            "already_linked": 0,
            "linked_mismatch": 0,
            "event_id": "",
        }
        with (
            self.SessionLocal() as db,
            patch(
                "backend.app.imports_service.create_skladbot_dry_run_for_import",
                return_value=skladbot_result,
            ),
        ):
            counter["enabled"] = True
            result = create_import(db, payload)
            counter["enabled"] = False

            self.assertEqual(result.orders_created, 1000)
            self.assertEqual(result.items_created, 1000)
            self.assertEqual(db.scalar(select(func.count()).select_from(Order)), 1000)
            self.assertEqual(db.scalar(select(func.count()).select_from(OrderItem)), 1000)
            self.assertEqual(db.scalar(select(func.count()).select_from(ClientPoint)), 1000)

        self.assertLessEqual(counter["statements"], 30)

    def test_client_point_list_keeps_saved_overlay_filters_and_business_counts(self):
        with self.SessionLocal() as db:
            active = Order(
                client="Overlay Client",
                address="Order Address",
                representative="Needle Representative",
                order_date=date(2026, 7, 9),
                payment_type="cash",
                status="not_completed",
                raw_payload={"coordinates": "41.1, 69.1"},
            )
            returned = Order(
                client="overlay-client",
                address="Latest Order Address",
                representative="Other Representative",
                order_date=date(2026, 7, 10),
                payment_type="cash",
                status="returned",
                raw_payload={"return_status": "returned"},
            )
            derived = Order(
                client="Derived Client",
                address="Derived Address",
                representative="Derived Rep",
                order_date=date(2026, 7, 10),
                payment_type="terminal",
                status="not_completed",
                raw_payload={"coordinates": "42.0, 70.0"},
            )
            saved = ClientPoint(
                client_name="Overlay Client",
                point_name="Needle Point",
                address="Saved Address",
                normalized_client="overlayclient",
                normalized_address="savedaddress",
                coordinates=None,
                representative=None,
                delivery_from="08:30",
                delivery_to="11:45",
                is_active=True,
                raw_payload={},
            )
            db.add_all([active, returned, derived, saved])
            db.commit()

            rows = list_client_points(db)
            custom = list_client_points(db, query="needle", custom_timeslot=True)
            defaults = list_client_points(db, custom_timeslot=False)
            literal_percent = list_client_points(db, query="%")

        self.assertEqual([row["client_name"] for row in rows], ["Overlay Client", "Derived Client"])
        self.assertEqual(len(custom), 1)
        self.assertEqual(custom[0]["source"], "saved")
        self.assertEqual(custom[0]["address"], "Saved Address")
        self.assertEqual(custom[0]["coordinates"], "41.1, 69.1")
        self.assertEqual(custom[0]["representative"], "Needle Representative")
        self.assertEqual(custom[0]["orders_count"], 1)
        self.assertEqual(custom[0]["returned_orders_count"], 1)
        self.assertEqual([row["client_name"] for row in defaults], ["Derived Client"])
        self.assertEqual(literal_percent, [])

    def test_client_point_first_page_query_count_is_constant_at_10x_history(self):
        counter = {"enabled": False, "statements": 0}

        @event.listens_for(self.engine, "before_cursor_execute")
        def count_statements(_connection, _cursor, _statement, _parameters, _context, _executemany):
            if counter["enabled"]:
                counter["statements"] += 1

        def add_history(db, start, stop):
            db.add_all([
                Order(
                    client=f"History Client {index % 5}",
                    address=f"History Address {index % 5}",
                    representative="Synthetic Rep",
                    order_date=date(2026, 7, 10),
                    payment_type="cash",
                    status="not_completed",
                    raw_payload={"coordinates": "0.0, 0.0"},
                )
                for index in range(start, stop)
            ])
            db.commit()

        with self.SessionLocal() as db:
            add_history(db, 0, 10)
            counter["enabled"] = True
            first_page = list_client_points(db, limit=5)
            counter["enabled"] = False
            small_count = counter["statements"]

            add_history(db, 10, 100)
            counter["statements"] = 0
            counter["enabled"] = True
            larger_page = list_client_points(db, limit=5)
            counter["enabled"] = False
            large_count = counter["statements"]

        self.assertEqual(len(first_page), 5)
        self.assertEqual(len(larger_page), 5)
        self.assertEqual(small_count, 1)
        self.assertEqual(large_count, small_count)


class ImportSmartupOrderIdentityTests(unittest.TestCase):
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

    def template_row(self, smartup_order_id, product, import_id):
        return {
            "Дата отгрузки": "13.08.2026",
            "Тип оплаты": "Перечисление",
            "Клиент": "JASUR-DIYOR UNIVERSAL XK",
            "Адрес": "Ташкент, Чиланзар 10",
            "Координаты": "41.296549, 69.277177",
            "Торговый представитель": "ТП5",
            "Товары": product,
            "Кол-во ШТ": 10,
            "Кол-во блок": 1,
            "ID импорта": import_id,
            # Синтетический хеш заказа, как его считает excel_importer:
            # один и тот же для всех строк одного клиента, даты, оплаты и адреса
            "ID заказа": "c" * 64,
            "Smartup ИД заказа": smartup_order_id,
        }

    def run_import(self, rows, filename):
        skladbot_result = {
            "status": "synthetic_stub",
            "ready": 0,
            "blocked": 0,
            "already_linked": 0,
            "linked_mismatch": 0,
            "event_id": "",
        }
        with (
            self.SessionLocal() as db,
            patch(
                "backend.app.imports_service.create_skladbot_dry_run_for_import",
                return_value=skladbot_result,
            ),
        ):
            return create_import(db, ImportCreate(source="telegram", filename=filename, rows=rows))

    def test_normalize_smartup_order_id_accepts_deal_and_rejects_noise(self):
        self.assertEqual(normalize_smartup_order_id("266627707"), "266627707")
        self.assertEqual(normalize_smartup_order_id(266627707), "266627707")
        self.assertEqual(normalize_smartup_order_id(266627707.0), "266627707")
        self.assertEqual(normalize_smartup_order_id(" 266627707 "), "266627707")
        self.assertEqual(normalize_smartup_order_id(""), "")
        self.assertEqual(normalize_smartup_order_id("0"), "")
        self.assertEqual(normalize_smartup_order_id("a" * 64), "")
        self.assertEqual(normalize_smartup_order_id("WH-R-2026-0001"), "")

    def test_template_order_id_becomes_smartup_identity_without_changing_grouping(self):
        self.run_import(
            [
                self.template_row("266627707", "Chapman Brown OP 20", "row-1"),
                self.template_row("266627707", "Chapman Green OP 20", "row-2"),
            ],
            "template.xlsx",
        )

        with self.SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Order)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(OrderItem)), 2)
            order = db.execute(select(Order)).scalar_one()
            self.assertEqual(order.raw_payload["source_order_id"], "smartup:266627707")
            for item in db.execute(select(OrderItem)).scalars():
                self.assertEqual(item.raw_payload["smartup_order_ids"], ["266627707"])
                self.assertEqual(len(item.raw_payload["source_order_id"]), 64)

    def test_second_deal_merged_into_one_position_keeps_both_identifiers(self):
        self.run_import(
            [
                self.template_row("266627707", "Chapman Brown OP 20", "row-1"),
                self.template_row("266968926", "Chapman Brown OP 20", "row-2"),
            ],
            "two-deals.xlsx",
        )

        with self.SessionLocal() as db:
            item = db.execute(select(OrderItem)).scalar_one()
            self.assertEqual(item.quantity_blocks, 2)
            self.assertEqual(
                sorted(item.raw_payload["smartup_order_ids"]),
                ["266627707", "266968926"],
            )
            points = list_client_points(db, query="266968926")
            self.assertEqual([point["client_name"] for point in points], ["JASUR-DIYOR UNIVERSAL XK"])
            self.assertIn("266968926", points[0]["search_identifiers"].split())

    def test_row_without_template_order_id_keeps_synthetic_identity(self):
        self.run_import([self.template_row("", "Chapman RED OP 20", "row-1")], "no-order-id.xlsx")

        with self.SessionLocal() as db:
            order = db.execute(select(Order)).scalar_one()
            self.assertEqual(len(order.raw_payload["source_order_id"]), 64)
            item = db.execute(select(OrderItem)).scalar_one()
            self.assertEqual(item.raw_payload["smartup_order_ids"], [])

    def test_logistics_external_id_still_uses_synthetic_hash_not_smartup_source(self):
        self.run_import(
            [self.template_row("266627707", "Chapman Brown OP 20", "row-1")],
            "logistics.xlsx",
        )

        with self.SessionLocal() as db:
            order = db.execute(
                select(Order).options(selectinload(Order.items))
            ).scalar_one()
            self.assertEqual(logistics_external_id(order), "c" * 64)
            self.assertEqual(logistics_external_id(order, order.items[0]), "c" * 64)

    def test_repeat_import_fills_empty_identity_and_never_overwrites_it(self):
        self.run_import([self.template_row("", "Chapman RED OP 20", "row-1")], "first.xlsx")
        self.run_import([self.template_row("266627707", "Chapman RED OP 20", "row-2")], "second.xlsx")
        self.run_import([self.template_row("266968926", "Chapman RED OP 20", "row-3")], "third.xlsx")

        with self.SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Order)), 1)
            order = db.execute(select(Order)).scalar_one()
            self.assertEqual(order.raw_payload["source_order_id"], "smartup:266627707")


class ClientPointSearchIdentifierTests(unittest.TestCase):
    def test_canonical_identifiers_keep_known_shapes_and_drop_import_hashes(self):
        raw = " ".join([
            "smartup:266627707",
            "WH-R-2026-0001",
            "1002",
            "WR-RET-1",
            "9f2c1b" + "0" * 58,
            '["266968926", "267807389"]',
            "",
        ])

        self.assertEqual(
            canonical_search_identifiers(raw),
            "266627707 WH-R-2026-0001 1002 WR-RET-1 266968926 267807389",
        )

    def test_canonical_identifiers_deduplicate_and_survive_empty_input(self):
        self.assertEqual(canonical_search_identifiers(None), "")
        self.assertEqual(canonical_search_identifiers("smartup:731 731 smartup:731"), "731")


if __name__ == "__main__":
    unittest.main()
