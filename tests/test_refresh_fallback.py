import threading
import unittest
from unittest import mock

from taksklad import backend_events
from taksklad import desktop_refresh_service as refresh_service
from taksklad.app_data_loading import DataLoadingMixin


class BackendOnlyRefreshTests(unittest.TestCase):
    def test_backend_only_mode_is_mandatory(self):
        self.assertTrue(refresh_service.backend_only_refresh_enabled())

    def test_refresh_reads_backend_and_merges_offline_backend_codes(self):
        with (
            mock.patch.object(
                refresh_service,
                "fetch_backend_sheet_data",
                return_value=([{"Клиент": "Backend"}], None, {"REMOTE"}),
            ),
            mock.patch.object(refresh_service, "get_pending_backend_codes", return_value={"OFFLINE"}),
        ):
            orders, source, codes = refresh_service.fetch_sheet_data()

        self.assertEqual(orders, [{"Клиент": "Backend"}])
        self.assertIsNone(source)
        self.assertEqual(codes, {"REMOTE", "OFFLINE"})

    def test_backend_failure_is_fail_closed(self):
        with mock.patch.object(
            refresh_service,
            "fetch_backend_sheet_data",
            side_effect=RuntimeError("timeout"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Backend refresh недоступен"):
                refresh_service.fetch_sheet_data()

    def test_refresh_syncs_offline_events_and_server_sources_only(self):
        with (
            mock.patch.object(
                refresh_service,
                "sync_pending_backend_events",
                return_value={"enabled": True, "synced": 1, "remaining": 0},
            ),
            mock.patch.object(
                refresh_service,
                "sync_backend_sources",
                return_value={"status": "completed", "skladbot": {"status": "completed", "matched": 2}},
            ) as sync_sources,
            mock.patch.object(
                refresh_service,
                "fetch_backend_sheet_data",
                return_value=([{"Клиент": "Backend"}], None, set()),
            ),
            mock.patch.object(refresh_service, "get_pending_backend_codes", return_value=set()),
        ):
            _orders, _source, _codes, result = refresh_service.fetch_sheet_data_with_sync()

        sync_sources.assert_called_once_with(sync_skladbot=True, wait_skladbot=False)
        self.assertEqual(result["primary_source"], "backend")
        self.assertTrue(result["backend_only_refresh"])
        self.assertNotIn("google_sheets", result)
        self.assertEqual(result["skladbot"]["matched"], 2)


class RefreshQueuePassTests(unittest.TestCase):
    """Обновление списка не держит замок очереди на весь проход, импорт шлёт очередь целиком"""

    SCAN_EVENT = {
        "id": "scan-1",
        "type": "scan",
        "payload": {"order_item_id": "item-1", "code": "TEST-CODE-ABC"},
        "attempts": 0,
        "last_error": "",
    }

    def setUp(self):
        for patcher in (
            mock.patch.object(refresh_service, "sync_backend_sources", return_value={"status": "completed"}),
            mock.patch.object(
                refresh_service,
                "fetch_backend_sheet_data",
                return_value=([{"Клиент": "Backend"}], None, set()),
            ),
            mock.patch.object(refresh_service, "get_pending_backend_codes", return_value=set()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def patch_real_queue_with_one_scan(self):
        """Настоящий проход очереди без сети и без записи в хранилище"""
        create_scan = mock.Mock(return_value={})
        for patcher in (
            mock.patch.object(backend_events, "backend_configured", return_value=True),
            mock.patch.object(backend_events, "load_pending_backend_events", return_value=[dict(self.SCAN_EVENT)]),
            mock.patch.object(backend_events, "reconcile_queue_section", return_value=[]),
            mock.patch.object(backend_events, "record_blocked_backend_events"),
            mock.patch.object(backend_events, "create_scan", create_scan),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        backend_events._DELIVERED_SCAN_KEYS.clear()
        self.addCleanup(backend_events._DELIVERED_SCAN_KEYS.clear)
        return create_scan

    def run_in_thread(self, call):
        outcome = {}

        def target():
            try:
                outcome["result"] = call()
            except Exception as exc:  # pragma: no cover - показывается в утверждениях
                outcome["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread, outcome

    def test_refresh_pass_is_full_by_default(self):
        with mock.patch.object(
            refresh_service, "sync_pending_backend_events", return_value={"enabled": True, "synced": 0, "remaining": 0}
        ) as sync:
            refresh_service.fetch_sheet_data_with_sync()

        sync.assert_called_once_with(background=False)

    def test_refresh_pass_can_run_in_background_mode(self):
        with mock.patch.object(
            refresh_service, "sync_pending_backend_events", return_value={"enabled": True, "synced": 0, "remaining": 0}
        ) as sync:
            refresh_service.fetch_sheet_data_with_sync(background=True)

        sync.assert_called_once_with(background=True)

    def test_background_refresh_neither_waits_nor_sends_while_the_queue_lock_is_busy(self):
        create_scan = self.patch_real_queue_with_one_scan()
        backend_events._SYNC_LOCK.acquire()
        try:
            thread, outcome = self.run_in_thread(lambda: refresh_service.fetch_sheet_data_with_sync(background=True))
            thread.join(timeout=5)
            finished = not thread.is_alive()
        finally:
            backend_events._SYNC_LOCK.release()
            thread.join(timeout=5)

        self.assertTrue(finished, "обновление списка ждёт замок очереди")
        self.assertNotIn("error", outcome)
        create_scan.assert_not_called()
        _orders, _sheet, _codes, sync_result = outcome["result"]
        self.assertTrue(sync_result["backend"]["skipped"])
        self.assertEqual(sync_result["primary_source"], "backend")

    def test_background_refresh_sends_the_queue_when_the_lock_is_free(self):
        create_scan = self.patch_real_queue_with_one_scan()

        refresh_service.fetch_sheet_data_with_sync(background=True)

        create_scan.assert_called_once()

    def test_import_refresh_uses_the_full_pass_and_waits_for_the_queue_lock(self):
        create_scan = self.patch_real_queue_with_one_scan()
        app = DataLoadingMixin()
        backend_events._SYNC_LOCK.acquire()
        try:
            thread, outcome = self.run_in_thread(app.fetch_sheet_data_after_import)
            thread.join(timeout=0.3)
            waiting = thread.is_alive()
            create_scan.assert_not_called()
        finally:
            backend_events._SYNC_LOCK.release()
        thread.join(timeout=5)

        self.assertTrue(waiting, "импорт не должен пропускать проход очереди")
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", outcome)
        create_scan.assert_called_once()
        self.assertNotIn("skipped", outcome["result"][3]["backend"])

    def test_import_refresh_asks_for_a_full_pass(self):
        with mock.patch(
            "taksklad.app_data_loading.fetch_sheet_data_with_sync", return_value=([], None, set(), {})
        ) as fetch:
            DataLoadingMixin().fetch_sheet_data_after_import()

        fetch.assert_called_once_with(sync_skladbot=False)


if __name__ == "__main__":
    unittest.main()
