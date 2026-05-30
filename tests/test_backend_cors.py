import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.main import configure_cors
from backend.app.settings import load_settings


class BackendCorsTests(unittest.TestCase):
    def test_configured_frontend_origin_can_call_api_with_bearer_header(self):
        test_app = FastAPI()

        @test_app.get("/api/v1/orders/active")
        def active_orders():
            return []

        settings = load_settings({
            "TAKSKLAD_CORS_ORIGINS": "https://app.example.com",
        })
        configure_cors(test_app, settings)
        client = TestClient(test_app)

        response = client.options(
            "/api/v1/orders/active",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "https://app.example.com")
        self.assertIn("GET", response.headers["access-control-allow-methods"])
        self.assertIn("Authorization", response.headers["access-control-allow-headers"])


if __name__ == "__main__":
    unittest.main()
