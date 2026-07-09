import ast
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from taksklad import main, single_instance, storage, update_service


class StorageConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_data_file = storage.TAKSKLAD_DATA_FILE
        storage.TAKSKLAD_DATA_FILE = str(Path(self.temp_dir.name) / "TakSklad_data.json")

    def tearDown(self):
        storage.TAKSKLAD_DATA_FILE = self.original_data_file
        self.temp_dir.cleanup()

    def _run_barrier_appends(self, left_section, right_section, iterations=40):
        for iteration in range(iterations):
            barrier = threading.Barrier(3)
            failures = []

            def append(section, suffix):
                try:
                    barrier.wait(timeout=5)
                    storage.append_queue_item(section, {"id": f"{iteration}-{suffix}"})
                except Exception as exc:
                    failures.append(exc)

            threads = [
                threading.Thread(target=append, args=(left_section, "left")),
                threading.Thread(target=append, args=(right_section, "right")),
            ]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])

    def test_naive_snapshot_replace_reproduces_lost_update(self):
        shared = []
        barrier = threading.Barrier(3)

        def unsafe_append(value):
            snapshot = list(shared)
            snapshot.append(value)
            barrier.wait(timeout=5)
            shared[:] = snapshot

        threads = [threading.Thread(target=unsafe_append, args=(value,)) for value in ("a", "b")]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(len(shared), 1)

    def test_same_section_barrier_loses_zero_entries(self):
        self._run_barrier_appends("pending_saves", "pending_saves")
        items = storage.load_queue_section("pending_saves")
        self.assertEqual(len(items), 80)
        self.assertEqual(len({item["id"] for item in items}), 80)

    def test_different_section_barrier_loses_zero_entries(self):
        self._run_barrier_appends("pending_saves", "pending_backend_events")
        self.assertEqual(len(storage.load_queue_section("pending_saves")), 40)
        self.assertEqual(len(storage.load_queue_section("pending_backend_events")), 40)

    def test_concurrent_duplicate_event_id_remains_one(self):
        barrier = threading.Barrier(9)
        threads = []
        for _ in range(8):
            thread = threading.Thread(
                target=lambda: (barrier.wait(timeout=5), storage.append_queue_item(
                    "pending_prints", {"id": "duplicate", "payload": "same"}
                )),
            )
            thread.start()
            threads.append(thread)
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(storage.load_queue_section("pending_prints"), [{"id": "duplicate", "payload": "same"}])

    def test_atomic_json_section_mutations_lose_zero_entries(self):
        barrier = threading.Barrier(3)

        def mutate(section, key):
            barrier.wait(timeout=5)
            storage.mutate_data_section(section, lambda value: {**(value or {}), key: True}, default={})

        threads = [
            threading.Thread(target=mutate, args=("product_catalog", "left")),
            threading.Thread(target=mutate, args=("product_catalog", "right")),
        ]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(storage.load_data_section("product_catalog", {}), {"left": True, "right": True})

    def test_crash_before_replace_keeps_old_valid_state(self):
        self.assertTrue(storage.save_data_section("print_settings", {"marker": "old"}))

        def fault(stage):
            if stage == "before_replace":
                raise RuntimeError("synthetic crash before replace")

        with mock.patch.object(storage, "_storage_fault_hook", side_effect=fault):
            self.assertFalse(storage.save_data_section("print_settings", {"marker": "new"}))
        self.assertEqual(storage.load_data_section("print_settings", {}), {"marker": "old"})

    def test_crash_after_replace_keeps_new_valid_state(self):
        self.assertTrue(storage.save_data_section("print_settings", {"marker": "old"}))

        def fault(stage):
            if stage == "after_replace":
                raise RuntimeError("synthetic crash after replace")

        with mock.patch.object(storage, "_storage_fault_hook", side_effect=fault):
            self.assertFalse(storage.save_data_section("print_settings", {"marker": "new"}))
        self.assertEqual(storage.load_data_section("print_settings", {}), {"marker": "new"})

    def test_corrupt_primary_restores_settings_and_identical_sqlite_queue_counts(self):
        for index in range(7):
            storage.append_queue_item("pending_saves", {"id": f"save-{index}"})
        for index in range(5):
            storage.append_queue_item("pending_backend_events", {"id": f"backend-{index}"})
        self.assertTrue(storage.save_data_section("print_settings", {"marker": "last-good"}))
        self.assertTrue(storage.save_data_section("telegram_state", {"last_update_id": 10}))
        before = storage.app_data_queue_counts(storage.load_app_data())
        Path(storage.TAKSKLAD_DATA_FILE).write_text("{corrupt", encoding="utf-8")

        restored = storage.load_app_data()

        self.assertEqual(restored["print_settings"], {"marker": "last-good"})
        self.assertEqual(storage.app_data_queue_counts(restored), before)

    def test_no_production_caller_uses_known_unlocked_load_save_pair(self):
        source_root = Path(__file__).resolve().parents[1] / "src" / "taksklad"
        unsafe_pairs = (
            ("load_pending_saves", "save_pending_saves"),
            ("load_pending_prints", "save_pending_prints"),
            ("load_pending_telegram", "save_pending_telegram"),
            ("load_pending_backend_events", "save_pending_backend_events"),
            ("load_product_catalog", "save_product_catalog"),
            ("load_telegram_state", "save_telegram_state"),
        )
        violations = []
        for path in source_root.glob("*.py"):
            if path.name == "storage.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for function in [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                calls = {
                    node.func.id if isinstance(node.func, ast.Name) else node.func.attr
                    for node in ast.walk(function)
                    if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
                }
                for load_name, save_name in unsafe_pairs:
                    if load_name in calls and save_name in calls:
                        violations.append(f"{path.name}:{function.name}:{load_name}->{save_name}")
        self.assertEqual(violations, [])

    def test_second_instance_returns_before_any_startup_write_or_queue_operation(self):
        denied = single_instance.SingleInstanceResult(False, message="already running", reason="already_running")
        with (
            mock.patch.object(main, "acquire_single_instance_lock", return_value=denied),
            mock.patch.object(main, "show_startup_error_message"),
            mock.patch.object(main, "maybe_rename_windows_executable") as rename,
            mock.patch.object(main, "ensure_windows_desktop_shortcut") as shortcut,
            mock.patch.object(main, "migrate_legacy_json_files_to_app_data") as migrate,
            mock.patch.object(main, "log_startup_self_check") as self_check,
        ):
            self.assertEqual(main.run_app(), 2)
        rename.assert_not_called()
        shortcut.assert_not_called()
        migrate.assert_not_called()
        self_check.assert_not_called()

    def test_onedir_update_preserves_sqlite_main_wal_and_shm(self):
        for filename in (
            "TakSklad_queues.sqlite3",
            "TakSklad_queues.sqlite3-wal",
            "TakSklad_queues.sqlite3-shm",
        ):
            self.assertIn(filename, update_service.UPDATE_RUNTIME_EXCLUDE_FILES)


if __name__ == "__main__":
    unittest.main()
