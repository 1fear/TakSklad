import unittest
from unittest import mock

from taksklad import backend_client, backend_events
from taksklad.config import SKLADBOT_REQUEST_NUMBER_COLUMN, STATUS_COMPLETED


class BackendBridgeTests(unittest.TestCase):
    def test_backend_orders_convert_to_desktop_rows_with_existing_codes(self):
        rows = backend_client.backend_orders_to_rows([
            {
                "id": "order-1",
                "order_date": "2026-05-30",
                "payment_type": "Терминал",
                "client": "Client",
                "address": "Address",
                "representative": "Rep",
                "status": "not_completed",
                "skladbot_request_number": "WR-100",
                "items": [
                    {
                        "id": "item-1",
                        "product": "Product",
                        "quantity_pieces": 20,
                        "quantity_blocks": 2,
                        "status": "completed",
                        "scan_codes": ["01000000000000000001", "01000000000000000002"],
                    }
                ],
            }
        ])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Дата отгрузки"], "30.05.2026")
        self.assertEqual(rows[0][SKLADBOT_REQUEST_NUMBER_COLUMN], "WR-100")
        self.assertEqual(rows[0]["_backend_order_id"], "order-1")
        self.assertEqual(rows[0]["_backend_order_item_id"], "item-1")
        self.assertEqual(rows[0]["_existing_scanned_codes"], ["01000000000000000001", "01000000000000000002"])
        self.assertEqual(rows[0]["Отсканированные коды"], "01000000000000000001\n01000000000000000002")
        self.assertEqual(rows[0]["Статус"], STATUS_COMPLETED)

    def test_backend_queue_keeps_ambiguous_duplicate_scan_conflict(self):
        pending = [{
            "id": "event-1",
            "type": "scan",
            "payload": {
                "order_item_id": "item-1",
                "code": "01000000000000000001",
                "workstation_id": "pc-1",
            },
        }]
        saved = []

        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "load_pending_backend_events", return_value=pending),
            mock.patch.object(backend_events, "save_pending_backend_events", side_effect=lambda value: saved.append(value)),
            mock.patch.object(
                backend_events,
                "create_scan",
                side_effect=backend_client.BackendApiError(
                    "Backend HTTP 409: Code already scanned in another order item",
                    status_code=409,
                    detail={"message": "Code already scanned in another order item"},
                ),
            ),
        ):
            result = backend_events.sync_pending_backend_events()

        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["remaining"], 1)
        self.assertEqual(saved[0][0]["attempts"], 1)
        self.assertIn("another order item", saved[0][0]["last_error"])

    def test_backend_queue_keeps_retryable_failures(self):
        pending = [{
            "id": "event-1",
            "type": "scan",
            "payload": {
                "order_item_id": "item-1",
                "code": "01000000000000000001",
            },
        }]
        saved = []

        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "load_pending_backend_events", return_value=pending),
            mock.patch.object(backend_events, "save_pending_backend_events", side_effect=lambda value: saved.append(value)),
            mock.patch.object(
                backend_events,
                "create_scan",
                side_effect=backend_client.BackendApiError("timeout"),
            ),
        ):
            result = backend_events.sync_pending_backend_events()

        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["remaining"], 1)
        self.assertEqual(saved[0][0]["attempts"], 1)
        self.assertEqual(saved[0][0]["last_error"], "timeout")

    def test_backend_queue_drops_extra_scan_when_item_already_full(self):
        pending = [{
            "id": "event-1",
            "type": "scan",
            "payload": {
                "order_item_id": "item-1",
                "code": "01000000000000000002",
            },
        }]
        saved = []

        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "load_pending_backend_events", return_value=pending),
            mock.patch.object(backend_events, "save_pending_backend_events", side_effect=lambda value: saved.append(value)),
            mock.patch.object(
                backend_events,
                "create_scan",
                side_effect=backend_client.BackendApiError(
                    "Backend HTTP 409: Order item is already fully scanned",
                    status_code=409,
                    detail="Order item is already fully scanned",
                ),
            ),
        ):
            result = backend_events.sync_pending_backend_events()

        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(saved, [[]])

    def test_backend_queue_drops_non_retryable_complete_not_found(self):
        pending = [{
            "id": "event-1",
            "type": "order_complete",
            "payload": {
                "order_id": "missing-order",
            },
        }]
        saved = []

        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "load_pending_backend_events", return_value=pending),
            mock.patch.object(backend_events, "save_pending_backend_events", side_effect=lambda value: saved.append(value)),
            mock.patch.object(
                backend_events,
                "complete_order",
                side_effect=backend_client.BackendApiError(
                    "Backend HTTP 404: Order not found",
                    status_code=404,
                    detail={"message": "Order not found"},
                ),
            ),
        ):
            result = backend_events.sync_pending_backend_events()

        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(saved, [[]])

    def test_backend_queue_drops_incomplete_order_complete_conflict(self):
        pending = [{
            "id": "event-1",
            "type": "order_complete",
            "payload": {
                "order_id": "order-1",
            },
        }]
        saved = []

        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "load_pending_backend_events", return_value=pending),
            mock.patch.object(backend_events, "save_pending_backend_events", side_effect=lambda value: saved.append(value)),
            mock.patch.object(
                backend_events,
                "complete_order",
                side_effect=backend_client.BackendApiError(
                    "Backend HTTP 409: Order has incomplete required items",
                    status_code=409,
                    detail={"message": "Order has incomplete required items", "items": []},
                ),
            ),
        ):
            result = backend_events.sync_pending_backend_events()

        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(saved, [[]])

    def test_backend_queue_scan_deduplicates_and_exposes_pending_code(self):
        pending = []
        saved = []

        def fake_load():
            return list(pending)

        def fake_save(value):
            saved.append(value)
            pending[:] = value

        order = {"_backend_order_item_id": "item-1"}
        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "load_pending_backend_events", side_effect=fake_load),
            mock.patch.object(backend_events, "save_pending_backend_events", side_effect=fake_save),
        ):
            first_id = backend_events.queue_backend_scan(order, "01000000000000000001", scanned_at="2026-05-31T10:00:00+05:00")
            second_id = backend_events.queue_backend_scan(order, "01000000000000000001", scanned_at="2026-05-31T10:01:00+05:00")
            codes = backend_events.get_pending_backend_codes()

        self.assertEqual(first_id, second_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["type"], "scan")
        self.assertEqual(pending[0]["payload"]["order_item_id"], "item-1")
        self.assertEqual(pending[0]["payload"]["code"], "01000000000000000001")
        self.assertEqual(codes, {"01000000000000000001"})
        self.assertEqual(len(saved), 1)

    def test_backend_queue_remove_pending_scan_on_undo(self):
        event_id = backend_events.make_backend_event_id(
            "scan",
            {"order_item_id": "item-1", "code": "01000000000000000001"},
        )
        pending = [{
            "id": event_id,
            "type": "scan",
            "payload": {
                "order_item_id": "item-1",
                "code": "01000000000000000001",
            },
        }]
        saved = []

        with (
            mock.patch.object(backend_events, "load_pending_backend_events", return_value=pending),
            mock.patch.object(backend_events, "save_pending_backend_events", side_effect=lambda value: saved.append(value)),
        ):
            removed = backend_events.remove_pending_backend_scan(
                {"_backend_order_item_id": "item-1"},
                "01000000000000000001",
            )

        self.assertTrue(removed)
        self.assertEqual(saved, [[]])

    def test_backend_undo_calls_server_when_scan_is_not_pending(self):
        calls = []

        with (
            mock.patch.object(backend_events, "remove_pending_backend_scan", return_value=False),
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(
                backend_events,
                "undo_scan",
                side_effect=lambda order_item_id, code, workstation_id=None, actor="desktop": calls.append(
                    (order_item_id, code, actor)
                ),
            ),
        ):
            backend_events.undo_backend_scan(
                {"_backend_order_item_id": "item-1"},
                "01000000000000000001",
            )

        self.assertEqual(calls, [("item-1", "01000000000000000001", "desktop")])

    def test_backend_queue_syncs_order_complete(self):
        pending = [{
            "id": "event-1",
            "type": "order_complete",
            "payload": {
                "order_id": "order-1",
            },
        }]
        saved = []
        completed = []

        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "load_pending_backend_events", return_value=pending),
            mock.patch.object(backend_events, "save_pending_backend_events", side_effect=lambda value: saved.append(value)),
            mock.patch.object(backend_events, "complete_order", side_effect=lambda order_id: completed.append(order_id)),
        ):
            result = backend_events.sync_pending_backend_events()

        self.assertEqual(completed, ["order-1"])
        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(saved, [[]])

    def test_backend_queue_unknown_event_does_not_block_queue(self):
        pending = [{
            "id": "event-1",
            "type": "unknown",
            "payload": {},
        }]
        saved = []

        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "load_pending_backend_events", return_value=pending),
            mock.patch.object(backend_events, "save_pending_backend_events", side_effect=lambda value: saved.append(value)),
        ):
            result = backend_events.sync_pending_backend_events()

        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(saved, [[]])


if __name__ == "__main__":
    unittest.main()
