import unittest
from unittest import mock

from taksklad import desktop_refresh_service as refresh_service


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


if __name__ == "__main__":
    unittest.main()
