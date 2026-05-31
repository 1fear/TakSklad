from pathlib import Path
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "windows_backend_acceptance.ps1"


class WindowsAcceptanceHelperTest(unittest.TestCase):
    def test_helper_contains_expected_backend_flags(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("https://api.taksklad.uz", script)
        self.assertIn("$Token = $env:TAKSKLAD_BACKEND_API_TOKEN", script)
        self.assertIn('$MinAppVersion = "2.0.0"', script)
        self.assertIn('$ExpectedAppVersion = ""', script)
        self.assertIn('$ExpectedBuildLabel = "MVP 2.0"', script)
        self.assertIn("Compare-TakSkladVersion", script)
        self.assertIn("SkipAppVersionCheck", script)
        self.assertIn("SkipBuildLabelCheck", script)
        self.assertIn("APP_VERSION", script)
        self.assertIn("APP_BUILD_LABEL", script)
        self.assertIn("app_build_label", script)
        self.assertIn("build_manifest.json", script)
        self.assertIn("Cannot verify TakSklad.exe version", script)
        self.assertIn(".venv\\Scripts\\python.exe", script)
        self.assertIn("TAKSKLAD_BACKEND_ENABLED", script)
        self.assertIn("TAKSKLAD_BACKEND_READ_ORDERS_ENABLED", script)
        self.assertIn("TAKSKLAD_BACKEND_BASE_URL", script)
        self.assertIn("TAKSKLAD_BACKEND_API_TOKEN", script)
        self.assertIn("TAKSKLAD_BACKEND_TIMEOUT_SECONDS", script)
        self.assertIn("/health", script)
        self.assertIn("/api/v1/orders/active", script)
        self.assertIn("Remove-Item", script)

    def test_helper_does_not_store_token_literal(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        forbidden_fragments = [
            "<service-token-from-local-secret-storage>",
            "api.135.181.245.84.sslip.io",
            "TELEGRAM_BOT_TOKEN=",
            "SKLADBOT_API_TOKEN=",
        ]
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, script)


if __name__ == "__main__":
    unittest.main()
