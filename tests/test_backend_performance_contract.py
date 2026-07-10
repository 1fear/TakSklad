import copy
import gc
import io
import json
import tempfile
import unittest
from types import SimpleNamespace
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

from tools import benchmark_backend


class BackendPerformanceContractTests(unittest.TestCase):
    def setUp(self):
        self.profiles = benchmark_backend.load_json(benchmark_backend.PROFILES_PATH)
        self.budgets = benchmark_backend.load_json(benchmark_backend.BUDGETS_PATH)

    def test_profiles_are_fixed_synthetic_and_have_exact_hot_sets(self):
        contract = self.profiles["synthetic_contract"]
        self.assertFalse(contract["contains_real_data"])
        self.assertTrue(contract["namespace"].startswith("SYNTHETIC-"))
        self.assertEqual("unconfirmed", contract["production_scale_status"])
        self.assertIn("unavailable", contract["production_scale_reason"].lower())

        for name, profile in self.profiles["profiles"].items():
            self.assertEqual(20260710, profile["seed"], name)
            self.assertGreaterEqual(profile["orders"], 400, name)
            self.assertEqual(110, profile["scan_targets"], name)
            self.assertEqual(110, profile["complete_targets"], name)
            self.assertEqual(110, profile["return_targets"], name)
            counts = benchmark_backend.expected_counts(profile)
            self.assertEqual(profile["orders"] * profile["items_per_order"], counts["order_items"])
            self.assertEqual(
                counts["order_items"] * profile["scans_per_item"],
                counts["scan_codes"],
            )
            self.assertEqual(0, counts["audit_log"])

    def test_workload_budgets_cover_real_service_paths(self):
        self.assertEqual(set(benchmark_backend.WORKLOADS), set(self.budgets["workloads"]))
        self.assertEqual(next(iter(benchmark_backend.WORKLOADS)), "queue_claim_50")
        self.assertEqual(next(reversed(benchmark_backend.WORKLOADS)), "import_1000")
        self.assertEqual(10, self.budgets["regression_limit_percent"])
        self.assertEqual(3000, self.budgets["workloads"]["import_1000"]["p95_ms"])
        self.assertNotIn("return_lookup_db", benchmark_backend.WORKLOADS)
        self.assertNotIn("import_preview_1000", benchmark_backend.WORKLOADS)

    def test_absolute_budget_failure_is_nonempty(self):
        results = {
            name: {"p95_ms": 0, "p99_ms": 0}
            for name in self.budgets["workloads"]
        }
        results["scan_db"]["p95_ms"] = 151
        failures = benchmark_backend.assertion_failures(results, self.budgets)
        self.assertTrue(any("scan_db.p95_ms" in failure for failure in failures))

    def test_more_than_ten_percent_regression_is_rejected(self):
        results = {
            name: {"p95_ms": 100, "p99_ms": 100}
            for name in self.budgets["workloads"]
        }
        approved = {"results": copy.deepcopy(results)}
        results["day_report"]["p95_ms"] = 111
        failures = benchmark_backend.assertion_failures(results, self.budgets, approved=approved)
        self.assertTrue(any("day_report.p95_ms" in failure and "10%" in failure for failure in failures))

    def test_repeated_regression_uses_median_without_dropping_noisy_run(self):
        approved_metrics = {
            name: {"p95_ms": 100, "p99_ms": 100}
            for name in benchmark_backend.WORKLOADS
        }
        approved = {"results": approved_metrics}

        def runs(values):
            return [
                {"results": {
                    name: {"p95_ms": value, "p99_ms": value}
                    for name in benchmark_backend.WORKLOADS
                }}
                for value in values
            ]

        one_noisy, medians = benchmark_backend.aggregate_regression_failures(
            runs((100, 100, 200)), self.budgets, approved
        )
        two_regressed, _ = benchmark_backend.aggregate_regression_failures(
            runs((100, 120, 200)), self.budgets, approved
        )

        self.assertEqual(one_noisy, [])
        self.assertEqual(medians["scan_db"]["p95_ms"], 100)
        self.assertTrue(any("aggregate median scan_db.p95_ms=120.000" in item for item in two_regressed))

    def test_percentiles_use_nearest_rank(self):
        values = list(range(1, 101))
        self.assertEqual(50, benchmark_backend.percentile(values, 50))
        self.assertEqual(95, benchmark_backend.percentile(values, 95))
        self.assertEqual(99, benchmark_backend.percentile(values, 99))

    def test_return_cleanup_restores_all_phase25_state(self):
        context = {
            "return_order": "synthetic-order",
            "return_items": [{"quantity_blocks": 2}, {"quantity_blocks": 3}],
        }
        result = SimpleNamespace(items=[1, 2])
        with (
            mock.patch("backend.app.orders_service.mark_order_returned", return_value=result),
            mock.patch.object(benchmark_backend, "cleanup_sql", return_value=[4, 3, 5, 1]) as cleanup_sql,
        ):
            _rows, cleanup = benchmark_backend.return_db(mock.Mock(), context, 7)
            cleanup()

        statements = "\n".join(statement for statement, _parameters in cleanup_sql.call_args.args[1])
        self.assertIn("skladbot_return_request_create_queued", statements)
        self.assertIn("google_sheets_archive_export", statements)
        self.assertIn("google_sheets_return_export", statements)
        self.assertIn("skladbot_return_request_status", statements)
        self.assertIn("skladbot_return_create_event_id", statements)
        self.assertIn("skladbot_return_create_idempotency_key", statements)

    def test_scan_and_complete_cleanup_remove_atomic_outbox_state(self):
        scan_context = {"scan_item": "synthetic-item", "base_scanned_blocks": 1}
        scan_result = SimpleNamespace()
        with (
            mock.patch("backend.app.orders_service.create_scan", return_value=scan_result),
            mock.patch.object(benchmark_backend, "cleanup_sql", return_value=[1] * 7) as scan_cleanup_sql,
        ):
            _rows, cleanup = benchmark_backend.scan_db(mock.Mock(), scan_context, 3)
            cleanup()
        scan_statements = "\n".join(
            statement for statement, _parameters in scan_cleanup_sql.call_args.args[1]
        )
        self.assertIn("google_sheets_scan_export", scan_statements)
        self.assertIn("pending_events", scan_statements)

        complete_context = {"complete_order": "synthetic-order", "return_items": [{}, {}, {}]}
        complete_result = SimpleNamespace(items=[1, 2, 3])
        with (
            mock.patch("backend.app.orders_service.complete_order", return_value=complete_result),
            mock.patch.object(benchmark_backend, "cleanup_sql", return_value=[1, 1, 1, 3, 1]) as complete_cleanup_sql,
        ):
            _rows, cleanup = benchmark_backend.complete_db(mock.Mock(), complete_context, 4)
            cleanup()
        complete_statements = "\n".join(
            statement for statement, _parameters in complete_cleanup_sql.call_args.args[1]
        )
        self.assertIn("google_sheets_archive_export", complete_statements)
        self.assertIn("pending_events", complete_statements)

    def test_queue_cleanup_restores_exact_seed_state(self):
        context = {}
        claimed = [SimpleNamespace(), SimpleNamespace()]
        with (
            mock.patch("backend.app.event_leases.claim_event_leases", return_value=claimed),
            mock.patch.object(benchmark_backend, "cleanup_sql", return_value=[2]) as cleanup_sql,
        ):
            _rows, cleanup = benchmark_backend.queue_claim_50(mock.Mock(), context, 9)
            cleanup()

        statement = cleanup_sql.call_args.args[1][0][0]
        self.assertIn("THEN 'failed' ELSE 'pending'", statement)
        self.assertIn("THEN 'synthetic retry' ELSE ''", statement)
        self.assertIn("available_at=created_at", statement)
        self.assertIn("updated_at=created_at", statement)
        self.assertIn("completed_at=NULL", statement)

    def test_benchmark_vacuum_is_allowlisted_and_outside_transactions(self):
        connection = mock.Mock()
        connection.autocommit = False
        context = {"_benchmark_cleanup_connection": connection}

        self.assertTrue(benchmark_backend.benchmark_vacuum(context, "pending_events"))
        connection.execute.assert_called_once_with("VACUUM (ANALYZE) pending_events")
        self.assertFalse(connection.autocommit)
        with self.assertRaises(ValueError):
            benchmark_backend.benchmark_vacuum(context, "unknown_table")

    def test_isolated_gc_sample_restores_interpreter_state(self):
        was_enabled = gc.isenabled()
        with benchmark_backend.isolated_gc_sample():
            self.assertFalse(gc.isenabled())
        self.assertEqual(gc.isenabled(), was_enabled)

    def test_profile_compare_runs_every_workload_three_times_without_overwriting_approved(self):
        metrics = {
            "iterations": 100,
            "warmup_iterations": 10,
            "p50_ms": 1,
            "p95_ms": 1,
            "p99_ms": 1,
            "query_count": {"min": 1, "median": 1, "max": 1},
            "rows_returned": {"min": 1, "median": 1, "max": 1},
        }

        @contextmanager
        def fake_database():
            yield "postgresql+psycopg://synthetic", {"image": "synthetic-postgres"}

        release_state = benchmark_backend.ROOT / ".release-state"
        release_state.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=release_state) as temp_dir:
            evidence_dir = Path(temp_dir)
            approved_path = evidence_dir / "backend-baseline-approved.json"
            approved_payload = {
                "results": {name: copy.deepcopy(metrics) for name in benchmark_backend.WORKLOADS},
            }
            approved_path.write_text(json.dumps(approved_payload), encoding="utf-8")
            measured = []

            def fake_measure(_url, workload, _context, iterations):
                measured.append((workload, iterations))
                return copy.deepcopy(metrics)

            with (
                mock.patch.object(benchmark_backend, "EVIDENCE_DIR", evidence_dir),
                mock.patch.object(benchmark_backend, "disposable_database", fake_database),
                mock.patch.object(
                    benchmark_backend,
                    "seed_profile",
                    return_value=({"table_counts": {"orders": 1}}, evidence_dir / "dataset-reference.json"),
                ),
                mock.patch.object(benchmark_backend, "workload_context", return_value={}),
                mock.patch.object(benchmark_backend, "measure_workload", side_effect=fake_measure),
                mock.patch.object(benchmark_backend, "prepare_profile_benchmark") as prepare_profile,
                mock.patch.object(
                    benchmark_backend, "ensure_foreground_task_policy", return_value="synthetic"
                ) as task_policy,
                mock.patch.object(benchmark_backend, "host_manifest", return_value={"synthetic": True}),
                mock.patch.object(benchmark_backend.time, "sleep") as sleep,
                mock.patch.object(
                    benchmark_backend,
                    "wait_for_benchmark_quiescence",
                    return_value={"waited_seconds": 0},
                ) as quiescence,
            ):
                with redirect_stdout(io.StringIO()):
                    result = benchmark_backend.run_profile_compare("reference", 3, True)

            self.assertEqual(result, 0)
            self.assertEqual(len(measured), 3 * len(benchmark_backend.WORKLOADS))
            self.assertEqual(prepare_profile.call_count, 3)
            task_policy.assert_called_once_with()
            self.assertEqual(sleep.call_count, 3 + 3 * len(benchmark_backend.WORKLOADS))
            self.assertEqual(quiescence.call_count, 3 + 3 * len(benchmark_backend.WORKLOADS))
            self.assertTrue(all(iterations == 100 for _workload, iterations in measured))
            self.assertEqual(json.loads(approved_path.read_text(encoding="utf-8")), approved_payload)
            evidence = json.loads((evidence_dir / "compare-reference.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["repeat"], 3)
            self.assertEqual(evidence["status"], "pass")
            self.assertEqual(len(evidence["runs"]), 3)


if __name__ == "__main__":
    unittest.main()
