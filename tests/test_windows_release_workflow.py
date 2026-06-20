from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WindowsReleaseWorkflowTests(unittest.TestCase):
    def test_windows_release_collects_taksklad_package_and_smoke_tests_exe(self):
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "build-windows-release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("--collect-submodules=taksklad", workflow)
        self.assertIn('--add-data "assets\\product_images;assets\\product_images"', workflow)
        self.assertIn("pyinstaller_entry.py", workflow)
        self.assertIn("$env:PYTHONPATH = $srcPath", workflow)
        self.assertIn("PYTHONPATH=$srcPath", workflow)
        self.assertIn("Rename-Item taksklad taksklad_bridge_disabled", workflow)
        self.assertIn("import taksklad, taksklad.main", workflow)
        self.assertIn("Smoke test transition exe imports from clean directory", workflow)
        self.assertIn("RUNNER_TEMP", workflow)
        self.assertIn(".\\TakSklad.exe --smoke-import", workflow)
        self.assertIn(".\\TakSklad\\TakSklad.exe --smoke-import", workflow)

    def test_windows_release_does_not_smoke_test_from_checkout_root(self):
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "build-windows-release.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("& dist\\transition\\TakSklad.exe --smoke-import", workflow)
        self.assertNotIn("& dist\\onedir\\TakSklad\\TakSklad.exe --smoke-import", workflow)


if __name__ == "__main__":
    unittest.main()
