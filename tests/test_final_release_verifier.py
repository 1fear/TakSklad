import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.final_release_verifier import (
    GATES,
    VerificationError,
    _is_declared_clean_change,
    _is_declared_evidence,
    isolated_environment,
    load_identity,
    mandatory_commands,
    run_rehearsals,
    sanitize,
    wait_for_rehearsal_quiescence,
)


IDENTITY = {
    "source_sha": "a" * 40,
    "release_manifest_sha256": "b" * 64,
    "backend_digest": "sha256:" + "c" * 64,
    "frontend_digest": "sha256:" + "d" * 64,
    "windows_artifact_sha256": "e" * 64,
    "sbom_manifest_sha256": "f" * 64,
    "provenance_sha256": "1" * 64,
}


class FinalReleaseVerifierTests(unittest.TestCase):
    @staticmethod
    def fake_workspace_factory(source_sha, destination):
        destination.mkdir(parents=True)
        return {
            "source_sha": source_sha,
            "detached": True,
            "runtime_source_drift": 0,
            "evidence_overlay_paths": [],
            "changed_evidence_paths": [],
        }

    @staticmethod
    def fake_workspace_cleanup(destination):
        if destination.exists():
            destination.rmdir()
        return not destination.exists()

    def test_isolated_environment_does_not_inherit_credentials(self):
        with patch.dict("os.environ", {"PATH": "/bin", "TELEGRAM_TOKEN": "forbidden", "DATABASE_URL": "forbidden"}, clear=True):
            environment = isolated_environment(Path("/tmp/synthetic"), "run-1")
        self.assertEqual(environment["PATH"], "/bin")
        self.assertNotIn("TELEGRAM_TOKEN", environment)
        self.assertNotIn("DATABASE_URL", environment)
        self.assertEqual(environment["TAKSKLAD_EVENT_LEASES_ENABLED"], "0")
        self.assertEqual(environment["SKLADBOT_SKU_MAPPING_JSON"], "")

    def test_repository_identity_fails_closed_until_artifact_source_matches_release(self):
        statement = json.loads(Path("test-artifacts/release/provenance.intoto.json").read_text())
        head = statement["predicate"]["buildDefinition"]["externalParameters"]["sourceSha"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "synthetic.exe"
            artifact.write_bytes(b"synthetic")
            manifest = root / "release.json"
            manifest.write_text(json.dumps({
                "source_sha": head,
                "images": {
                    "backend": {"digest": "sha256:" + "a" * 64},
                    "frontend": {"digest": "sha256:" + "b" * 64},
                },
                "windows": {
                    "artifact": str(artifact),
                    "artifact_sha256": hashlib.sha256(b"synthetic").hexdigest(),
                    "release_source_sha": head,
                    "artifact_source_sha": "0" * 40,
                },
            }))
            with self.assertRaisesRegex(VerificationError, "artifact source SHA"):
                load_identity(manifest)

    def test_phase_commands_are_deduplicated_across_phases_one_to_twenty_five(self):
        commands = mandatory_commands()
        self.assertEqual(len(commands), len(set(commands)))
        self.assertIn("./tools/run_postgres_tests.sh observability", commands)
        self.assertIn("./tools/verify_release_attestations.sh --local", commands)

    def test_requires_exact_phase_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(VerificationError):
                run_rehearsals(repeat=2, same_artifact=True, output_dir=Path(temporary), gates=[], identity=IDENTITY)

    def test_owned_tree_gate_preserves_exact_phase_one_command(self):
        command = next(command for gate_id, _, command in GATES if gate_id == "owned-tree")
        self.assertTrue(command.endswith("tools/check_release_tree.py --compare-owned-manifest --strict"))
        self.assertNotIn("--exclude", command)

    def test_clean_worktree_overlay_allowlist_rejects_runtime_paths(self):
        self.assertTrue(_is_declared_evidence("test-artifacts/release.json"))
        self.assertTrue(_is_declared_evidence("test-artifacts/provenance/verification.json"))
        self.assertFalse(_is_declared_evidence("backend/app/logistics_service.py"))
        self.assertFalse(_is_declared_evidence("tests/test_backend_api_persistence.py"))
        self.assertTrue(_is_declared_clean_change(".venv"))
        self.assertTrue(_is_declared_clean_change("frontend/node_modules"))
        self.assertFalse(_is_declared_clean_change("frontend/src/App.tsx"))

    @patch("tools.final_release_verifier.os.cpu_count", return_value=8)
    @patch("tools.final_release_verifier.os.getloadavg", return_value=(1.0, 1.5, 2.0))
    def test_rehearsal_cooldown_requires_stricter_load_than_benchmark(self, _load, _cpu):
        result = wait_for_rehearsal_quiescence()
        self.assertEqual(result["status"], "quiescent")
        self.assertEqual(result["load_per_cpu"], 0.125)
        self.assertEqual(result["max_load_per_cpu"], 0.15)

    def test_three_fresh_runs_have_same_identity_and_zero_external_effects(self):
        roots = []

        def runner(command, environment, timeout):
            roots.append(environment["TAKSKLAD_REHEARSAL_ROOT"])
            self.assertEqual(environment["TAKSKLAD_NO_PRODUCTION"], "1")
            self.assertEqual(environment["TAKSKLAD_EXTERNAL_SENDS_DISABLED"], "1")
            if "rehearse_deploy" in command:
                return 0, (
                    f"REHEARSE_DEPLOY_OK source_sha={IDENTITY['source_sha']} "
                    f"backend_digest={IDENTITY['backend_digest']} frontend_digest={IDENTITY['frontend_digest']} "
                    "migration_seconds=1 migration_budget_seconds=120 backfill_seconds=2 "
                    "readiness=green worker_heartbeats=green "
                    "production_mutations=0 external_sends=0"
                ), 0.1
            if "rehearse_rollback" in command:
                return 0, (
                    f"REHEARSE_ROLLBACK_OK source_sha={IDENTITY['source_sha']} "
                    f"candidate_backend_digest={IDENTITY['backend_digest']} rollback_seconds=3 "
                    "database_downgrade=0 data_loss=0 production_mutations=0 external_sends=0"
                ), 0.1
            return 0, "PASS", 0.1

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "evidence"
            summary = run_rehearsals(
                repeat=3, same_artifact=True, output_dir=output,
                gates=[("safe-gate", "code_quality", "true")], runner=runner, identity=IDENTITY,
                workspace_factory=self.fake_workspace_factory,
                workspace_cleanup=self.fake_workspace_cleanup,
                quiescence_waiter=lambda: {"status": "quiescent", "waited_seconds": 0},
            )
            manifests = [json.loads((output / f"run-{number}.json").read_text()) for number in range(1, 4)]
            matrix = json.loads((output / "gate-matrix.json").read_text())

        self.assertEqual(summary["status"], "pass")
        self.assertTrue(summary["identities_equal"])
        self.assertEqual(len(set(roots)), 3)
        self.assertTrue(all(item["identity"] == IDENTITY for item in manifests))
        self.assertTrue(all(item["fresh_environment"]["cleanup_zero"] for item in manifests))
        self.assertTrue(all(item["clean_worktree"]["source_sha"] == IDENTITY["source_sha"] for item in manifests))
        self.assertTrue(all(item["clean_worktree"]["runtime_source_drift"] == 0 for item in manifests))
        self.assertTrue(all(item["fresh_environment"]["type"] == "temporary-isolated-clean-worktree" for item in manifests))
        self.assertEqual(summary["production_mutations"], 0)
        self.assertEqual(summary["external_sends"], 0)
        self.assertEqual(summary["timings"]["migration_seconds"]["p95"], 1.0)
        self.assertEqual(summary["timings"]["rollback_seconds"]["p99"], 3.0)
        self.assertTrue(matrix["all_passed"])

    def test_failure_is_fail_closed_and_stops_repetitions(self):
        def runner(command, environment, timeout):
            return 9, "synthetic failure", 0.1

        with tempfile.TemporaryDirectory() as temporary:
            summary = run_rehearsals(
                repeat=3, same_artifact=True, output_dir=Path(temporary),
                gates=[("fails", "security", "false")], runner=runner, identity=IDENTITY,
                workspace_factory=self.fake_workspace_factory,
                workspace_cleanup=self.fake_workspace_cleanup,
                quiescence_waiter=lambda: {"status": "quiescent", "waited_seconds": 0},
            )
        self.assertEqual(summary["status"], "fail")
        self.assertEqual(len(summary["runs"]), 1)
        self.assertFalse(summary["all_gates_passed"])

    def test_worktree_cleanup_failure_marks_run_failed(self):
        def runner(command, environment, timeout):
            if "rehearse_deploy" in command:
                return 0, (
                    f"REHEARSE_DEPLOY_OK source_sha={IDENTITY['source_sha']} "
                    f"backend_digest={IDENTITY['backend_digest']} frontend_digest={IDENTITY['frontend_digest']} "
                    "migration_seconds=1 migration_budget_seconds=120 readiness=green worker_heartbeats=green "
                    "production_mutations=0 external_sends=0"
                ), 0.1
            if "rehearse_rollback" in command:
                return 0, (
                    f"REHEARSE_ROLLBACK_OK source_sha={IDENTITY['source_sha']} "
                    f"candidate_backend_digest={IDENTITY['backend_digest']} rollback_seconds=1 "
                    "database_downgrade=0 data_loss=0 production_mutations=0 external_sends=0"
                ), 0.1
            return 0, "PASS", 0.1

        with tempfile.TemporaryDirectory() as temporary:
            summary = run_rehearsals(
                repeat=3, same_artifact=True, output_dir=Path(temporary),
                gates=[("safe", "code_quality", "true")], runner=runner, identity=IDENTITY,
                workspace_factory=self.fake_workspace_factory,
                workspace_cleanup=lambda destination: False,
                quiescence_waiter=lambda: {"status": "quiescent", "waited_seconds": 0},
            )
        self.assertEqual(summary["status"], "fail")
        self.assertEqual(len(summary["runs"]), 1)
        self.assertFalse(summary["runs"][0]["status"] == "pass")

    def test_duplicate_gate_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(VerificationError):
                run_rehearsals(
                    repeat=3, same_artifact=True, output_dir=Path(temporary), identity=IDENTITY,
                    gates=[("same", "security", "true"), ("same", "security", "true")],
                )

    def test_output_sanitization_redacts_credentials_and_home(self):
        cleaned = sanitize("token=abc password: xyz /Users/anton/private")
        self.assertNotIn("abc", cleaned)
        self.assertNotIn("xyz", cleaned)
        self.assertNotIn("/Users/anton", cleaned)


if __name__ == "__main__":
    unittest.main()
