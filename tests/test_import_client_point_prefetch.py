import unittest
from datetime import date
from unittest.mock import patch

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.client_points_service import (
    list_client_points,
    prefetch_client_points_for_import,
    sync_client_point_from_import_row_cached,
)
from backend.app.imports_service import create_import
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

        google_result = {
            "status": "synthetic_stub",
            "imported": 0,
            "duplicates": 0,
            "updated": 0,
            "error": "",
        }
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
                "backend.app.imports_service.export_import_records_to_google_sheets",
                return_value=google_result,
            ),
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


if __name__ == "__main__":
    unittest.main()
