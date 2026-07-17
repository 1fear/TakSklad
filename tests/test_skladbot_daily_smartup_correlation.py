import unittest

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import Base, Order, OrderItem
from backend.app.skladbot_daily_report import enrich_smartup_ids_from_orders


class SkladBotDailySmartupCorrelationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    @staticmethod
    def add_order(db, request_id, request_number, smartup_id, *item_smartup_ids):
        order = Order(
            payment_type="Терминал",
            client=f"Synthetic client {smartup_id}",
            address="Synthetic address",
            raw_payload={
                "source_order_id": f"smartup:{smartup_id}",
                "skladbot_request_id": str(request_id),
                "skladbot_request_number": request_number,
            },
        )
        order.items.append(OrderItem(
            product="Synthetic product",
            quantity_pieces=1,
            quantity_blocks=1,
            raw_payload={
                "source_order_id": f"smartup:{item_smartup_ids[0]}"
                if item_smartup_ids
                else "",
            },
        ))
        for item_smartup_id in item_smartup_ids[1:]:
            order.items.append(OrderItem(
                product=f"Synthetic product {item_smartup_id}",
                quantity_pieces=1,
                quantity_blocks=1,
                raw_payload={"source_order_id": f"smartup:{item_smartup_id}"},
            ))
        db.add(order)

    def test_requires_one_unambiguous_exact_id_number_pair(self):
        with self.SessionLocal() as db:
            self.add_order(db, 901, "WH-R-EXACT-1", 731, 732)
            self.add_order(db, 902, "WH-R-OTHER-1", 733)
            self.add_order(db, 903, "WH-R-DUPLICATE-1", 734)
            self.add_order(db, 903, "WH-R-DUPLICATE-1", 735)
            db.commit()
            report = {"requests": [
                {"id": 901, "number": "WH-R-EXACT-1", "smartup_id": "stale"},
                {"id": 999, "number": "WH-R-EXACT-1", "smartup_id": "stale"},
                {"id": 901, "number": "WH-R-WRONG-1", "smartup_id": "stale"},
                {"id": 901, "number": "WH-R-OTHER-1", "smartup_id": "stale"},
                {"id": 903, "number": "WH-R-DUPLICATE-1", "smartup_id": "stale"},
                {"id": 901, "number": "", "smartup_id": "stale"},
                {"id": "", "number": "WH-R-EXACT-1", "smartup_id": "stale"},
                {"id": 999, "number": "WH-R-MISSING-1", "smartup_id": "stale"},
            ]}

            enrich_smartup_ids_from_orders(db, report)

        self.assertEqual(report["requests"][0]["smartup_id"], "731, 732")
        self.assertTrue(all(request["smartup_id"] == "" for request in report["requests"][1:]))

    def test_uses_one_bounded_join_and_excludes_unrelated_history(self):
        with self.SessionLocal() as db:
            self.add_order(db, 901, "WH-R-EXACT-1", 731, 732)
            self.add_order(db, 990, "WH-R-HISTORY-1", 799, 798)
            db.commit()
            report = {"requests": [
                {"id": 901, "number": "WH-R-EXACT-1", "smartup_id": "stale"},
            ]}
            select_statements = []

            def capture_select(_connection, _cursor, statement, parameters, _context, _executemany):
                if statement.lstrip().upper().startswith("SELECT"):
                    select_statements.append((statement, parameters))

            event.listen(self.engine, "before_cursor_execute", capture_select)
            try:
                enrich_smartup_ids_from_orders(db, report)
            finally:
                event.remove(self.engine, "before_cursor_execute", capture_select)

        self.assertEqual(report["requests"][0]["smartup_id"], "731, 732")
        order_selects = [entry for entry in select_statements if "FROM orders" in entry[0]]
        self.assertEqual(len(order_selects), 1)
        normalized_sql = " ".join(order_selects[0][0].split()).upper()
        self.assertIn("LEFT OUTER JOIN ORDER_ITEMS", normalized_sql)
        self.assertIn("TRIM", normalized_sql)
        self.assertIn(" WHERE ", normalized_sql)
        self.assertNotIn("WH-R-HISTORY-1", repr(order_selects[0][1]))
        self.assertNotIn("990", repr(order_selects[0][1]))

    def test_whitespace_duplicate_owner_fails_closed_for_order_and_item_links(self):
        with self.SessionLocal() as db:
            self.add_order(db, "901", "WH-R-EXACT-1", 731)
            self.add_order(db, " 901 ", " WH-R-EXACT-1 ", 732)
            item_link_owner = Order(
                payment_type="Терминал",
                client="Synthetic item link owner",
                address="Synthetic address",
                raw_payload={"source_order_id": "smartup:733"},
            )
            item_link_owner.items.append(OrderItem(
                product="Synthetic product",
                quantity_pieces=1,
                quantity_blocks=1,
                raw_payload={
                    "source_order_id": "smartup:733",
                    "skladbot_request_id": " 901",
                    "skladbot_request_number": "WH-R-EXACT-1 ",
                },
            ))
            db.add(item_link_owner)
            db.commit()
            report = {"requests": [{
                "id": "901",
                "number": "WH-R-EXACT-1",
                "smartup_id": "stale",
            }]}

            enrich_smartup_ids_from_orders(db, report)

        self.assertEqual(report["requests"][0]["smartup_id"], "")

    def test_raw_list_detail_id_evidence_is_strict_before_db_correlation(self):
        with self.SessionLocal() as db:
            self.add_order(db, "7001", "WH-R-7001", 731)
            db.commit()
            report = {"requests": [
                {
                    "id": 7001,
                    "number": "WH-R-7001",
                    "raw": {
                        "list": {"id": "7.001e3", "delivery_number": "WH-R-7001"},
                        "detail": {"id": "7.001e3", "delivery_number": "WH-R-7001"},
                    },
                },
                {
                    "id": 7001,
                    "number": "WH-R-7001",
                    "raw": {
                        "list": {"id": "123", "delivery_number": "WH-R-7001"},
                        "detail": {"id": "7001", "delivery_number": "WH-R-7001"},
                    },
                },
                {
                    "id": 7001,
                    "number": "WH-R-7001",
                    "raw": {
                        "list": {"id": "7001", "delivery_number": "WH-R-7001"},
                        "detail": {"id": "7001.9", "delivery_number": "WH-R-7001"},
                    },
                },
                {
                    "id": 7001,
                    "number": "WH-R-7001",
                    "raw": {
                        "list": {"id": "7001", "delivery_number": "WH-R-7001"},
                        "detail": {"id": "7001", "delivery_number": "WH-R-7001"},
                    },
                },
                {
                    "id": 7001,
                    "number": "WH-R-7001",
                    "raw": {"list": {"id": "7001", "delivery_number": "WH-R-7001"}},
                },
                {
                    "id": 7001,
                    "number": "WH-R-7001",
                    "raw": {"detail": {"id": "7001", "delivery_number": "WH-R-7001"}},
                },
            ]}

            enrich_smartup_ids_from_orders(db, report)

        self.assertEqual(
            [request["smartup_id"] for request in report["requests"]],
            ["", "", "", "731", "731", "731"],
        )

    def test_item_link_loads_all_sibling_smartup_sources_for_only_bounded_owner(self):
        with self.SessionLocal() as db:
            owner = Order(
                payment_type="Терминал",
                client="Synthetic owner",
                address="Synthetic address",
                raw_payload={"source_order_id": "smartup:731"},
            )
            owner.items.extend([
                OrderItem(
                    product="Linked item",
                    quantity_pieces=1,
                    quantity_blocks=1,
                    raw_payload={
                        "source_order_id": "smartup:732",
                        "skladbot_request_id": "901",
                        "skladbot_request_number": "WH-R-ITEM-1",
                    },
                ),
                OrderItem(
                    product="Sibling one",
                    quantity_pieces=1,
                    quantity_blocks=1,
                    raw_payload={"source_order_id": "smartup:733"},
                ),
                OrderItem(
                    product="Sibling two",
                    quantity_pieces=1,
                    quantity_blocks=1,
                    raw_payload={"source_order_id": "smartup:734"},
                ),
            ])
            unrelated = Order(
                payment_type="Терминал",
                client="Unrelated history",
                address="Synthetic address",
                raw_payload={"source_order_id": "smartup:799"},
            )
            unrelated.items.append(OrderItem(
                product="Unrelated item",
                quantity_pieces=1,
                quantity_blocks=1,
                raw_payload={"source_order_id": "smartup:798"},
            ))
            db.add_all([owner, unrelated])
            db.commit()
            report = {"requests": [{"id": "901", "number": "WH-R-ITEM-1"}]}
            statements = []

            def capture_select(_connection, _cursor, statement, parameters, _context, _executemany):
                if statement.lstrip().upper().startswith("SELECT"):
                    statements.append((statement, parameters))

            event.listen(self.engine, "before_cursor_execute", capture_select)
            try:
                enrich_smartup_ids_from_orders(db, report)
            finally:
                event.remove(self.engine, "before_cursor_execute", capture_select)

        self.assertEqual(report["requests"][0]["smartup_id"], "731, 732, 733, 734")
        order_selects = [entry for entry in statements if "FROM orders" in entry[0]]
        self.assertEqual(len(order_selects), 1)
        normalized_sql = " ".join(order_selects[0][0].split()).upper()
        self.assertIn("EXISTS", normalized_sql)
        self.assertIn("LEFT OUTER JOIN ORDER_ITEMS", normalized_sql)
        self.assertNotIn("799", repr(order_selects[0][1]))
        self.assertNotIn("798", repr(order_selects[0][1]))


if __name__ == "__main__":
    unittest.main()
