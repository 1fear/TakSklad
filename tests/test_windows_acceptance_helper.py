from pathlib import Path
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "windows_backend_acceptance.ps1"


class WindowsAcceptanceHelperTest(unittest.TestCase):
    def test_helper_contains_expected_backend_flags(self):
        script = SCRIPT_PATH.read_text(encoding="utf-8")

        self.assertIn("https://api.135.181.245.84.sslip.io", script)
        self.assertIn("$Token = $env:TAKSKLAD_BACKEND_API_TOKEN", script)
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
            "TELEGRAM_BOT_TOKEN=",
            "SKLADBOT_API_TOKEN=",
        ]
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, script)


if __name__ == "__main__":
    unittest.main()
