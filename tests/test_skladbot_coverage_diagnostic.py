import unittest
from datetime import date
from types import SimpleNamespace

from backend.app.skladbot_coverage_diagnostic import verify_skladbot_coverage


def make_order(**overrides):
    order = SimpleNamespace(
        id="order-1",
        order_date=date(2026, 5, 31),
        payment_type="Перечисление",
        client="Client One",
        external_id="",
        status="not_completed",
        raw_payload={
            "skladbot_request_number": "WH-R-100",
            "skladbot_request_id": "100",
            "skladbot_status": "found",
            "skladbot_checked_at": "2026-06-01T10:00:00+00:00",
        },
        items=[
            SimpleNamespace(status="not_completed"),
        ],
    )
    for key, value in overrides.items():
        setattr(order, key, value)
    return order


class SkladBotCoverageDiagnosticTests(unittest.TestCase):
    def test_ok_when_all_active_visible_orders_have_skladbot_numbers(self):
        result = verify_skladbot_coverage([make_order()])

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["active_orders"], 1)
        self.assertEqual(result["numbered_orders"], 1)
        self.assertEqual(result["missing_orders"], 0)

    def test_fails_when_active_order_has_no_skladbot_number(self):
        order = make_order(raw_payload={"skladbot_status": "not_found"})

        result = verify_skladbot_coverage([order])

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["missing_orders"], 1)
        self.assertEqual(result["missing_statuses"], {"not_found": 1})
        self.assertEqual(result["missing_details"][0]["client"], "Client One")

    def test_ignores_order_with_only_removed_google_items(self):
        order = make_order(items=[SimpleNamespace(status="removed_from_google_sheet")], raw_payload={})

        result = verify_skladbot_coverage([order])

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["active_orders"], 0)
        self.assertEqual(result["missing_orders"], 0)


if __name__ == "__main__":
    unittest.main()
