import unittest
from datetime import date
from types import SimpleNamespace

from backend.app.google_backend_sync_diagnostic import verify_google_backend_sync


def make_order(**overrides):
    order = SimpleNamespace(
        id="order-db-1",
        order_date=date(2026, 5, 31),
        payment_type="Перечисление",
        client="Client One",
        address="Ташкент, улица Сакичмон, 10C",
        representative="Rep One",
        status="not_completed",
        raw_payload={
            "skladbot_request_number": "SB-100",
            "skladbot_request_id": "100",
            "skladbot_status": "found",
        },
        items=[],
    )
    for key, value in overrides.items():
        setattr(order, key, value)
    return order


def make_item(order, **overrides):
    item = SimpleNamespace(
        id="item-db-1",
        order=order,
        product="Chapman RED OP 20",
        quantity_pieces=10,
        quantity_blocks=1,
        scanned_blocks=0,
        status="not_completed",
        raw_payload={
            "source_import_id": "import-1",
            "source_order_id": "order-1",
            "block_price": 240000,
            "line_total": 240000,
            "calculated_line_total": 240000,
        },
        scan_codes=[],
    )
    for key, value in overrides.items():
        setattr(item, key, value)
    order.items.append(item)
    return item


def make_record(**overrides):
    record = {
        "row_number": 2,
        "source_import_id": "import-1",
        "source_order_id": "order-1",
        "order_date": date(2026, 5, 31),
        "payment_type": "Перечисление",
        "client": "Client One",
        "address": "Ташкент, улица Сакичмон, 10C",
        "representative": "Rep One",
        "product": "Chapman RED OP 20",
        "quantity_pieces": 10,
        "quantity_blocks": 1,
        "scanned_codes": [],
        "skladbot_request_number": "SB-100",
        "skladbot_request_id": "100",
        "skladbot_status": "Найдено",
    }
    record.update(overrides)
    return record


class GoogleBackendSyncDiagnosticTests(unittest.TestCase):
    def test_ok_when_google_sheet_and_backend_active_item_match(self):
        order = make_order()
        make_item(order)

        result = verify_google_backend_sync([make_record()], [order])

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["google_rows"], 1)
        self.assertEqual(result["backend_active_items"], 1)
        self.assertEqual(result["matched_items"], 1)
        self.assertEqual(result["field_mismatch_count"], 0)

    def test_fails_when_backend_quantity_lags_google_sheet(self):
        order = make_order()
        make_item(order, quantity_blocks=15, quantity_pieces=150)

        result = verify_google_backend_sync([make_record(quantity_blocks=1, quantity_pieces=10)], [order])

        self.assertEqual(result["status"], "failed")
        fields = {item["field"] for item in result["field_mismatches"]}
        self.assertIn("quantity_blocks", fields)
        self.assertIn("quantity_pieces", fields)

    def test_fails_when_backend_line_total_is_stale_after_quantity_change(self):
        order = make_order()
        make_item(order, quantity_blocks=1, quantity_pieces=10, raw_payload={
            "source_import_id": "import-1",
            "source_order_id": "order-1",
            "block_price": 240000,
            "line_total": 3600000,
            "calculated_line_total": 3600000,
        })

        result = verify_google_backend_sync([make_record(quantity_blocks=1, quantity_pieces=10)], [order])

        self.assertEqual(result["status"], "failed")
        fields = {item["field"] for item in result["field_mismatches"]}
        self.assertIn("line_total", fields)
        self.assertIn("calculated_line_total", fields)

    def test_fails_when_skladbot_number_exists_in_backend_but_not_google_sheet(self):
        order = make_order()
        make_item(order)

        result = verify_google_backend_sync([make_record(skladbot_request_number="", skladbot_request_id="")], [order])

        self.assertEqual(result["status"], "failed")
        fields = {item["field"] for item in result["field_mismatches"]}
        self.assertIn("skladbot_request_number", fields)
        self.assertIn("skladbot_request_id", fields)

    def test_fails_when_google_sheet_row_has_no_matching_backend_item(self):
        result = verify_google_backend_sync([make_record()], [])

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["sheet_missing_backend_count"], 1)

    def test_fails_when_backend_active_item_has_no_google_sheet_row(self):
        order = make_order()
        make_item(order)

        result = verify_google_backend_sync([], [order])

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["backend_missing_sheet_count"], 1)


if __name__ == "__main__":
    unittest.main()
