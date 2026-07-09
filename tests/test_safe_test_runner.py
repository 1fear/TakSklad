import tempfile
import unittest
from pathlib import Path

from tools.run_safe_tests import EXCLUDED_MODULES, discover_safe_test_modules


class SafeTestRunnerTests(unittest.TestCase):
    def test_env_example_readers_are_explicitly_excluded(self):
        self.assertEqual(
            set(EXCLUDED_MODULES),
            {"tests.test_backend_skeleton", "tests.test_vds_acceptance_scripts"},
        )

    def test_discovery_keeps_other_test_modules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tests = root / "tests"
            tests.mkdir()
            (tests / "test_backend_skeleton.py").write_text("", encoding="utf-8")
            (tests / "test_release_tree_guard.py").write_text("", encoding="utf-8")

            modules = discover_safe_test_modules(root)

        self.assertEqual(modules, ["tests.test_release_tree_guard"])


if __name__ == "__main__":
    unittest.main()
