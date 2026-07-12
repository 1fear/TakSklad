import unittest
from unittest import mock

from tools import benchmark_backend
from tools import verify_paired_backend_performance as paired


class PairedBackendPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.budgets = benchmark_backend.load_json(benchmark_backend.BUDGETS_PATH)

    def test_contract_allows_subject_change_but_rejects_measurement_change(self):
        approved = {
            "host": {
                "working_tree_source_hashes": {
                    "runner": "runner",
                    "profiles": "profiles",
                    "budgets": "budgets",
                    "orders_service": "old-subject",
                }
            }
        }
        current = {
            "runner": "runner",
            "profiles": "profiles",
            "budgets": "budgets",
            "orders_service": "new-subject",
        }

        self.assertEqual(paired.measurement_contract_failures(approved, current), [])
        current["runner"] = "changed-runner"
        self.assertEqual(
            paired.measurement_contract_failures(approved, current),
            ["paired measurement contract hash mismatch: runner"],
        )

    def test_pair_ratios_use_median_and_keep_exact_ten_percent_limit(self):
        pairs = [
            paired.synthetic_pair(control=100, candidate=value)
            for value in (105, 120, 106)
        ]
        failures, medians = paired.aggregate_pair_failures(
            pairs, self.budgets, workloads=("queue_claim_50",),
        )

        self.assertEqual(failures, [])
        self.assertEqual(medians["queue_claim_50"]["p95_ms"], 1.06)
        self.assertEqual(medians["queue_claim_50"]["p99_ms"], 1.06)
        self.assertEqual(self.budgets["regression_limit_percent"], 10)

        regressed = [
            paired.synthetic_pair(control=100, candidate=value)
            for value in (105, 120, 112)
        ]
        failures, _medians = paired.aggregate_pair_failures(
            regressed, self.budgets, workloads=("queue_claim_50",),
        )
        self.assertTrue(any("median paired ratio" in item and "1.120000" in item for item in failures))

    def test_candidate_absolute_budget_still_fails(self):
        pair = paired.synthetic_pair(control=50, candidate=101)
        failures = paired.candidate_absolute_failures(
            [pair], self.budgets, workloads=("queue_claim_50",),
        )
        self.assertTrue(any("absolute budget" in item for item in failures))

    def test_pair_parity_rejects_query_or_row_drift(self):
        pair = paired.synthetic_pair(control=100, candidate=100)
        pair["candidate"]["results"]["queue_claim_50"]["query_count"]["median"] = 2
        pair["candidate"]["results"]["queue_claim_50"]["rows_returned"]["median"] = 49

        failures = paired.parity_failures([pair], workloads=("queue_claim_50",))

        self.assertEqual(len(failures), 2)
        self.assertTrue(any("query-count regression" in item for item in failures))
        self.assertTrue(any("row-count mismatch" in item for item in failures))

    def test_worker_environment_drops_credentials(self):
        with mock.patch.dict(
            "os.environ",
            {
                "PATH": "/bin",
                "DATABASE_URL": "forbidden",
                "TELEGRAM_TOKEN": "forbidden",
                "TAKSKLAD_POSTGRES_TEST_IMAGE": "postgres:16-alpine",
            },
            clear=True,
        ):
            environment = paired.worker_environment()

        self.assertEqual(environment["PATH"], "/bin")
        self.assertNotIn("DATABASE_URL", environment)
        self.assertNotIn("TELEGRAM_TOKEN", environment)
        self.assertEqual(environment["TAKSKLAD_NO_PRODUCTION"], "1")
        self.assertEqual(environment["TAKSKLAD_EXTERNAL_SENDS_DISABLED"], "1")


if __name__ == "__main__":
    unittest.main()
