import unittest
from types import SimpleNamespace

from tools.google_cutover_audit import summarize


def scan(scan_id, code):
    return SimpleNamespace(id=scan_id, code=code)


def item(item_id, order_id, status="completed", payload=None, scans=None):
    order = SimpleNamespace(id=order_id, status=status, raw_payload=payload or {})
    return SimpleNamespace(id=item_id, order=order, scan_codes=scans or [])


class GoogleCutoverAuditTests(unittest.TestCase):
    def test_reconciled_active_and_returned_records_are_safe(self):
        active_item = item("i1", "o1", scans=[scan("s1", "A")])
        returned_item = item("i2", "o2", status="returned", scans=[scan("s2", "B")])
        result = summarize([
            ({"archived": False, "scanned_codes": ["A"]}, active_item),
            ({"archived": True, "return_status": "Возврат", "scanned_codes": ["B"]}, returned_item),
        ], {"s2"})

        self.assertTrue(result["safe_to_cutover"])
        self.assertEqual(result["blockers"], 0)
        self.assertEqual(result["returned_records"], 1)

    def test_google_only_return_and_active_record_block_cutover(self):
        result = summarize([
            ({"archived": False, "scanned_codes": []}, None),
            ({"archived": True, "return_status": "returned", "scanned_codes": []}, None),
        ], set())

        self.assertFalse(result["safe_to_cutover"])
        self.assertEqual(result["active_missing_backend"], 1)
        self.assertEqual(result["returned_missing_backend"], 1)

    def test_unmarked_return_and_missing_return_movement_block_cutover(self):
        backend_item = item("i1", "o1", scans=[scan("s1", "KIZ")])
        result = summarize([
            ({"archived": True, "return_status": "return", "scanned_codes": ["KIZ"]}, backend_item),
        ], set())

        self.assertFalse(result["safe_to_cutover"])
        self.assertEqual(result["returned_orders_not_marked"], 1)
        self.assertEqual(result["returned_codes_without_return_movement"], 1)

    def test_counts_only_contract_contains_no_operational_values(self):
        result = summarize([
            ({"archived": False, "source_order_id": "SECRET-ORDER", "scanned_codes": ["SECRET-KIZ"]}, None),
        ], set())

        serialized = str(result)
        self.assertNotIn("SECRET-ORDER", serialized)
        self.assertNotIn("SECRET-KIZ", serialized)


if __name__ == "__main__":
    unittest.main()
