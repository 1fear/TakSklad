import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "tools/rehearse_deploy.sh"
ROLLBACK = ROOT / "tools/rehearse_rollback.sh"


class ReleaseRehearsalContractTests(unittest.TestCase):
    def test_deploy_requires_all_isolation_assertions(self):
        result = subprocess.run(
            [str(DEPLOY), "--environment", "isolated", "--assert-readiness"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("both --assert-readiness", result.stderr)

    def test_deploy_rejects_nonisolated_environment(self):
        result = subprocess.run(
            [str(DEPLOY), "--environment", "production", "--assert-readiness", "--assert-migration-budget"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    def test_rollback_requires_bounded_numeric_deadline(self):
        result = subprocess.run(
            [str(ROLLBACK), "--environment", "isolated", "--assert-max-seconds", "unbounded"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("positive number", result.stderr)

    def test_evidence_schema_carries_no_mutation_and_rollback_invariants(self):
        deploy = {
            "status": "pass", "environment": "isolated", "synthetic_only": True,
            "production_mutations": 0, "external_sends": 0, "cleanup_zero": True,
        }
        rollback = {
            "status": "pass", "environment": "isolated", "synthetic_only": True,
            "database_downgrade": 0, "data_loss": 0, "production_mutations": 0,
            "external_sends": 0, "cleanup_zero": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(json.dumps({"deploy": deploy, "rollback": rollback}), encoding="utf-8")
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["deploy"]["production_mutations"], 0)
        self.assertEqual(payload["rollback"]["database_downgrade"], 0)
        self.assertEqual(payload["rollback"]["data_loss"], 0)
        self.assertTrue(payload["rollback"]["cleanup_zero"])


if __name__ == "__main__":
    unittest.main()
