import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.release_tree_policy import forbidden_path_reason, is_runtime_surface


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECKER = PROJECT_ROOT / "tools" / "check_release_tree.py"


class ReleaseTreePolicyTests(unittest.TestCase):
    def test_every_forbidden_path_class_is_classified_without_content_reads(self):
        cases = {
            "Сверка/client.xlsx": "operational",
            ".env.synthetic": "secret",
            "credentials/service.json": "operational",
            "credentials_test.json": "secret",
            "backups/db.dump": "operational",
            "reports/runtime.txt": "operational",
            "outputs/result.json": "operational",
            "client_exports/customer.csv": "operational",
        }
        for path in cases:
            with self.subTest(path=path):
                self.assertIsNotNone(forbidden_path_reason(path))

    def test_runtime_surfaces_are_explicit(self):
        self.assertTrue(is_runtime_surface("src/taksklad/new_runtime.py"))
        self.assertTrue(is_runtime_surface("tests/test_new_runtime.py"))
        self.assertFalse(is_runtime_surface("docs/release-notes.md"))


class ReleaseTreeCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Synthetic Test"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "synthetic@example.invalid"], cwd=self.root, check=True)
        (self.root / "safe.txt").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "add", "safe.txt"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=self.root, check=True)

    def tearDown(self):
        self.temporary.cleanup()

    def run_checker(self, *args):
        return subprocess.run(
            [sys.executable, str(CHECKER), "--root", str(self.root), *args],
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_staged_guard_rejects_every_forbidden_class(self):
        paths = [
            "Сверка/client.xlsx",
            ".env.synthetic",
            "credentials/service.json",
            "credentials_test.json",
            "backups/db.dump",
            "reports/runtime.txt",
            "outputs/result.json",
            "client_exports/customer.csv",
        ]
        for relative in paths:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic-only\n", encoding="utf-8")
        subprocess.run(["git", "add", "-f", *paths], cwd=self.root, check=True)

        result = self.run_checker("--staged", "--strict", "--path-only")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for relative in paths:
            self.assertIn(relative, result.stderr)

    def test_strict_rejects_untracked_runtime_but_allows_untracked_docs(self):
        runtime = self.root / "src" / "new_runtime.py"
        runtime.parent.mkdir()
        runtime.write_text("VALUE = 1\n", encoding="utf-8")
        docs = self.root / "docs" / "note.md"
        docs.parent.mkdir()
        docs.write_text("synthetic\n", encoding="utf-8")

        result = self.run_checker("--strict", "--path-only")

        self.assertEqual(result.returncode, 1)
        self.assertIn("untracked runtime/source path", result.stderr)
        self.assertNotIn("docs/note.md", result.stderr)

    def test_staged_guard_allows_only_deletion_of_tracked_forbidden_path(self):
        forbidden = self.root / "outputs" / "historical.txt"
        forbidden.parent.mkdir()
        forbidden.write_text("synthetic-only\n", encoding="utf-8")
        subprocess.run(["git", "add", "-f", str(forbidden)], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "historical fixture"], cwd=self.root, check=True)
        forbidden.unlink()
        subprocess.run(["git", "add", "-u"], cwd=self.root, check=True)

        result = self.run_checker("--staged", "--strict", "--path-only")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_owned_manifest_detects_head_and_path_hash_drift(self):
        manifest = self.root / "owned.json"
        written = self.run_checker("--strict", "--write-owned-manifest", "--manifest", str(manifest))
        self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(payload["branch"], "main")

        (self.root / "safe.txt").write_text("drifted\n", encoding="utf-8")
        compared = self.run_checker("--strict", "--compare-owned-manifest", "--manifest", str(manifest))

        self.assertEqual(compared.returncode, 1)
        self.assertIn("allowed path/status/hash set drifted", compared.stderr)

    def test_owned_manifest_can_exclude_generated_evidence_prefix(self):
        manifest = self.root.parent / f"{self.root.name}-owned.json"
        self.addCleanup(manifest.unlink, missing_ok=True)
        evidence = self.root / "test-artifacts" / "release-rehearsal" / "summary.json"
        evidence.parent.mkdir(parents=True)
        evidence.write_text("first\n", encoding="utf-8")
        written = self.run_checker(
            "--strict", "--write-owned-manifest", "--manifest", str(manifest),
            "--exclude-prefix", "test-artifacts/release-rehearsal/",
        )
        self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
        evidence.write_text("second\n", encoding="utf-8")
        compared = self.run_checker(
            "--strict", "--compare-owned-manifest", "--manifest", str(manifest),
            "--exclude-prefix", "test-artifacts/release-rehearsal/",
        )
        self.assertEqual(compared.returncode, 0, compared.stdout + compared.stderr)

    def test_owned_manifest_can_exclude_one_generated_evidence_path(self):
        manifest = self.root.parent / f"{self.root.name}-owned-exact.json"
        self.addCleanup(manifest.unlink, missing_ok=True)
        evidence = self.root / "test-artifacts" / "phase24" / "offsite.json"
        evidence.parent.mkdir(parents=True)
        evidence.write_text("first\n", encoding="utf-8")
        arguments = (
            "--strict", "--manifest", str(manifest),
            "--exclude-path", "test-artifacts/phase24/offsite.json",
        )
        written = self.run_checker("--write-owned-manifest", *arguments)
        self.assertEqual(written.returncode, 0, written.stdout + written.stderr)
        evidence.write_text("second\n", encoding="utf-8")
        compared = self.run_checker("--compare-owned-manifest", *arguments)
        self.assertEqual(compared.returncode, 0, compared.stdout + compared.stderr)


if __name__ == "__main__":
    unittest.main()
