import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class VersionEndpointTests(unittest.TestCase):
    def test_version_reports_exact_runtime_identity_without_dependency_probe(self):
        with patch.dict(
            os.environ,
            {
                "TAKSKLAD_COMMIT_SHA": "a" * 40,
                "TAKSKLAD_IMAGE_DIGEST": "sha256:" + "b" * 64,
            },
            clear=False,
        ):
            from backend.app.main import version

            payload = version()

        self.assertEqual(payload["version"], "2.0.34")
        self.assertEqual(payload["commit_sha"], "a" * 40)
        self.assertEqual(payload["image_digest"], "sha256:" + "b" * 64)
        self.assertNotIn("database", payload)

    def test_version_is_public_and_schema_bounded(self):
        from backend.app.main import app

        with patch.dict(
            os.environ,
            {
                "TAKSKLAD_COMMIT_SHA": "c" * 40,
                "TAKSKLAD_IMAGE_DIGEST": "sha256:" + "d" * 64,
            },
            clear=False,
        ):
            response = TestClient(app).get("/version")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.json()),
            {"service", "version", "commit_sha", "image_digest", "environment"},
        )


if __name__ == "__main__":
    unittest.main()
