import unittest
from datetime import date
from unittest import mock

from backend.app.models import Order, OrderItem
from backend.app.skladbot_worker import (
    address_soft_match,
    client_matches,
    product_matches,
    request_created_recently,
    request_match_diagnostics,
    request_matches_order,
    request_type_matches,
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

    def test_candidate_window_uses_created_date_not_future_unloading_date(self):
        request = {
            "created_at": "2026-05-31 10:00:00",
            "updated_at": "",
            "unloading_date": "02.06.2026",
        }

        self.assertTrue(request_created_recently(request, today=date(2026, 5, 31), lookback_days=1))

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

    def test_worker_default_interval_is_fast_but_not_below_one_minute(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(worker_interval_seconds(), 60)

        with mock.patch.dict("os.environ", {"SKLADBOT_WORKER_INTERVAL_SECONDS": "10"}, clear=True):
            self.assertEqual(worker_interval_seconds(), 60)

        with mock.patch.dict("os.environ", {"SKLADBOT_WORKER_INTERVAL_SECONDS": "120"}, clear=True):
            self.assertEqual(worker_interval_seconds(), 120)


if __name__ == "__main__":
    unittest.main()
