from pathlib import Path
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "build_windows_test_archive.ps1"


class WindowsTestBuildHelperTest(unittest.TestCase):
    def test_helper_builds_test_archive_without_rollout_manifest(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("PyInstaller", script)
        self.assertIn("--onedir", script)
        self.assertIn("TakSklad-test-$AppVersion-windows-x64", script)
        self.assertIn("windows_test_onedir_zip", script)
        self.assertIn('$ExpectedBuildLabel = "MVP 2.0"', script)
        self.assertIn("APP_BUILD_LABEL", script)
        self.assertIn("app_build_label = $AppBuildLabel", script)
        self.assertIn("Build label: $AppBuildLabel", script)
        self.assertIn("version.json has local changes", script)
        self.assertIn("forced 2.0.15 rollout manifest", script)
        self.assertIn("public_version_json_changed = $false", script)
        self.assertIn("windows_backend_acceptance.ps1", script)
        self.assertIn("release_go_no_go.py", script)
        self.assertIn("prepare_acceptance_kit.py", script)
        self.assertIn("ACCEPTANCE_RESULTS_TEMPLATE.md", script)
        self.assertIn("ACCEPTANCE_RESULTS.md", script)
        self.assertIn("build_manifest.json", script)
        self.assertIn("PackagedAppDir", script)
        self.assertIn("TakSklad.exe", script)
        self.assertIn("Compress-Archive", script)

    def test_helper_does_not_embed_secrets_or_release_uploads(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("Assert-TestPackageDoesNotContainLocalSecrets", script)
        self.assertIn("Test package contains local runtime/secret file", script)
        forbidden_fragments = [
            "gh release upload",
            "TELEGRAM_BOT_TOKEN=",
            "SKLADBOT_API_TOKEN=",
            "GOOGLE_PRIVATE_KEY",
            "service-token-from-local-secret-storage",
        ]
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, script)


if __name__ == "__main__":
    unittest.main()
