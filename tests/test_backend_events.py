import unittest

from taksklad import backend_events
from taksklad.backend_client import BackendApiError


class BackendEventQueueTests(unittest.TestCase):
    def setUp(self):
        self.original_backend_configured = backend_events.backend_configured
        self.original_load_pending_backend_events = backend_events.load_pending_backend_events
        self.original_save_pending_backend_events = backend_events.save_pending_backend_events
        self.original_create_scan = backend_events.create_scan
        self.original_complete_order = backend_events.complete_order

    def tearDown(self):
        backend_events.backend_configured = self.original_backend_configured
        backend_events.load_pending_backend_events = self.original_load_pending_backend_events
        backend_events.save_pending_backend_events = self.original_save_pending_backend_events
        backend_events.create_scan = self.original_create_scan
        backend_events.complete_order = self.original_complete_order

    def use_pending_events(self, items):
        state = {"items": list(items)}
        backend_events.backend_configured = lambda: True
        backend_events.load_pending_backend_events = lambda: state["items"]

        def save_pending(items_to_save):
            state["items"] = list(items_to_save)
            return True

        backend_events.save_pending_backend_events = save_pending
        return state

    def test_retryable_scan_failure_stays_pending_with_attempt_and_error(self):
        state = self.use_pending_events([
            {
                "id": "scan-1",
                "type": "scan",
                "payload": {"order_item_id": "item-1", "code": "TEST-CODE-ABC"},
                "attempts": 0,
                "last_error": "",
            }
        ])

        def fail_create_scan(*args, **kwargs):
            raise BackendApiError("temporary timeout")

        backend_events.create_scan = fail_create_scan

        result = backend_events.sync_pending_backend_events()

        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["remaining"], 1)
        self.assertEqual(state["items"][0]["attempts"], 1)
        self.assertIn("temporary timeout", state["items"][0]["last_error"])
        self.assertIn("updated_at", state["items"][0])

    def test_duplicate_scan_ack_removes_pending_event(self):
        state = self.use_pending_events([
            {
                "id": "scan-1",
                "type": "scan",
                "payload": {"order_item_id": "item-1", "code": "TEST-CODE-ABC"},
                "attempts": 0,
                "last_error": "",
            }
        ])

        def duplicate_create_scan(*args, **kwargs):
            raise BackendApiError(
                "Backend HTTP 409: already scanned",
                status_code=409,
                detail="already scanned for this order item",
            )

        backend_events.create_scan = duplicate_create_scan

        result = backend_events.sync_pending_backend_events()

        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(state["items"], [])

    def test_non_retryable_scan_conflict_is_returned_as_blocked_event(self):
        state = self.use_pending_events([
            {
                "id": "scan-1",
                "type": "scan",
                "payload": {"order_item_id": "item-1", "code": "TEST-CODE-ABC"},
                "attempts": 0,
                "last_error": "",
            }
        ])

        def conflict_create_scan(*args, **kwargs):
            raise BackendApiError(
                "Backend HTTP 409: code already scanned for another order item",
                status_code=409,
                detail="code already scanned for another order item",
            )

        backend_events.create_scan = conflict_create_scan

        result = backend_events.sync_pending_backend_events()

        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(len(result["blocked_events"]), 1)
        self.assertEqual(result["blocked_events"][0]["attempts"], 1)
        self.assertIn("409", result["blocked_events"][0]["last_error"])
        self.assertEqual(state["items"], [])


if __name__ == "__main__":
    unittest.main()
