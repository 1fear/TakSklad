import copy
import unittest

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

    def test_percentiles_use_nearest_rank(self):
        values = list(range(1, 101))
        self.assertEqual(50, benchmark_backend.percentile(values, 50))
        self.assertEqual(95, benchmark_backend.percentile(values, 95))
        self.assertEqual(99, benchmark_backend.percentile(values, 99))


if __name__ == "__main__":
    unittest.main()
