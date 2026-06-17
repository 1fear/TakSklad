import unittest

from taksklad.update_service import validate_update_download_url, validate_update_sha256


class UpdateServiceTests(unittest.TestCase):
    def test_update_download_url_accepts_github_release_asset(self):
        validate_update_download_url(
            "https://github.com/1fear/TakSklad/releases/download/v2.0.15/TakSklad.exe"
        )

    def test_update_download_url_rejects_insecure_or_wrong_host(self):
        bad_urls = [
            "http://github.com/1fear/TakSklad/releases/download/v2.0.15/TakSklad.exe",
            "https://mirror.example.com/1fear/TakSklad/releases/download/v2.0.15/TakSklad.exe",
            "https://github.com/other/TakSklad/releases/download/v2.0.15/TakSklad.exe",
            "https://user:pass@github.com/1fear/TakSklad/releases/download/v2.0.15/TakSklad.exe",
        ]

        for url in bad_urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_update_download_url(url)

    def test_update_sha256_rejects_bad_shape(self):
        validate_update_sha256("a" * 64)

        for checksum in ("", "A" * 64, "short", "g" * 64):
            with self.subTest(checksum=checksum):
                if not checksum:
                    validate_update_sha256(checksum)
                else:
                    with self.assertRaises(ValueError):
                        validate_update_sha256(checksum)


if __name__ == "__main__":
    unittest.main()
