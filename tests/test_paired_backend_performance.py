import tempfile
import unittest
from pathlib import Path
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

    def test_candidate_runner_can_use_approved_semantic_contract(self):
        approved = {
            "host": {
                "working_tree_source_hashes": {
                    "runner": "historical-runner",
                    "profiles": "profiles",
                    "budgets": "budgets",
                }
            }
        }
        current = {
            "runner": "candidate-runner",
            "profiles": "profiles",
            "budgets": "budgets",
        }

        with mock.patch.object(
            benchmark_backend,
            "_runner_matches_approved_measurement_contract",
            return_value=True,
        ) as semantic_match:
            failures = paired.measurement_contract_failures(
                approved,
                current,
                allow_candidate_runner_semantic_contract=True,
            )

        self.assertEqual(failures, [])
        semantic_match.assert_called_once_with("historical-runner")

    def test_control_runner_still_requires_exact_historical_hash(self):
        approved = {
            "host": {
                "working_tree_source_hashes": {
                    "runner": "historical-runner",
                    "profiles": "profiles",
                    "budgets": "budgets",
                }
            }
        }
        control = {
            "runner": "different-control-runner",
            "profiles": "profiles",
            "budgets": "budgets",
        }

        with mock.patch.object(
            benchmark_backend,
            "_runner_matches_approved_measurement_contract",
            return_value=True,
        ) as semantic_match:
            failures = paired.measurement_contract_failures(approved, control)

        self.assertEqual(
            failures,
            ["paired measurement contract hash mismatch: runner"],
        )
        semantic_match.assert_not_called()

    def test_candidate_runner_semantic_mismatch_still_fails(self):
        approved = {
            "host": {
                "working_tree_source_hashes": {
                    "runner": "historical-runner",
                    "profiles": "profiles",
                    "budgets": "budgets",
                }
            }
        }
        current = {
            "runner": "candidate-runner",
            "profiles": "profiles",
            "budgets": "budgets",
        }

        with mock.patch.object(
            benchmark_backend,
            "_runner_matches_approved_measurement_contract",
            return_value=False,
        ):
            failures = paired.measurement_contract_failures(
                approved,
                current,
                allow_candidate_runner_semantic_contract=True,
            )

        self.assertEqual(
            failures,
            ["paired measurement contract hash mismatch: runner"],
        )

    def test_candidate_semantic_contract_does_not_waive_profiles_or_budgets(self):
        approved = {
            "host": {
                "working_tree_source_hashes": {
                    "runner": "historical-runner",
                    "profiles": "historical-profiles",
                    "budgets": "historical-budgets",
                }
            }
        }
        current = {
            "runner": "candidate-runner",
            "profiles": "candidate-profiles",
            "budgets": "candidate-budgets",
        }

        with mock.patch.object(
            benchmark_backend,
            "_runner_matches_approved_measurement_contract",
            return_value=True,
        ):
            failures = paired.measurement_contract_failures(
                approved,
                current,
                allow_candidate_runner_semantic_contract=True,
            )

        self.assertEqual(
            failures,
            [
                "paired measurement contract hash mismatch: profiles",
                "paired measurement contract hash mismatch: budgets",
            ],
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

    def test_swapped_launch_order_cancels_process_slot_bias(self):
        balanced = {
            "races": [
                paired.synthetic_pair(control=100, candidate=120),
                paired.synthetic_pair(control=120, candidate=100),
            ]
        }
        pairs = [balanced, balanced, balanced]

        failures, medians = paired.aggregate_pair_failures(
            pairs, self.budgets, workloads=("queue_claim_50",),
        )

        self.assertEqual(failures, [])
        self.assertEqual(medians["queue_claim_50"]["p95_ms"], 1.0)
        self.assertEqual(medians["queue_claim_50"]["p99_ms"], 1.0)

    def test_balanced_pair_recomputes_tails_from_pooled_raw_samples(self):
        balanced = {
            "races": [
                paired.synthetic_pair(control=100, candidate=100),
                paired.synthetic_pair(control=100, candidate=100),
            ]
        }
        for race in balanced["races"]:
            race["control"]["results"]["queue_claim_50"]["p95_ms"] = 1
            race["candidate"]["results"]["queue_claim_50"]["p95_ms"] = 1000

        failures, medians = paired.aggregate_pair_failures(
            [balanced, balanced, balanced],
            self.budgets,
            workloads=("queue_claim_50",),
        )

        self.assertEqual(failures, [])
        self.assertEqual(medians["queue_claim_50"]["p95_ms"], 1.0)

    def test_balanced_pair_rejects_raw_sample_count_mismatch(self):
        pair = paired.synthetic_pair(control=100, candidate=100)
        pair["candidate"]["results"]["queue_claim_50"]["durations_ms"].pop()

        with self.assertRaisesRegex(ValueError, "raw sample count mismatch"):
            paired.aggregate_pair_failures(
                [pair, pair, pair], self.budgets, workloads=("queue_claim_50",),
            )

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

    def test_worker_uses_shared_release_without_self_observing_quiescence(self):
        worker = paired.WORKER_CODE

        self.assertNotIn("original_quiescence", worker)
        self.assertNotIn("host_quiescence", worker)
        self.assertIn("time.monotonic() + 0.5", worker)
        self.assertIn("temporary_release.replace(release)", worker)
        self.assertIn("release_at - time.monotonic()", worker)

    def test_parent_quiescence_completes_before_workers_spawn(self):
        events = []
        quiescence = {
            "status": "quiescent",
            "method": "aggregate-cpu-idle",
            "max_cpu_busy_percent": 20.0,
        }
        process = mock.Mock()
        process.poll.return_value = 0

        def wait_for_quiescence():
            events.append("quiescence")
            return quiescence

        def start_worker(*_args, **_kwargs):
            events.append("spawn")
            return process

        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(paired.subprocess, "Popen", side_effect=start_worker),
                mock.patch.object(
                    paired,
                    "_decode_worker",
                    side_effect=[{"side": "control"}, {"side": "candidate"}],
                ),
            ):
                result = paired.run_pair(
                    profile="reference",
                    control_root=Path(temporary) / "control",
                    candidate_root=Path(temporary) / "candidate",
                    barrier_root=Path(temporary) / "barrier",
                    parent_quiescence_waiter=wait_for_quiescence,
                )

        self.assertEqual(events, ["quiescence", "spawn", "spawn"])
        self.assertEqual(result["parent_quiescence"], quiescence)


if __name__ == "__main__":
    unittest.main()
