import tempfile
import unittest
from pathlib import Path

from tools.check_desktop_api_contract import (
    FROZEN_DESKTOP_VERSION,
    FROZEN_ROUTES,
    discover_backend_client_calls,
    validate_contract,
)


ROOT = Path(__file__).resolve().parents[1]


class FrozenDesktopApiContractTests(unittest.TestCase):
    def test_desktop_2049_contract_is_satisfied(self):
        self.assertEqual(FROZEN_DESKTOP_VERSION, "2.0.50")
        self.assertEqual(validate_contract(ROOT), [])

    def test_contract_contains_real_warehouse_and_report_paths(self):
        required = {(route.method, route.path, route.scope) for route in FROZEN_ROUTES}
        self.assertIn(("GET", "/api/v1/orders/active", "orders:read"), required)
        self.assertIn(("POST", "/api/v1/scans", "scans:create"), required)
        self.assertIn(("POST", "/api/v1/orders/{order_id}/complete", "orders:complete"), required)
        self.assertIn(("GET", "/api/v1/reports/day", "orders:read"), required)
        self.assertIn(("GET", "/api/v1/returns/auth-canary/desktop", "returns:read"), required)
        self.assertIn(("POST", "/api/v1/returns/{order_id}", "returns:write"), required)

    def test_backend_client_discovery_normalizes_queries_and_path_parameters(self):
        source = '''\
def sample(order_id, query):
    backend_request("GET", f"/api/v1/reports/day{query}")
    backend_request("POST", f"/api/v1/orders/{order_id}/complete")
    backend_request("GET", f"/api/v1/returns?{query}")
'''
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "client.py"
            path.write_text(source, encoding="utf-8")
            self.assertEqual(
                discover_backend_client_calls(path),
                {
                    ("GET", "/api/v1/reports/day"),
                    ("POST", "/api/v1/orders/{order_id}/complete"),
                    ("GET", "/api/v1/returns"),
                },
            )

    def test_additive_backend_route_does_not_change_frozen_required_set(self):
        frozen = {(route.method, route.path) for route in FROZEN_ROUTES}
        backend_with_addition = frozen | {("GET", "/api/v1/server-only/additive")}
        self.assertTrue(frozen.issubset(backend_with_addition))


if __name__ == "__main__":
    unittest.main()
