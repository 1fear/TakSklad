import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from taksklad import backend_client, backend_events, backend_flow, storage
from taksklad.config import SKLADBOT_REQUEST_NUMBER_COLUMN, STATUS_COMPLETED


class BackendBridgeTests(unittest.TestCase):
    def setUp(self):
        # Синхронизация очереди пишет заблокированные события в durable-хранилище.
        # Без изоляции тесты трогали бы реальный TakSklad_queues.sqlite3 склада.
        self._temp_dir = tempfile.TemporaryDirectory()
        self._original_data_file = storage.TAKSKLAD_DATA_FILE
        storage.TAKSKLAD_DATA_FILE = str(Path(self._temp_dir.name) / "TakSklad_data.json")

    def tearDown(self):
        storage.TAKSKLAD_DATA_FILE = self._original_data_file
        self._temp_dir.cleanup()
    @staticmethod
    def reconcile_stub(pending, saved):
        def reconcile(_section, _snapshot, remaining):
            saved.append(list(remaining))
            pending[:] = list(remaining)
            return list(pending)

        return reconcile

    def test_backend_duplicate_scan_reuse_status_allows_return_undo_reset(self):
        order = {"_backend_order_item_id": "item-1"}

        for movement in ("return", "undo", "reset"):
            with (
                mock.patch.object(backend_flow, "backend_enabled", return_value=True),
                mock.patch.object(
                    backend_flow,
                    "lookup_kiz_availability",
                    return_value={
                        "available": True,
                        "latest_movement_type": movement,
                        "existing_order_item_id": "old-item",
                    },
                ),
            ):
                status = backend_flow.backend_duplicate_scan_reuse_status(
                    order,
                    "0104006396053978-TEST-REUSEXXXXXXXX",
                )

            self.assertTrue(status["checked"])
            self.assertTrue(status["available"])
            self.assertEqual(status["latest_movement_type"], movement)
            self.assertEqual(status["existing_order_item_id"], "old-item")

    def test_backend_duplicate_scan_reuse_status_blocks_outbound_and_failed_check(self):
        order = {"_backend_order_item_id": "item-1"}

        with (
            mock.patch.object(backend_flow, "backend_enabled", return_value=True),
            mock.patch.object(
                backend_flow,
                "lookup_kiz_availability",
                return_value={
                    "available": False,
                    "latest_movement_type": "outbound",
                    "existing_order_item_id": "busy-item",
                },
            ),
        ):
            busy = backend_flow.backend_duplicate_scan_reuse_status(order, "0104006396053978-TEST-REUSEXXXXXXXX")

        self.assertTrue(busy["checked"])
        self.assertFalse(busy["available"])
        self.assertEqual(busy["latest_movement_type"], "outbound")

        with (
            mock.patch.object(backend_flow, "backend_enabled", return_value=True),
            mock.patch.object(
                backend_flow,
                "lookup_kiz_availability",
                side_effect=backend_client.BackendApiError("unavailable"),
            ),
        ):
            failed = backend_flow.backend_duplicate_scan_reuse_status(order, "0104006396053978-TEST-REUSEXXXXXXXX")

        self.assertFalse(failed["checked"])
        self.assertFalse(failed["available"])
        self.assertIn("failed", failed["reason"])

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
                        "scan_codes": ["01000000000000000001XXXXXXXXXXXXXXX", "01000000000000000002XXXXXXXXXXXXXXX"],
                        "scan_entries": [
                            {"code": "01000000000000000001XXXXXXXXXXXXXXX", "scan_type": "unit", "block_quantity": 1},
                            {"code": "01000000000000000002XXXXXXXXXXXXXXX", "scan_type": "unit", "block_quantity": 1},
                        ],
                    }
                ],
            }
        ])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Дата отгрузки"], "30.05.2026")
        self.assertEqual(rows[0][SKLADBOT_REQUEST_NUMBER_COLUMN], "WR-100")
        self.assertEqual(rows[0]["_backend_order_id"], "order-1")
        self.assertEqual(rows[0]["_backend_order_item_id"], "item-1")
        self.assertEqual(rows[0]["_existing_scanned_codes"], ["01000000000000000001XXXXXXXXXXXXXXX", "01000000000000000002XXXXXXXXXXXXXXX"])
        self.assertEqual(rows[0]["_existing_scan_entries"][0]["block_quantity"], 1)
        self.assertEqual(rows[0]["Отсканированные коды"], "01000000000000000001XXXXXXXXXXXXXXX\n01000000000000000002XXXXXXXXXXXXXXX")
        self.assertEqual(rows[0]["Статус"], STATUS_COMPLETED)

    def test_preview_import_orders_posts_to_preview_endpoint(self):
        records = [{"Клиент": "Client", "_source_file_sha256": ["a" * 64]}]
        with mock.patch.object(backend_client, "backend_request", return_value={"rows_importable": 1}) as request:
            result = backend_client.preview_import_orders(records, filename="orders.xlsx")

        self.assertEqual(result, {"rows_importable": 1})
        request.assert_called_once_with(
            "POST",
            "/api/v1/imports/preview",
            {
                "source": "excel",
                "filename": "orders.xlsx",
                "rows": [{"Клиент": "Client"}],
            },
        )
        self.assertEqual(records[0]["_source_file_sha256"], ["a" * 64])

    def test_backend_queue_drops_non_retryable_duplicate_scan_conflict(self):
        pending = [{
            "id": "event-1",
            "type": "scan",
            "payload": {
                "order_item_id": "item-1",
                "code": "01000000000000000001XXXXXXXXXXXXXXX",
                "workstation_id": "pc-1",
            },
        }]
        saved = []

        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "load_pending_backend_events", return_value=pending),
            mock.patch.object(
                backend_events,
                "reconcile_queue_section",
                side_effect=self.reconcile_stub(pending, saved),
            ),
            mock.patch.object(
                backend_events,
                "create_scan",
                side_effect=backend_client.BackendApiError(
                    "Backend HTTP 409: Code already scanned in another order item",
                    status_code=409,
                    detail={
                        "message": "Code already scanned in another order item",
                        "existing_order": {
                            "client": "OOO Busy Client",
                            "order_date_display": "30.05.2026",
                        },
                    },
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
        self.assertEqual(len(result["blocked_events"]), 1)
        self.assertEqual(result["blocked_events"][0]["payload"]["order_item_id"], "item-1")
        self.assertIn("another order item", result["blocked_events"][0]["last_error"])
        self.assertEqual(
            result["blocked_events"][0]["last_error_detail"]["existing_order"]["client"],
            "OOO Busy Client",
        )

    def test_backend_queue_drops_non_retryable_wrong_sku_scan_conflict(self):
        pending = [{
            "id": "event-1",
            "type": "scan",
            "payload": {
                "order_item_id": "item-1",
                "code": "01000000000000000001XXXXXXXXXXXXXXX",
                "workstation_id": "pc-1",
            },
        }]
        saved = []

        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "load_pending_backend_events", return_value=pending),
            mock.patch.object(
                backend_events,
                "reconcile_queue_section",
                side_effect=self.reconcile_stub(pending, saved),
            ),
            mock.patch.object(
                backend_events,
                "create_scan",
                side_effect=backend_client.BackendApiError(
                    "Backend HTTP 409: Scan product does not match order item",
                    status_code=409,
                    detail={"message": "Scan product does not match order item"},
                ),
            ),
        ):
            result = backend_events.sync_pending_backend_events()

        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(saved, [[]])
        self.assertIn("does not match", result["blocked_events"][0]["last_error"])

    def test_backend_queue_keeps_retryable_failures(self):
        pending = [{
            "id": "event-1",
            "type": "scan",
            "payload": {
                "order_item_id": "item-1",
                "code": "01000000000000000001XXXXXXXXXXXXXXX",
            },
        }]
        saved = []

        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "load_pending_backend_events", return_value=pending),
            mock.patch.object(
                backend_events,
                "reconcile_queue_section",
                side_effect=self.reconcile_stub(pending, saved),
            ),
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

    def test_backend_queue_blocks_extra_scan_when_item_already_full(self):
        pending = [{
            "id": "event-1",
            "type": "scan",
            "payload": {
                "order_item_id": "item-1",
                "code": "01000000000000000002XXXXXXXXXXXXXXX",
            },
        }]
        saved = []

        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "load_pending_backend_events", return_value=pending),
            mock.patch.object(
                backend_events,
                "reconcile_queue_section",
                side_effect=self.reconcile_stub(pending, saved),
            ),
            mock.patch.object(
                backend_events,
                "create_scan",
                side_effect=backend_client.BackendApiError(
                    "Backend HTTP 409: Order item is already fully scanned",
                    status_code=409,
                    detail={
                        "code": "order_item_fully_scanned_new_code",
                        "message": "Order item is already fully scanned",
                        "order_item_id": "item-1",
                        "quantity_blocks": 2,
                        "scanned_blocks": 2,
                    },
                ),
            ),
        ):
            result = backend_events.sync_pending_backend_events()

        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(result["blocked"], 1)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(saved, [[]])
        self.assertEqual(
            result["blocked_events"][0]["last_error_detail"]["code"],
            "order_item_fully_scanned_new_code",
        )

    def test_blocked_scan_survives_sync_even_when_no_position_is_open(self):
        """Заблокированный скан описывает блок, который физически уехал со склада.

        Он намеренно убран из очереди повторов, но обязан пережить процесс:
        иначе при закрытой другим контуром позиции КИЗ исчезает молча.
        """
        pending = [{
            "id": "event-1",
            "type": "scan",
            "payload": {
                "order_item_id": "item-1",
                "code": "01000000000000000002XXXXXXXXXXXXXXX",
            },
        }]
        saved = []
        blocked_store = []

        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "load_pending_backend_events", return_value=pending),
            mock.patch.object(
                backend_events,
                "reconcile_queue_section",
                side_effect=self.reconcile_stub(pending, saved),
            ),
            mock.patch.object(backend_events, "load_blocked_backend_events", side_effect=lambda: list(blocked_store)),
            mock.patch.object(
                backend_events,
                "save_blocked_backend_events",
                side_effect=lambda items: blocked_store.__setitem__(slice(None), list(items)),
            ),
            mock.patch.object(
                backend_events,
                "create_scan",
                side_effect=backend_client.BackendApiError(
                    "Backend HTTP 409: Order item is already fully scanned",
                    status_code=409,
                    detail={
                        "code": "order_item_fully_scanned_new_code",
                        "message": "Order item is already fully scanned",
                        "order_item_id": "item-1",
                        "quantity_blocks": 2,
                        "scanned_blocks": 2,
                    },
                ),
            ),
        ):
            backend_events.sync_pending_backend_events()

        self.assertEqual(len(blocked_store), 1)
        self.assertEqual(blocked_store[0]["payload"]["code"], "01000000000000000002XXXXXXXXXXXXXXX")
        self.assertEqual(blocked_store[0]["payload"]["order_item_id"], "item-1")
        self.assertEqual(
            blocked_store[0]["last_error_detail"]["code"],
            "order_item_fully_scanned_new_code",
        )

    def test_blocked_scan_store_is_idempotent_across_repeated_syncs(self):
        stored = [{
            "id": "event-1",
            "type": "scan",
            "payload": {"order_item_id": "item-1", "code": "01000000000000000002XXXXXXXXXXXXXXX"},
        }]
        saved_calls = []

        with (
            mock.patch.object(backend_events, "load_blocked_backend_events", return_value=list(stored)),
            mock.patch.object(
                backend_events,
                "save_blocked_backend_events",
                side_effect=lambda items: saved_calls.append(list(items)),
            ),
        ):
            added = backend_events.record_blocked_backend_events(list(stored))

        self.assertEqual(added, 0)
        self.assertEqual(saved_calls, [])

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
            mock.patch.object(
                backend_events,
                "reconcile_queue_section",
                side_effect=self.reconcile_stub(pending, saved),
            ),
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
            mock.patch.object(
                backend_events,
                "reconcile_queue_section",
                side_effect=self.reconcile_stub(pending, saved),
            ),
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

        def fake_append(_section, item, **_kwargs):
            if any(existing.get("id") == item.get("id") for existing in pending):
                return False
            pending.append(item)
            saved.append(list(pending))
            return True

        order = {"_backend_order_item_id": "item-1"}
        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "load_pending_backend_events", side_effect=fake_load),
            mock.patch.object(backend_events, "append_queue_item", side_effect=fake_append),
        ):
            first_id = backend_events.queue_backend_scan(order, "01000000000000000001XXXXXXXXXXXXXXX", scanned_at="2026-05-31T10:00:00+05:00")
            second_id = backend_events.queue_backend_scan(order, "01000000000000000001XXXXXXXXXXXXXXX", scanned_at="2026-05-31T10:01:00+05:00")
            codes = backend_events.get_pending_backend_codes()

        self.assertEqual(first_id, second_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["type"], "scan")
        self.assertEqual(pending[0]["payload"]["order_item_id"], "item-1")
        self.assertEqual(pending[0]["payload"]["code"], "01000000000000000001XXXXXXXXXXXXXXX")
        self.assertEqual(codes, {"01000000000000000001XXXXXXXXXXXXXXX"})
        self.assertEqual(len(saved), 1)

    def test_backend_queue_remove_pending_scan_on_undo(self):
        event_id = backend_events.make_backend_event_id(
            "scan",
            {"order_item_id": "item-1", "code": "01000000000000000001XXXXXXXXXXXXXXX"},
        )
        pending = [{
            "id": event_id,
            "type": "scan",
            "payload": {
                "order_item_id": "item-1",
                "code": "01000000000000000001XXXXXXXXXXXXXXX",
            },
        }]
        saved = []

        with (
            mock.patch.object(
                backend_events,
                "mutate_queue_section",
                side_effect=lambda _section, mutator: saved.append(mutator(list(pending))) or saved[-1],
            ),
        ):
            removed = backend_events.remove_pending_backend_scan(
                {"_backend_order_item_id": "item-1"},
                "01000000000000000001XXXXXXXXXXXXXXX",
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
                "01000000000000000001XXXXXXXXXXXXXXX",
            )

        self.assertEqual(calls, [("item-1", "01000000000000000001XXXXXXXXXXXXXXX", "desktop")])

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
            mock.patch.object(
                backend_events,
                "reconcile_queue_section",
                side_effect=self.reconcile_stub(pending, saved),
            ),
            mock.patch.object(backend_events, "complete_order", side_effect=lambda order_id: completed.append(order_id)),
        ):
            result = backend_events.sync_pending_backend_events()

        self.assertEqual(completed, ["order-1"])
        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(saved, [[]])

    def test_remove_pending_backend_order_complete_removes_only_matching_event(self):
        matching = {
            "id": backend_events.make_backend_event_id("order_complete", {"order_id": "order-1"}),
            "type": "order_complete",
            "payload": {"order_id": "order-1"},
        }
        other = {
            "id": backend_events.make_backend_event_id("order_complete", {"order_id": "order-2"}),
            "type": "order_complete",
            "payload": {"order_id": "order-2"},
        }
        saved = []

        def mutate(_section, callback):
            saved.extend(callback([matching, other]))

        with mock.patch.object(backend_events, "mutate_queue_section", side_effect=mutate):
            removed = backend_events.remove_pending_backend_order_complete("order-1")

        self.assertTrue(removed)
        self.assertEqual(saved, [other])

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
            mock.patch.object(
                backend_events,
                "reconcile_queue_section",
                side_effect=self.reconcile_stub(pending, saved),
            ),
        ):
            result = backend_events.sync_pending_backend_events()

        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(saved, [[]])

    def test_filtered_sync_sends_only_matching_position_and_leaves_others_untouched(self):
        order_1 = {"_backend_order_item_id": "item-1"}
        order_2 = {"_backend_order_item_id": "item-2"}
        with mock.patch.object(backend_events, "backend_configured", return_value=True):
            backend_events.queue_backend_scan(order_1, "0104006396053978-TEST-ONEXXXXXXX", scanned_at="2026-05-31T10:00:00+05:00")
            backend_events.queue_backend_scan(order_2, "0104006396053978-TEST-TWOXXXXXXX", scanned_at="2026-05-31T10:00:00+05:00")
            backend_events.queue_backend_order_complete("order-9")

        before = storage.load_data_section("pending_backend_events", [])
        self.assertEqual(len(before), 3)

        create_scan_calls = []
        complete_order_calls = []
        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(
                backend_events,
                "create_scan",
                side_effect=lambda order_item_id, code, **kwargs: create_scan_calls.append((order_item_id, code)),
            ),
            mock.patch.object(
                backend_events,
                "complete_order",
                side_effect=lambda order_id: complete_order_calls.append(order_id),
            ),
        ):
            result = backend_events.sync_pending_backend_events(order_item_ids=["item-1"])

        self.assertEqual(create_scan_calls, [("item-1", "0104006396053978-TEST-ONEXXXXXXX")])
        self.assertEqual(complete_order_calls, [])
        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["failed"], 0)

        after = storage.load_data_section("pending_backend_events", [])
        # Отправленное событие позиции item-1 доставлено и уходит из очереди,
        # остальные два события остаются как были - без изменений attempts и порядка.
        self.assertEqual(len(after), 2)
        before_by_id = {item["id"]: item for item in before if item["payload"].get("order_item_id") != "item-1"}
        after_by_id = {item["id"]: item for item in after}
        self.assertEqual(before_by_id, after_by_id)
        self.assertEqual([item["id"] for item in after], [item["id"] for item in before if item["payload"].get("order_item_id") != "item-1"])

    def test_background_pass_skips_when_lock_is_already_held(self):
        pending = [{
            "id": "event-1",
            "type": "scan",
            "payload": {"order_item_id": "item-1", "code": "01000000000000000001XXXXXXXXXXXXXXX"},
        }]

        create_scan_calls = []
        acquired = backend_events._SYNC_LOCK.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            with (
                mock.patch.object(backend_events, "backend_configured", return_value=True),
                mock.patch.object(backend_events, "load_pending_backend_events", return_value=pending),
                mock.patch.object(
                    backend_events,
                    "create_scan",
                    side_effect=lambda *a, **k: create_scan_calls.append((a, k)),
                ),
            ):
                result = backend_events.sync_pending_backend_events(background=True)
        finally:
            backend_events._SYNC_LOCK.release()

        self.assertEqual(create_scan_calls, [])
        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["dropped"], 0)
        self.assertEqual(result["blocked"], 0)
        self.assertEqual(result["blocked_events"], [])
        self.assertEqual(result["remaining"], 1)
        self.assertTrue(result["skipped"])

    def test_background_pass_stops_before_next_event_when_screen_pass_is_waiting(self):
        order_1 = {"_backend_order_item_id": "item-1"}
        order_2 = {"_backend_order_item_id": "item-2"}
        with mock.patch.object(backend_events, "backend_configured", return_value=True):
            backend_events.queue_backend_scan(order_1, "0104006396053978-TEST-ONEXXXXXXX", scanned_at="2026-05-31T10:00:00+05:00")
            backend_events.queue_backend_scan(order_2, "0104006396053978-TEST-TWOXXXXXXX", scanned_at="2026-05-31T10:00:00+05:00")

        create_scan_calls = []
        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(
                backend_events,
                "create_scan",
                side_effect=lambda order_item_id, code, **kwargs: create_scan_calls.append((order_item_id, code)),
            ),
            # Перед первым событием очереди на экран ещё никто не претендует, перед
            # вторым уже появился ожидающий проход - тот же сигнал, что дал бы реальный
            # поток экрана, но без реальных потоков и sleep.
            mock.patch.object(backend_events, "_foreground_waiters_count", side_effect=[0, 1]),
        ):
            result = backend_events.sync_pending_backend_events(background=True)

        self.assertEqual(create_scan_calls, [("item-1", "0104006396053978-TEST-ONEXXXXXXX")])
        self.assertTrue(result["preempted"])
        self.assertEqual(result["synced"], 1)

        remaining_items = storage.load_data_section("pending_backend_events", [])
        self.assertEqual(len(remaining_items), 1)
        self.assertEqual(remaining_items[0]["payload"]["order_item_id"], "item-2")
        self.assertEqual(remaining_items[0].get("attempts"), 0)

    def assert_sync_primitives_are_idle(self):
        self.assertEqual(backend_events._foreground_waiters_count(), 0)
        acquired = backend_events._SYNC_LOCK.acquire(blocking=False)
        self.assertTrue(acquired, "замок прохода не освобождён")
        backend_events._SYNC_LOCK.release()

    def queue_two_scans(self):
        with mock.patch.object(backend_events, "backend_configured", return_value=True):
            backend_events.queue_backend_scan(
                {"_backend_order_item_id": "item-1"}, "0104006396053978-TEST-ONEXXXXXXX",
                scanned_at="2026-05-31T10:00:00+05:00",
            )
            backend_events.queue_backend_scan(
                {"_backend_order_item_id": "item-2"}, "0104006396053978-TEST-TWOXXXXXXX",
                scanned_at="2026-05-31T10:00:00+05:00",
            )

    def test_sync_pass_leaves_waiters_counter_and_lock_idle_after_normal_run(self):
        self.assert_sync_primitives_are_idle()
        self.queue_two_scans()
        create_scan_calls = []
        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(
                backend_events,
                "create_scan",
                side_effect=lambda order_item_id, code, **kwargs: create_scan_calls.append(order_item_id),
            ),
        ):
            result = backend_events.sync_pending_backend_events()
            self.assert_sync_primitives_are_idle()
            background_result = backend_events.sync_pending_backend_events(background=True)

        self.assertEqual(create_scan_calls, ["item-1", "item-2"])
        self.assertEqual(result["synced"], 2)
        self.assertEqual(background_result["synced"], 0)
        self.assert_sync_primitives_are_idle()

    def test_sync_pass_leaves_waiters_counter_and_lock_idle_after_exception(self):
        self.assert_sync_primitives_are_idle()
        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(
                backend_events, "load_pending_backend_events", side_effect=RuntimeError("queue unreadable")
            ),
        ):
            with self.assertRaises(RuntimeError):
                backend_events.sync_pending_backend_events()
            self.assert_sync_primitives_are_idle()
            with self.assertRaises(RuntimeError):
                backend_events.sync_pending_backend_events(background=True)
        self.assert_sync_primitives_are_idle()

    def test_sync_pass_leaves_primitives_idle_when_filter_argument_is_rejected(self):
        self.assert_sync_primitives_are_idle()
        with mock.patch.object(backend_events, "backend_configured", return_value=True):
            with self.assertRaises(TypeError):
                backend_events.sync_pending_backend_events(order_item_ids="item-1")
        self.assert_sync_primitives_are_idle()

    def test_background_pass_stops_when_real_waiters_counter_is_raised(self):
        self.queue_two_scans()
        before = storage.load_data_section("pending_backend_events", [])
        self.assertEqual(len(before), 2)

        create_scan_calls = []
        backend_events._increment_foreground_waiters()
        try:
            with (
                mock.patch.object(backend_events, "backend_configured", return_value=True),
                mock.patch.object(
                    backend_events,
                    "create_scan",
                    side_effect=lambda *a, **k: create_scan_calls.append((a, k)),
                ),
            ):
                result = backend_events.sync_pending_backend_events(background=True)
            self.assertEqual(backend_events._foreground_waiters_count(), 1)
        finally:
            backend_events._decrement_foreground_waiters()

        self.assertEqual(create_scan_calls, [])
        self.assertTrue(result["preempted"])
        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["remaining"], 2)
        self.assertEqual(storage.load_data_section("pending_backend_events", []), before)
        self.assert_sync_primitives_are_idle()

    def test_screen_pass_counts_as_waiter_while_another_pass_holds_the_lock(self):
        self.queue_two_scans()
        create_scan_calls = []
        result_box = {}

        def screen_pass():
            result_box["result"] = backend_events.sync_pending_backend_events()

        held = backend_events._SYNC_LOCK.acquire(blocking=False)
        self.assertTrue(held)
        try:
            with (
                mock.patch.object(backend_events, "backend_configured", return_value=True),
                mock.patch.object(
                    backend_events,
                    "create_scan",
                    side_effect=lambda order_item_id, code, **kwargs: create_scan_calls.append(order_item_id),
                ),
            ):
                worker = threading.Thread(target=screen_pass, daemon=True)
                worker.start()
                deadline = time.monotonic() + 2
                while backend_events._foreground_waiters_count() != 1 and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(backend_events._foreground_waiters_count(), 1)
                self.assertEqual(create_scan_calls, [])
                self.assertTrue(worker.is_alive())
                backend_events._SYNC_LOCK.release()
                held = False
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
        finally:
            if held:
                backend_events._SYNC_LOCK.release()

        self.assertEqual(create_scan_calls, ["item-1", "item-2"])
        self.assertEqual(result_box["result"]["synced"], 2)
        self.assert_sync_primitives_are_idle()

    def test_background_pass_yields_to_real_screen_thread_that_appears_mid_pass(self):
        self.queue_two_scans()
        create_scan_calls = []
        screen_results = []
        screen_threads = []

        def screen_pass():
            screen_results.append(backend_events.sync_pending_backend_events())

        def fake_create_scan(order_item_id, code, **kwargs):
            create_scan_calls.append(order_item_id)
            if len(create_scan_calls) == 1:
                # Проход с экрана появляется, пока фон занят первым событием.
                worker = threading.Thread(target=screen_pass, daemon=True)
                screen_threads.append(worker)
                worker.start()
                deadline = time.monotonic() + 2
                while backend_events._foreground_waiters_count() != 1 and time.monotonic() < deadline:
                    time.sleep(0.005)

        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "create_scan", side_effect=fake_create_scan),
        ):
            background_result = backend_events.sync_pending_backend_events(background=True)
            for worker in screen_threads:
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())

        self.assertTrue(background_result["preempted"])
        self.assertEqual(background_result["synced"], 1)
        self.assertEqual(create_scan_calls, ["item-1", "item-2"])
        self.assertEqual(len(screen_results), 1)
        self.assertEqual(screen_results[0]["synced"], 1)
        self.assertEqual(screen_results[0]["remaining"], 0)
        self.assertEqual(storage.load_data_section("pending_backend_events", []), [])
        self.assert_sync_primitives_are_idle()

    def queue_interleaved_scans_of_two_positions(self):
        # Порядок очереди перемешан нарочно: хвост после обрыва должен считаться
        # по списку прохода (события фильтра), а не по позициям очереди целиком.
        codes = (
            ("item-1", "0104006396053978-TEST-ONEAXXXXXX"),
            ("item-2", "0104006396053978-TEST-TWOAXXXXXX"),
            ("item-1", "0104006396053978-TEST-ONEBXXXXXX"),
            ("item-2", "0104006396053978-TEST-TWOBXXXXXX"),
            ("item-1", "0104006396053978-TEST-ONECXXXXXX"),
        )
        with mock.patch.object(backend_events, "backend_configured", return_value=True):
            for order_item_id, code in codes:
                backend_events.queue_backend_scan(
                    {"_backend_order_item_id": order_item_id}, code,
                    scanned_at="2026-05-31T10:00:00+05:00",
                )
        return storage.load_data_section("pending_backend_events", [])

    def test_filtered_sync_network_failure_marks_only_the_tail_of_this_position(self):
        before = self.queue_interleaved_scans_of_two_positions()
        self.assertEqual(len(before), 5)
        create_scan_calls = []

        def unreachable_create_scan(order_item_id, code, **kwargs):
            create_scan_calls.append((order_item_id, code))
            raise backend_client.BackendTransportError(
                "<urlopen error _ssl.c:993: The handshake operation timed out>"
            )

        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "create_scan", side_effect=unreachable_create_scan),
        ):
            result = backend_events.sync_pending_backend_events(order_item_ids=["item-1"])

        # Проход встал на первом же событии позиции, остальные не отправлялись.
        self.assertEqual(create_scan_calls, [("item-1", "0104006396053978-TEST-ONEAXXXXXX")])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["synced"], 0)
        self.assertEqual(result["remaining"], 5)

        after = storage.load_data_section("pending_backend_events", [])
        self.assertEqual([item["id"] for item in after], [item["id"] for item in before])
        by_id_before = {item["id"]: item for item in before}
        head, other_a, tail_b, other_b, tail_c = after

        # Упавшее событие: попытка засчитана, признак сетевой.
        self.assertEqual(head["attempts"], 1)
        self.assertEqual(head["last_error_kind"], "network")
        self.assertIn("handshake operation timed out", head["last_error"])

        # Хвост этой позиции: только признак, попытки, ошибка и время прежние.
        for tail in (tail_b, tail_c):
            self.assertEqual(tail, {**by_id_before[tail["id"]], "last_error_kind": "network"})

        # События другой позиции вне прохода: ни признака, ни попытки, ни времени.
        for other in (other_a, other_b):
            self.assertEqual(other, by_id_before[other["id"]])
            self.assertNotIn("last_error_kind", other)
            self.assertEqual(other["attempts"], 0)

        # Экран позиции теперь читает обрыв связи, а не отказ сервера.
        message = backend_flow.backend_sync_item_blocker({"blocked_events": []}, "item-1", after)
        self.assertIsInstance(backend_flow.backend_blocker_error(message), backend_flow.BackendOfflineQueueError)
        self.assertNotIn("не принял", message)
        # Другая позиция обрыва не наследует.
        other_message = backend_flow.backend_sync_item_blocker({"blocked_events": []}, "item-2", after)
        self.assertNotIsInstance(
            backend_flow.backend_blocker_error(other_message), backend_flow.BackendOfflineQueueError
        )
        self.assert_sync_primitives_are_idle()

    def test_background_pass_network_failure_releases_lock_and_waiters_counter(self):
        before = self.queue_interleaved_scans_of_two_positions()
        create_scan_calls = []

        def unreachable_create_scan(order_item_id, code, **kwargs):
            create_scan_calls.append(order_item_id)
            raise backend_client.BackendTransportError(
                "<urlopen error _ssl.c:993: The handshake operation timed out>"
            )

        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "create_scan", side_effect=unreachable_create_scan),
        ):
            result = backend_events.sync_pending_backend_events(background=True)
            self.assert_sync_primitives_are_idle()
            # Замок свободен: следующий фоновый проход берёт его, а не отвечает skipped.
            second = backend_events.sync_pending_backend_events(background=True)

        self.assertEqual(create_scan_calls, ["item-1", "item-1"])
        self.assertNotIn("skipped", result)
        self.assertNotIn("skipped", second)
        self.assertNotIn("preempted", result)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["remaining"], len(before))
        after = storage.load_data_section("pending_backend_events", [])
        self.assertEqual(len(after), len(before))
        # Без фильтра хвост это вся остальная очередь: проход разметил её целиком.
        self.assertEqual([item["last_error_kind"] for item in after], ["network"] * len(before))
        self.assertEqual(after[0]["attempts"], 2)
        self.assertEqual([item["attempts"] for item in after[1:]], [0] * (len(before) - 1))
        self.assert_sync_primitives_are_idle()

    def test_background_pass_preempted_events_are_not_marked_as_network(self):
        # Вытеснение это не обрыв: невзятые события остаются ровно как были.
        self.queue_two_scans()
        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "create_scan"),
            mock.patch.object(backend_events, "_foreground_waiters_count", side_effect=[0, 1]),
        ):
            result = backend_events.sync_pending_backend_events(background=True)

        self.assertTrue(result["preempted"])
        left = storage.load_data_section("pending_backend_events", [])
        self.assertEqual(len(left), 1)
        self.assertNotIn("last_error_kind", left[0])
        self.assertEqual(left[0]["attempts"], 0)
        self.assertEqual(left[0]["last_error"], "")

    def test_filtered_sync_by_order_ids_sends_only_matching_order_complete(self):
        with mock.patch.object(backend_events, "backend_configured", return_value=True):
            backend_events.queue_backend_scan(
                {"_backend_order_item_id": "item-1"}, "0104006396053978-TEST-ONEXXXXXXX",
                scanned_at="2026-05-31T10:00:00+05:00",
            )
            backend_events.queue_backend_order_complete("order-9")
            backend_events.queue_backend_order_complete("order-10")
        before = storage.load_data_section("pending_backend_events", [])
        self.assertEqual(len(before), 3)

        create_scan_calls = []
        complete_order_calls = []
        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(
                backend_events, "create_scan", side_effect=lambda *a, **k: create_scan_calls.append((a, k))
            ),
            mock.patch.object(
                backend_events, "complete_order", side_effect=lambda order_id: complete_order_calls.append(order_id)
            ),
        ):
            result = backend_events.sync_pending_backend_events(order_ids=[" order-9 "])

        self.assertEqual(create_scan_calls, [])
        self.assertEqual(complete_order_calls, ["order-9"])
        self.assertEqual(result["synced"], 1)
        self.assertEqual(result["remaining"], 2)
        after = storage.load_data_section("pending_backend_events", [])
        expected = [item for item in before if item["payload"].get("order_id") != "order-9"]
        self.assertEqual(after, expected)

    def test_filtered_sync_with_no_match_returns_full_shape_and_touches_nothing(self):
        self.queue_two_scans()
        before = storage.load_data_section("pending_backend_events", [])

        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(
                backend_events, "create_scan", side_effect=AssertionError("must not send")
            ),
        ):
            result = backend_events.sync_pending_backend_events(order_item_ids=["item-404"])

        self.assertEqual(
            result,
            {
                "synced": 0,
                "failed": 0,
                "remaining": 2,
                "dropped": 0,
                "blocked": 0,
                "blocked_events": [],
                "enabled": True,
            },
        )
        self.assertEqual(storage.load_data_section("pending_backend_events", []), before)

    def test_sync_of_empty_queue_returns_full_shape(self):
        with mock.patch.object(backend_events, "backend_configured", return_value=True):
            result = backend_events.sync_pending_backend_events()
            filtered = backend_events.sync_pending_backend_events(order_item_ids=["item-1"])

        expected = {
            "synced": 0,
            "failed": 0,
            "remaining": 0,
            "dropped": 0,
            "blocked": 0,
            "blocked_events": [],
            "enabled": True,
        }
        self.assertEqual(result, expected)
        self.assertEqual(filtered, expected)

    def test_filter_arguments_reject_str_and_bytes(self):
        self.queue_two_scans()
        with mock.patch.object(backend_events, "backend_configured", return_value=True):
            for bad in ("item-1", b"item-1", bytearray(b"item-1")):
                with self.subTest(kind="order_item_ids", value=repr(bad)):
                    with self.assertRaises(TypeError):
                        backend_events.sync_pending_backend_events(order_item_ids=bad)
                with self.subTest(kind="order_ids", value=repr(bad)):
                    with self.assertRaises(TypeError):
                        backend_events.sync_pending_backend_events(order_ids=bad)

    def test_filter_with_empty_collections_sends_nothing(self):
        self.queue_two_scans()
        before = storage.load_data_section("pending_backend_events", [])

        with (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(
                backend_events, "create_scan", side_effect=AssertionError("must not send")
            ),
            mock.patch.object(
                backend_events, "complete_order", side_effect=AssertionError("must not send")
            ),
        ):
            for kwargs in (
                {"order_item_ids": []},
                {"order_ids": []},
                {"order_item_ids": [], "order_ids": []},
                {"order_item_ids": set(), "order_ids": ()},
            ):
                with self.subTest(**{key: repr(value) for key, value in kwargs.items()}):
                    result = backend_events.sync_pending_backend_events(**kwargs)
                    self.assertEqual(result["synced"], 0)
                    self.assertEqual(result["failed"], 0)
                    self.assertEqual(result["remaining"], 2)

        self.assertEqual(storage.load_data_section("pending_backend_events", []), before)


class BackendEventMatchesItemTests(unittest.TestCase):
    """backend_event_matches_item: один предикат с очередью, битый payload не роняет разбор"""

    def test_matches_only_scan_events_of_the_same_position(self):
        matches = backend_flow.backend_event_matches_item
        scan = {"type": "scan", "payload": {"order_item_id": " item-1 ", "code": "C"}}

        self.assertTrue(matches(scan, "item-1"))
        self.assertTrue(matches(scan, " item-1 "))
        self.assertFalse(matches(scan, "item-2"))
        self.assertFalse(matches({"type": "order_complete", "payload": {"order_item_id": "item-1"}}, "item-1"))
        self.assertFalse(matches({"type": "scan", "payload": {}}, "item-1"))
        self.assertFalse(matches({"type": "scan"}, "item-1"))
        self.assertFalse(matches({"type": "scan", "payload": None}, "item-1"))

    def test_empty_item_id_matches_nothing(self):
        matches = backend_flow.backend_event_matches_item
        scan = {"type": "scan", "payload": {"order_item_id": "", "code": "C"}}

        self.assertFalse(matches(scan, ""))
        self.assertFalse(matches(scan, None))
        self.assertFalse(matches(scan, "   "))

    def test_non_dict_payload_gives_false_instead_of_attribute_error(self):
        matches = backend_flow.backend_event_matches_item

        for payload in ("broken", ["item-1"], 5, ("item-1",)):
            with self.subTest(payload=repr(payload)):
                self.assertFalse(matches({"type": "scan", "payload": payload}, "item-1"))

    def test_blocked_events_for_item_survive_a_broken_payload_in_the_result(self):
        good = {"type": "scan", "payload": {"order_item_id": "item-1", "code": "C"}}
        broken = {"type": "scan", "payload": "broken"}

        found = backend_flow.backend_blocked_scan_events_for_item({"blocked_events": [broken, good]}, "item-1")

        self.assertEqual(found, [good])

    def test_item_predicate_is_built_on_the_shared_queue_predicate(self):
        item = {"type": "scan", "payload": {"order_item_id": "item-1"}}

        with mock.patch.object(backend_flow, "backend_event_matches_filter", return_value=True) as predicate:
            self.assertTrue(backend_flow.backend_event_matches_item(item, " item-1 "))

        predicate.assert_called_once_with(item, {"item-1"}, set())


if __name__ == "__main__":
    unittest.main()
