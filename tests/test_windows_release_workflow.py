from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WindowsReleaseWorkflowTests(unittest.TestCase):
    def test_windows_release_collects_taksklad_package_and_smoke_tests_exe(self):
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "build-windows-release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("--collect-submodules taksklad", workflow)
        self.assertIn("Smoke test transition exe imports", workflow)
        self.assertIn("dist\\transition\\TakSklad.exe --smoke-import", workflow)
        self.assertIn("dist\\onedir\\TakSklad\\TakSklad.exe --smoke-import", workflow)


if __name__ == "__main__":
    unittest.main()
