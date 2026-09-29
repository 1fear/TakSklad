import tempfile
import unittest
from pathlib import Path

from taksklad import backend_events, storage
from taksklad.backend_flow import (
    BackendOfflineQueueError,
    backend_blocker_error,
    backend_group_blocker_error,
    backend_sync_group_blocker,
    backend_sync_item_blocker,
)
from taksklad.backend_client import BackendApiError, BackendTransportError


class BackendEventQueueTests(unittest.TestCase):
    def setUp(self):
        # Заблокированные события уходят в durable-хранилище, поэтому тест
        # обязан работать в своём каталоге, а не в реальном TakSklad_queues.sqlite3.
        self._temp_dir = tempfile.TemporaryDirectory()
        self._original_data_file = storage.TAKSKLAD_DATA_FILE
        storage.TAKSKLAD_DATA_FILE = str(Path(self._temp_dir.name) / "TakSklad_data.json")
        self.original_backend_configured = backend_events.backend_configured
        self.original_load_pending_backend_events = backend_events.load_pending_backend_events
        self.original_save_pending_backend_events = backend_events.save_pending_backend_events
        self.original_create_scan = backend_events.create_scan
        self.original_complete_order = backend_events.complete_order
        self.original_reconcile_queue_section = backend_events.reconcile_queue_section
        self.original_undo_scan = backend_events.undo_scan
        backend_events._DELIVERED_SCAN_KEYS.clear()

    def tearDown(self):
        backend_events.backend_configured = self.original_backend_configured
        backend_events.load_pending_backend_events = self.original_load_pending_backend_events
        backend_events.save_pending_backend_events = self.original_save_pending_backend_events
        backend_events.create_scan = self.original_create_scan
        backend_events.complete_order = self.original_complete_order
        backend_events.reconcile_queue_section = self.original_reconcile_queue_section
        backend_events.undo_scan = self.original_undo_scan
        backend_events._DELIVERED_SCAN_KEYS.clear()
        storage.TAKSKLAD_DATA_FILE = self._original_data_file
        self._temp_dir.cleanup()

    def use_pending_events(self, items):
        state = {"items": list(items)}
        backend_events.backend_configured = lambda: True
        backend_events.load_pending_backend_events = lambda: state["items"]

        def save_pending(items_to_save):
            state["items"] = list(items_to_save)
            return True

        backend_events.save_pending_backend_events = save_pending

        def reconcile(_section, _snapshot, remaining):
            state["items"] = list(remaining)
            return state["items"]

        backend_events.reconcile_queue_section = reconcile
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

    def test_transport_failure_is_marked_as_network_kind(self):
        # Текст ошибки снят с боевого экрана склада 09.09.2026: TLS-рукопожатие
        # не уложилось в таймаут, ответа с кодом у такого отказа нет.
        state = self.use_pending_events([
            {
                "id": "scan-1",
                "type": "scan",
                "payload": {"order_item_id": "item-1", "code": "TEST-CODE-ABC"},
                "attempts": 0,
                "last_error": "",
            }
        ])

        def unreachable_create_scan(*args, **kwargs):
            raise BackendTransportError("<urlopen error _ssl.c:993: The handshake operation timed out>")

        backend_events.create_scan = unreachable_create_scan

        backend_events.sync_pending_backend_events()

        self.assertEqual(state["items"][0]["last_error_kind"], "network")

    def test_server_failure_is_not_marked_as_network_kind(self):
        state = self.use_pending_events([
            {
                "id": "scan-1",
                "type": "scan",
                "payload": {"order_item_id": "item-1", "code": "TEST-CODE-ABC"},
                "attempts": 0,
                "last_error": "",
            }
        ])

        def failing_create_scan(*args, **kwargs):
            raise BackendApiError("Backend HTTP 503: service unavailable", status_code=503)

        backend_events.create_scan = failing_create_scan

        backend_events.sync_pending_backend_events()

        self.assertEqual(state["items"][0]["last_error_kind"], "server")

    def test_error_without_status_code_that_is_not_transport_is_marked_as_server_kind(self):
        # Ответ без кода это ещё не обрыв: 200 от прокси с не-JSON телом или
        # ошибка в коде клиента не должны глушить проход очереди и рисовать
        # оператору «связь прервалась».
        state = self.use_pending_events([
            {
                "id": f"scan-{index}",
                "type": "scan",
                "payload": {"order_item_id": "item-1", "code": f"TEST-CODE-{index}"},
                "attempts": 0,
                "last_error": "",
            }
            for index in range(3)
        ])
        calls = []

        def broken_answer_create_scan(*args, **kwargs):
            calls.append(1)
            raise BackendApiError("Expecting value: line 1 column 1 (char 0)")

        backend_events.create_scan = broken_answer_create_scan

        backend_events.sync_pending_backend_events()

        self.assertEqual(len(calls), 3)
        self.assertEqual([item["last_error_kind"] for item in state["items"]], ["server"] * 3)

    def test_network_failure_stops_the_run_and_leaves_the_rest_untouched(self):
        # Канал лежит для всей очереди сразу: семь событий по 8 секунд держали
        # оператора минуту, хотя исход у них общий.
        state = self.use_pending_events([
            {
                "id": f"scan-{index}",
                "type": "scan",
                "payload": {"order_item_id": "item-1", "code": f"TEST-CODE-{index}"},
                "attempts": 0,
                "last_error": "",
            }
            for index in range(3)
        ])
        calls = []

        def unreachable_create_scan(*args, **kwargs):
            calls.append(1)
            raise BackendTransportError("<urlopen error _ssl.c:993: The handshake operation timed out>")

        backend_events.create_scan = unreachable_create_scan

        result = backend_events.sync_pending_backend_events()

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["remaining"], 3)
        self.assertEqual(state["items"][0]["attempts"], 1)
        self.assertEqual(state["items"][1]["attempts"], 0)
        self.assertEqual(state["items"][2]["attempts"], 0)

    def scan_events(self, count, **extra):
        return [
            {
                "id": f"scan-{index}",
                "type": "scan",
                "payload": {"order_item_id": "item-1", "code": f"TEST-CODE-{index}"},
                "attempts": 0,
                "last_error": "",
                **extra,
            }
            for index in range(count)
        ]

    @staticmethod
    def unreachable_create_scan(*args, **kwargs):
        raise BackendTransportError("<urlopen error _ssl.c:993: The handshake operation timed out>")

    def test_network_failure_marks_the_whole_position_so_offline_wording_shows(self):
        # 09.09.2026: семь событий позиции, первое упало по сети, проход встал.
        # Признак получало только первое, поэтому «все события позиции сетевые»
        # не выполнялось и оператор снова читал «Backend не принял КИЗы».
        state = self.use_pending_events(self.scan_events(7))
        backend_events.create_scan = self.unreachable_create_scan

        backend_events.sync_pending_backend_events()

        self.assertEqual([item["last_error_kind"] for item in state["items"]], ["network"] * 7)
        item_message = backend_sync_item_blocker({"blocked_events": []}, "item-1", state["items"])
        self.assertIn("Связь с сервером", item_message)
        self.assertIn("7", item_message)
        self.assertNotIn("не принял", item_message)
        self.assertIsInstance(backend_blocker_error(item_message), BackendOfflineQueueError)
        group_message = backend_sync_group_blocker({"blocked_events": []}, ["item-1"], [], state["items"])
        self.assertIn("Связь с сервером", group_message)
        self.assertNotIn("не принял", group_message)
        self.assertIsInstance(backend_group_blocker_error(group_message), BackendOfflineQueueError)

    def test_network_failure_marks_the_order_complete_event_of_the_tail_too(self):
        events = self.scan_events(2) + [
            {
                "id": "complete-1",
                "type": "order_complete",
                "payload": {"order_id": "order-1"},
                "attempts": 0,
                "last_error": "",
            }
        ]
        state = self.use_pending_events(events)
        backend_events.create_scan = self.unreachable_create_scan

        backend_events.sync_pending_backend_events()

        message = backend_sync_group_blocker({"blocked_events": []}, ["item-1"], ["order-1"], state["items"])
        self.assertIn("Связь с сервером", message)
        self.assertNotIn("не принял", message)

    def test_network_failure_keeps_attempts_error_and_time_of_the_untouched_tail(self):
        events = self.scan_events(3, last_error="earlier", updated_at="2026-09-09T10:00:00+05:00")
        events[1]["attempts"] = 4
        state = self.use_pending_events(events)
        backend_events.create_scan = self.unreachable_create_scan

        backend_events.sync_pending_backend_events()

        self.assertEqual(state["items"][0]["attempts"], 1)
        for item, attempts in zip(state["items"][1:], (4, 0)):
            self.assertEqual(item["attempts"], attempts)
            self.assertEqual(item["last_error"], "earlier")
            self.assertEqual(item["updated_at"], "2026-09-09T10:00:00+05:00")
            self.assertEqual(item["last_error_kind"], "network")

    def test_network_failure_does_not_relabel_a_tail_event_with_a_server_error(self):
        events = self.scan_events(3)
        events[2]["last_error"] = "Backend HTTP 503: service unavailable"
        events[2]["last_error_kind"] = "server"
        state = self.use_pending_events(events)
        backend_events.create_scan = self.unreachable_create_scan

        backend_events.sync_pending_backend_events()

        self.assertEqual(state["items"][1]["last_error_kind"], "network")
        self.assertEqual(state["items"][2]["last_error_kind"], "server")
        message = backend_sync_item_blocker({"blocked_events": []}, "item-1", state["items"])
        self.assertIn("Backend не принял", message)
        self.assertNotIn("Связь с сервером", message)

    def test_network_failure_leaves_already_network_tail_event_as_it_was(self):
        events = self.scan_events(2)
        events[1]["last_error_kind"] = "network"
        state = self.use_pending_events(events)
        backend_events.create_scan = self.unreachable_create_scan

        backend_events.sync_pending_backend_events()

        self.assertEqual([item["last_error_kind"] for item in state["items"]], ["network", "network"])

    def test_server_failure_does_not_stop_the_run(self):
        state = self.use_pending_events([
            {
                "id": f"scan-{index}",
                "type": "scan",
                "payload": {"order_item_id": "item-1", "code": f"TEST-CODE-{index}"},
                "attempts": 0,
                "last_error": "",
            }
            for index in range(3)
        ])
        calls = []

        def failing_create_scan(*args, **kwargs):
            calls.append(1)
            raise BackendApiError("Backend HTTP 503: service unavailable", status_code=503)

        backend_events.create_scan = failing_create_scan

        result = backend_events.sync_pending_backend_events()

        self.assertEqual(len(calls), 3)
        self.assertEqual(result["failed"], 3)
        self.assertEqual(state["items"][2]["attempts"], 1)

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

    def test_successful_scan_marks_delivered_registry(self):
        self.use_pending_events([
            {
                "id": "scan-1",
                "type": "scan",
                "payload": {"order_item_id": "item-1", "code": "TEST-CODE-ABC"},
                "attempts": 0,
                "last_error": "",
            }
        ])
        backend_events.create_scan = lambda *args, **kwargs: None

        self.assertFalse(backend_events.is_scan_delivered("item-1", "TEST-CODE-ABC"))
        backend_events.sync_pending_backend_events()
        self.assertTrue(backend_events.is_scan_delivered("item-1", "TEST-CODE-ABC"))

    def test_duplicate_scan_ack_marks_delivered_registry(self):
        self.use_pending_events([
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

        backend_events.sync_pending_backend_events()
        self.assertTrue(backend_events.is_scan_delivered("item-1", "TEST-CODE-ABC"))

    def test_network_error_does_not_mark_delivered_registry(self):
        self.use_pending_events([
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

        backend_events.sync_pending_backend_events()
        self.assertFalse(backend_events.is_scan_delivered("item-1", "TEST-CODE-ABC"))

    def test_blocked_conflict_does_not_mark_delivered_registry(self):
        self.use_pending_events([
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

        backend_events.sync_pending_backend_events()
        self.assertFalse(backend_events.is_scan_delivered("item-1", "TEST-CODE-ABC"))

    def test_new_failure_forgets_previously_delivered_registry_key(self):
        backend_events._DELIVERED_SCAN_KEYS.add(("item-1", "TEST-CODE-ABC"))
        self.use_pending_events([
            {
                "id": "scan-1",
                "type": "scan",
                "payload": {"order_item_id": "item-1", "code": "TEST-CODE-ABC"},
                "attempts": 0,
                "last_error": "",
            }
        ])
        backend_events.create_scan = lambda *args, **kwargs: (_ for _ in ()).throw(BackendApiError("timeout"))

        backend_events.sync_pending_backend_events()
        self.assertFalse(backend_events.is_scan_delivered("item-1", "TEST-CODE-ABC"))

    def test_remove_pending_backend_scan_forgets_delivered_registry(self):
        backend_events._DELIVERED_SCAN_KEYS.add(("item-1", "TEST-CODE-ABC"))

        backend_events.remove_pending_backend_scan({"_backend_order_item_id": "item-1"}, "TEST-CODE-ABC")

        self.assertFalse(backend_events.is_scan_delivered("item-1", "TEST-CODE-ABC"))

    def test_undo_backend_scan_forgets_delivered_registry(self):
        backend_events._DELIVERED_SCAN_KEYS.add(("item-1", "TEST-CODE-ABC"))
        backend_events.backend_configured = lambda: True
        backend_events.undo_scan = lambda *args, **kwargs: {"status": "ok"}

        backend_events.undo_backend_scan({"_backend_order_item_id": "item-1"}, "TEST-CODE-ABC")

        self.assertFalse(backend_events.is_scan_delivered("item-1", "TEST-CODE-ABC"))


if __name__ == "__main__":
    unittest.main()
