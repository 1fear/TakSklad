import unittest

from tools.verify_postgres_only_cutover import verified_marker


class VerifyPostgresOnlyCutoverTests(unittest.TestCase):
    def setUp(self):
        self.sha = "a" * 40
        self.digest = "sha256:" + "b" * 64
        self.deployment = {
            "source_sha": self.sha,
            "images": {"backend": {"digest": self.digest}},
        }
        self.version = {
            "commit_sha": self.sha,
            "image_digest": self.digest,
            "environment": "production",
        }

    def test_accepts_matching_signed_runtime_identity(self):
        marker = verified_marker(self.deployment, self.version)
        self.assertEqual(marker["mode"], "already_postgres_only")
        self.assertTrue(marker["safe_to_cutover"])
        self.assertEqual(marker["blockers"], 0)

    def test_rejects_source_sha_mismatch(self):
        self.version["commit_sha"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "source SHA"):
            verified_marker(self.deployment, self.version)

    def test_rejects_backend_digest_mismatch(self):
        self.version["image_digest"] = "sha256:" + "c" * 64
        with self.assertRaisesRegex(ValueError, "backend digest"):
            verified_marker(self.deployment, self.version)

    def test_rejects_non_production_runtime(self):
        self.version["environment"] = "development"
        with self.assertRaisesRegex(ValueError, "production"):
            verified_marker(self.deployment, self.version)


if __name__ == "__main__":
    unittest.main()
