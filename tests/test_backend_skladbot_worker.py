import unittest
from datetime import date, datetime, timezone
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import AuditLog, Base, Order, OrderItem
from backend.app.skladbot_worker import (
    address_soft_match,
    business_today,
    client_matches,
    dynamic_skladbot_lookback_days,
    fetch_candidate_requests,
    nearest_request_diagnostics,
    order_has_skladbot_number,
    parse_date,
    product_matches,
    request_created_recently,
    request_match_diagnostics,
    request_matches_order,
    request_type_matches,
    update_orders_from_skladbot,
    worker_interval_seconds,
)


class BackendSkladBotWorkerTests(unittest.TestCase):
    def test_request_type_matches_only_outgoing_3pl(self):
        self.assertTrue(request_type_matches("3PL отгрузка"))
        self.assertTrue(request_type_matches("Отгрузка 3PL"))
        self.assertFalse(request_type_matches("Возврат 3PL"))
        self.assertFalse(request_type_matches("Возврат 3PL отгрузка"))

    def test_address_soft_match_is_diagnostic_only(self):
        self.assertTrue(address_soft_match("Tashkent, Chilanzar 10", "Uzbekistan, Tashkent, Chilanzar 10"))
        self.assertFalse(address_soft_match("Tashkent, Chilanzar 10", "Samarkand, Registan"))

    def test_client_match_ignores_quotes_case_company_form_and_warehouse_suffix(self):
        self.assertTrue(client_matches('"TABACHNAYA LAVKA" MCHJ', '"Tabachnaya Lavka" MCHJ (склади)'))

    def test_product_match_accepts_concatenated_vendor_code(self):
        self.assertTrue(product_matches("Chapman Brown OP 20", "CHPMBrownOP20UZ"))
        self.assertTrue(product_matches("Chapman Gold SSL 100`20", "CHPMGoldSSL20UZ"))
        self.assertFalse(product_matches("Chapman Brown OP 20", "CHPMRedOP20UZ"))

    def test_request_matches_order_by_date_payment_client_products_and_blocks(self):
        order = Order(
            order_date=date(2026, 5, 29),
            payment_type="Терминал",
            client='"TABACHNAYA LAVKA" MCHJ',
            address="Адрес может отличаться",
            representative="Rep",
            status="not_completed",
            raw_payload={},
        )
        order.items = [
            OrderItem(
                product="Chapman Brown OP 20",
                quantity_pieces=200,
                quantity_blocks=20,
                pieces_per_block=10,
                status="not_completed",
                raw_payload={},
            ),
        ]
        request = {
            "unloading_date": "29.05.2026",
            "recipient": '"TABACHNAYA LAVKA" MCHJ (склади)',
            "comment": "ТЕРМИНАЛ",
            "address": "Другой адрес",
            "products": [
                {
                    "name": "Chapman Brown OP 20 UZ - KingSize",
                    "vendor_code": "CHPMBrownOP20UZ",
                    "barcode": "4006396053978",
                    "amount": 20,
                },
            ],
        }

        self.assertTrue(request_matches_order(order, request))

    def test_request_matches_when_skladbot_contains_extra_products(self):
        order = Order(
            order_date=date(2026, 5, 29),
            payment_type="Терминал",
            client='"TABACHNAYA LAVKA" MCHJ',
            address="Адрес может отличаться",
            representative="Rep",
            status="not_completed",
            raw_payload={},
        )
        order.items = [
            OrderItem(
                product="Chapman Brown OP 20",
                quantity_pieces=200,
                quantity_blocks=20,
                pieces_per_block=10,
                status="not_completed",
                raw_payload={},
            ),
        ]
        request = {
            "unloading_date": "29.05.2026",
            "recipient": '"TABACHNAYA LAVKA" MCHJ (склади)',
            "comment": "Терминал",
            "products": [
                {"name": "Chapman Brown OP 20 UZ", "vendor_code": "CHPMBrownOP20UZ", "amount": 20},
                {"name": "Chapman Gold SSL 20 UZ", "vendor_code": "CHPMGoldSSL20UZ", "amount": 3},
            ],
        }

        diagnostic = request_match_diagnostics(order, request)

        self.assertTrue(diagnostic["matched"])
        self.assertTrue(diagnostic["checks"]["products"])
        self.assertFalse(diagnostic["address_soft_match"])
        self.assertEqual(diagnostic["extra_request_products"], 1)

    def test_request_without_order_products_does_not_match(self):
        order = Order(
            order_date=date(2026, 5, 29),
            payment_type="Терминал",
            client='"TABACHNAYA LAVKA" MCHJ',
            address="Address",
            representative="Rep",
            status="not_completed",
            raw_payload={},
        )
        order.items = []
        request = {
            "unloading_date": "29.05.2026",
            "recipient": '"TABACHNAYA LAVKA" MCHJ',
            "comment": "Терминал",
            "products": [
                {"name": "Chapman Brown OP 20 UZ", "amount": 20},
            ],
        }

        self.assertFalse(request_matches_order(order, request))

    def test_request_match_diagnostics_explains_failed_checks(self):
        order = Order(
            order_date=date(2026, 5, 29),
            payment_type="Терминал",
            client='"TABACHNAYA LAVKA" MCHJ',
            address="Address",
            representative="Rep",
            status="not_completed",
            raw_payload={},
        )
        order.items = [
            OrderItem(
                product="Chapman Brown OP 20",
                quantity_blocks=20,
                status="not_completed",
                raw_payload={},
            ),
        ]
        request = {
            "unloading_date": "30.05.2026",
            "recipient": '"TABACHNAYA LAVKA" MCHJ',
            "comment": "Терминал",
            "products": [
                {
                    "name": "Chapman Brown OP 20 UZ",
                    "amount": 20,
                },
            ],
        }

        diagnostic = request_match_diagnostics(order, request)

        self.assertFalse(diagnostic["matched"])
        self.assertFalse(diagnostic["checks"]["date"])
        self.assertTrue(diagnostic["checks"]["client"])
        self.assertTrue(diagnostic["checks"]["payment"])
        self.assertTrue(diagnostic["checks"]["products"])

    def test_nearest_request_diagnostics_lists_failed_checks(self):
        order = Order(
            order_date=date(2026, 5, 29),
            payment_type="Перечисление",
            client='"TABACHNAYA LAVKA" MCHJ',
            address="Address",
            representative="Rep",
            status="not_completed",
            raw_payload={},
        )
        order.items = [
            OrderItem(
                product="Chapman Brown OP 20",
                quantity_blocks=2,
                status="not_completed",
                raw_payload={},
            ),
        ]
        requests = [
            {
                "id": 1,
                "number": "WH-R-1",
                "unloading_date": "29.05.2026",
                "recipient": '"TABACHNAYA LAVKA" MCHJ',
                "comment": "Перечисление",
                "products": [{"name": "Chapman Brown OP 20 UZ", "amount": 1}],
            }
        ]

        nearest = nearest_request_diagnostics(order, requests)

        self.assertEqual(nearest[0]["number"], "WH-R-1")
        self.assertEqual(nearest[0]["failed_checks"], ["products"])
        self.assertFalse(nearest[0]["products"][0]["matched"])

    def test_update_orders_exports_skladbot_numbers_to_google_sheets(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        try:
            with SessionLocal() as db:
                order = Order(
                    order_date=date(2026, 5, 29),
                    payment_type="Перечисление",
                    client='"TABACHNAYA LAVKA" MCHJ',
                    address="Address",
                    representative="Rep",
                    status="not_completed",
                    raw_payload={},
                )
                order.items = [
                    OrderItem(
                        product="Chapman Brown OP 20",
                        quantity_blocks=2,
                        quantity_pieces=20,
                        pieces_per_block=10,
                        scanned_blocks=0,
                        status="not_completed",
                        raw_payload={"source_import_id": "import-1", "source_order_id": "order-1"},
                    )
                ]
                db.add(order)
                db.commit()

            request = {
                "id": 191794,
                "number": "WH-R-191794",
                "unloading_date": "29.05.2026",
                "recipient": '"TABACHNAYA LAVKA" MCHJ',
                "comment": "Перечисление",
                "products": [{"name": "Chapman Brown OP 20 UZ", "amount": 2}],
                "raw": {},
            }
            with mock.patch("backend.app.skladbot_worker.SessionLocal", SessionLocal), mock.patch(
                "backend.app.skladbot_worker.fetch_candidate_requests",
                return_value=[request],
            ), mock.patch(
                "backend.app.skladbot_worker.sync_backend_orders_skladbot_to_google_sheets",
                return_value={"status": "completed", "updated": 1},
            ) as google_export:
                result = update_orders_from_skladbot()

            self.assertEqual(result["matched"], 1)
            google_export.assert_called_once()
            with SessionLocal() as db:
                order = db.execute(select(Order)).scalar_one()
                self.assertEqual(order.raw_payload["skladbot_request_number"], "WH-R-191794")
                self.assertEqual(order.raw_payload["skladbot_request_id"], "191794")
                audit = db.execute(
                    select(AuditLog).where(AuditLog.action == "skladbot_google_sheets_export")
                ).scalar_one()
                self.assertEqual(audit.payload["status"], "completed")
        finally:
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_candidate_window_uses_created_date_not_future_unloading_date(self):
        request = {
            "created_at": "2026-05-31 10:00:00",
            "updated_at": "",
            "unloading_date": "02.06.2026",
        }

        self.assertTrue(request_created_recently(request, today=date(2026, 5, 31), lookback_days=1))

    def test_business_today_uses_configured_sklad_timezone(self):
        with mock.patch.dict("os.environ", {"TAKSKLAD_TIMEZONE": "Asia/Tashkent"}):
            self.assertEqual(
                business_today(datetime(2026, 5, 31, 20, 30, tzinfo=timezone.utc)),
                date(2026, 6, 1),
            )

    def test_skladbot_timestamp_dates_use_business_timezone(self):
        with mock.patch.dict("os.environ", {"TAKSKLAD_TIMEZONE": "Asia/Tashkent"}):
            self.assertEqual(parse_date("2026-05-31T20:30:00+00:00"), date(2026, 6, 1))
            self.assertEqual(parse_date("2026-05-31 20:30:00+00:00"), date(2026, 6, 1))
            self.assertEqual(parse_date("31.05.2026 20:30:00+0000"), date(2026, 6, 1))
            self.assertEqual(parse_date("31.05.2026 20:30:00"), date(2026, 5, 31))
            self.assertTrue(
                request_created_recently(
                    {"created_at": "31.05.2026 20:30:00+0000", "updated_at": ""},
                    today=date(2026, 6, 1),
                    lookback_days=0,
                )
            )

    def test_candidate_window_rejects_old_created_request(self):
        request = {
            "created_at": "2026-05-20",
            "updated_at": "",
            "unloading_date": "31.05.2026",
        }

        self.assertFalse(request_created_recently(request, today=date(2026, 5, 31), lookback_days=1))

    def test_candidate_window_rejects_request_without_created_or_updated_date(self):
        request = {
            "created_at": "",
            "updated_at": "",
            "unloading_date": "31.05.2026",
        }

        self.assertFalse(request_created_recently(request, today=date(2026, 5, 31), lookback_days=1))

    def test_fetch_candidates_skips_old_list_items_before_detail_fetch(self):
        class FakeClient:
            configured = True
            request_delay = 0

            def __init__(self):
                self.detail_ids = []

            def list_requests(self):
                return [
                    {"id": 1, "type": "Отгрузка 3PL", "created_at": "2026-05-20", "delivery_number": "OLD"},
                    {"id": 2, "type": "Отгрузка 3PL", "created_at": "2026-05-31", "delivery_number": "NEW"},
                ]

            def get_request_detail(self, request_id):
                self.detail_ids.append(request_id)
                return {
                    "id": request_id,
                    "type": "Отгрузка 3PL",
                    "fields": [
                        {"name": "Дата выгрузки", "value": "29.05.2026"},
                        {"name": "Название компании/Имя человека", "value": "Client"},
                    ],
                }

        fake_client = FakeClient()
        with mock.patch("backend.app.skladbot_worker.SkladBotClient", return_value=fake_client):
            result = fetch_candidate_requests(today=date(2026, 5, 31))

        self.assertEqual(fake_client.detail_ids, [2])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["number"], "NEW")

    def test_dynamic_lookback_expands_for_older_active_orders_with_cap(self):
        orders = [
            Order(order_date=date(2026, 5, 29), raw_payload={}),
        ]

        with mock.patch.dict("os.environ", {
            "SKLADBOT_SYNC_LOOKBACK_DAYS": "1",
            "SKLADBOT_SYNC_MAX_LOOKBACK_DAYS": "7",
            "SKLADBOT_ORDER_CREATE_LEAD_DAYS": "3",
        }):
            self.assertEqual(
                dynamic_skladbot_lookback_days(
                    orders=orders,
                    today=date(2026, 6, 1),
                ),
                6,
            )

    def test_fetch_candidates_uses_dynamic_lookback_for_active_order_date(self):
        class FakeClient:
            configured = True
            request_delay = 0

            def __init__(self):
                self.detail_ids = []

            def list_requests(self):
                return [
                    {"id": 1, "type": "Отгрузка 3PL", "created_at": "2026-05-26", "delivery_number": "WH-R-1"},
                ]

            def get_request_detail(self, request_id):
                self.detail_ids.append(request_id)
                return {
                    "id": request_id,
                    "delivery_number": "WH-R-1",
                    "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                    "type": "Отгрузка 3PL",
                    "createdAt": "2026-05-26",
                    "comment": "Перечисление",
                    "fields": [
                        {"name": "Дата выгрузки", "value": "29.05.2026"},
                        {"name": "Название компании/Имя человека", "value": '"TABACHNAYA LAVKA" MCHJ'},
                    ],
                    "products": [
                        {"name": "Chapman Brown OP 20 UZ", "amount": 2},
                    ],
                }

        order = Order(
            order_date=date(2026, 5, 29),
            payment_type="Перечисление",
            client='"TABACHNAYA LAVKA" MCHJ',
            address="Address",
            status="not_completed",
            raw_payload={},
        )
        order.items = [
            OrderItem(product="Chapman Brown OP 20", quantity_blocks=2),
        ]
        fake_client = FakeClient()

        with mock.patch.dict("os.environ", {
            "SKLADBOT_SYNC_LOOKBACK_DAYS": "1",
            "SKLADBOT_SYNC_MAX_LOOKBACK_DAYS": "7",
            "SKLADBOT_DETAIL_LIMIT": "30",
        }):
            result = fetch_candidate_requests(today=date(2026, 6, 1), orders=[order], client=fake_client)

        self.assertEqual(fake_client.detail_ids, [1])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["number"], "WH-R-1")

    def test_fetch_candidates_stops_after_all_active_orders_matched(self):
        class FakeClient:
            configured = True
            request_delay = 0

            def __init__(self):
                self.detail_ids = []

            def list_requests(self):
                return [
                    {"id": 1, "type": "Отгрузка 3PL", "created_at": "2026-05-29", "delivery_number": "WH-R-1"},
                    {"id": 2, "type": "Отгрузка 3PL", "created_at": "2026-05-29", "delivery_number": "WH-R-2"},
                    {"id": 3, "type": "Отгрузка 3PL", "created_at": "2026-05-29", "delivery_number": "WH-R-3"},
                ]

            def get_request_detail(self, request_id):
                self.detail_ids.append(request_id)
                recipient = '"FIRST CLIENT" MCHJ' if request_id == 1 else '"SECOND CLIENT" MCHJ'
                return {
                    "id": request_id,
                    "delivery_number": f"WH-R-{request_id}",
                    "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                    "type": "Отгрузка 3PL",
                    "createdAt": "2026-05-29",
                    "comment": "Перечисление",
                    "fields": [
                        {"name": "Дата выгрузки", "value": "29.05.2026"},
                        {"name": "Название компании/Имя человека", "value": recipient},
                    ],
                    "products": [
                        {"name": "Chapman Brown OP 20 UZ", "amount": 1},
                    ],
                }

        first = Order(order_date=date(2026, 5, 29), payment_type="Перечисление", client='"FIRST CLIENT" MCHJ', raw_payload={})
        second = Order(order_date=date(2026, 5, 29), payment_type="Перечисление", client='"SECOND CLIENT" MCHJ', raw_payload={})
        first.items = [OrderItem(product="Chapman Brown OP 20", quantity_blocks=1)]
        second.items = [OrderItem(product="Chapman Brown OP 20", quantity_blocks=1)]
        fake_client = FakeClient()

        result = fetch_candidate_requests(today=date(2026, 5, 29), orders=[first, second], client=fake_client)

        self.assertEqual(fake_client.detail_ids, [1, 2])
        self.assertEqual([item["number"] for item in result], ["WH-R-1", "WH-R-2"])

    def test_order_has_skladbot_number_accepts_number_or_id(self):
        self.assertFalse(order_has_skladbot_number(Order(raw_payload={})))
        self.assertTrue(order_has_skladbot_number(Order(raw_payload={"skladbot_request_number": "WH-R-1"})))
        self.assertTrue(order_has_skladbot_number(Order(raw_payload={"skladbot_request_id": "191794"})))

    def test_worker_default_interval_is_fast_but_not_below_one_minute(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(worker_interval_seconds(), 60)

        with mock.patch.dict("os.environ", {"SKLADBOT_WORKER_INTERVAL_SECONDS": "10"}, clear=True):
            self.assertEqual(worker_interval_seconds(), 60)

        with mock.patch.dict("os.environ", {"SKLADBOT_WORKER_INTERVAL_SECONDS": "120"}, clear=True):
            self.assertEqual(worker_interval_seconds(), 120)


if __name__ == "__main__":
    unittest.main()
