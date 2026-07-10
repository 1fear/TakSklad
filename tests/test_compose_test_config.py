import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from tools.render_compose_test_config import render_config


class ComposeTestConfigTests(unittest.TestCase):
    def test_renderer_writes_synthetic_restrictive_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "compose-test-config"

            count = render_config(output)

            content = output.read_text(encoding="utf-8")
            self.assertGreater(count, 10)
            self.assertIn("TAKSKLAD_ENV=test", content)
            self.assertNotIn("TAKSKLAD_ENV_FILE", content)
            self.assertNotIn("private_key", content.lower())
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_renderer_rejects_forbidden_env_filename(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "forbidden"):
                render_config(Path(temporary) / ".env.synthetic")

    def test_contract_is_json_and_synthetic_only(self):
        contract_path = Path(__file__).resolve().parents[1] / "deploy/vds/config-contract.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        serialized = json.dumps(contract).casefold()
        self.assertIn("synthetic-only", serialized)
        self.assertNotIn("credentials.json", serialized)


if __name__ == "__main__":
    unittest.main()
