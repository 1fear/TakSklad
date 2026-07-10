from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FrontendCsrfContractTests(unittest.TestCase):
    def test_api_transport_separates_cookie_and_bearer(self):
        source = (ROOT / "frontend/src/api.ts").read_text(encoding="utf-8")

        self.assertIn('credentials: bearerRequest ? "omit" : "same-origin"', source)
        self.assertIn('"X-TakSklad-CSRF": config.csrfToken', source)
        self.assertIn("ensureCookieApiIsSameOrigin", source)
        self.assertGreaterEqual(source.count("ensureCookieApiIsSameOrigin"), 3)
        self.assertIn('credentials: config.token ? "omit" : "same-origin"', source)

    def test_app_persists_and_clears_csrf_token_in_memory(self):
        source = (ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")

        self.assertIn("csrfToken: session.csrf_token", source)
        self.assertGreaterEqual(source.count('csrfToken: ""'), 3)
        self.assertIn("Ограниченный доступ", source)
        self.assertIn("csrf_invalid", source)
        self.assertIn("origin_denied", source)
        self.assertIn("accessibleTabsForPermissions", source)
        self.assertIn("Нет доступных разделов", source)
        self.assertIn("showActionError(panelError", source)
        self.assertIn('accessibleTabs.includes("table") && tab === "table"', source)
        self.assertIn('accessibleTabs.includes("activity") && tab === "activity"', source)
        self.assertNotIn("listClientPoints(config).catch(() => [])", source)


if __name__ == "__main__":
    unittest.main()
