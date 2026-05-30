import unittest
from datetime import date

from backend.app.models import Order, OrderItem
from backend.app.skladbot_worker import client_matches, request_matches_order, request_type_matches


class BackendSkladBotWorkerTests(unittest.TestCase):
    def test_request_type_matches_only_outgoing_3pl(self):
        self.assertTrue(request_type_matches("3PL отгрузка"))
        self.assertFalse(request_type_matches("Возврат 3PL"))

    def test_client_match_ignores_quotes_case_company_form_and_warehouse_suffix(self):
        self.assertTrue(client_matches('"TABACHNAYA LAVKA" MCHJ', '"Tabachnaya Lavka" MCHJ (склади)'))

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


if __name__ == "__main__":
    unittest.main()
