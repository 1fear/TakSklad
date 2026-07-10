import unittest

from tools.release_readiness_scorecard import EXPECTED_DOMAINS, ScorecardError, validate_run


IDENTITY = {
    "source_sha": "a" * 40,
    "release_manifest_sha256": "b" * 64,
    "backend_digest": "sha256:" + "c" * 64,
    "frontend_digest": "sha256:" + "d" * 64,
    "windows_artifact_sha256": "e" * 64,
    "sbom_manifest_sha256": "f" * 64,
    "provenance_sha256": "1" * 64,
}


class ReleaseReadinessScorecardTests(unittest.TestCase):
    def valid_run(self):
        return {
            "status": "pass",
            "identity": IDENTITY,
            "fresh_environment": {"cleanup_zero": True},
            "gates": [{"status": "pass", "exit_code": 0}],
            "deploy": {
                "readiness": "green",
                "worker_heartbeats": "green",
                "migration_seconds": "1.5",
                "migration_budget_seconds": "120",
            },
            "rollback": {"rollback_seconds": "3", "database_downgrade": "0", "data_loss": "0"},
            "production_mutations": 0,
            "external_sends": 0,
            "production_deploys": 0,
        }

    def test_domain_contract_is_exactly_ten(self):
        self.assertEqual(len(EXPECTED_DOMAINS), 10)
        self.assertEqual(len(set(EXPECTED_DOMAINS)), 10)

    def test_run_validation_accepts_green_isolated_evidence(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.json"
            path.write_text(json.dumps(self.valid_run()), encoding="utf-8")
            validated = validate_run(path, IDENTITY)
        self.assertEqual(validated["status"], "pass")

    def test_run_validation_rejects_side_effect_and_data_loss(self):
        import json
        import tempfile
        from pathlib import Path

        for field, value in (("production_mutations", 1), ("external_sends", 1)):
            run = self.valid_run()
            run[field] = value
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "run.json"
                path.write_text(json.dumps(run), encoding="utf-8")
                with self.assertRaises(ScorecardError):
                    validate_run(path, IDENTITY)
        run = self.valid_run()
        run["rollback"]["data_loss"] = "1"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.json"
            path.write_text(json.dumps(run), encoding="utf-8")
            with self.assertRaises(ScorecardError):
                validate_run(path, IDENTITY)


if __name__ == "__main__":
    unittest.main()
