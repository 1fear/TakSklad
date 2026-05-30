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

    def test_backend_queue_treats_duplicate_scan_as_already_synced(self):
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
                    "Backend HTTP 409: Code already scanned",
                    status_code=409,
                    detail="Code already scanned",
                ),
            ),
        ):
            result = backend_events.sync_pending_backend_events()

        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(saved, [[]])

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


if __name__ == "__main__":
    unittest.main()
