import unittest
from unittest import mock

from taksklad.update_service import (
    package_transition_required,
    select_update_download,
    validate_update_download_url,
    validate_update_sha256,
)


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

    def test_package_transition_required_only_for_frozen_onefile_to_onedir(self):
        update_info = {
            "package_type": "onedir_zip",
            "download_url_onedir": "https://github.com/1fear/TakSklad/releases/download/v2.0.23/TakSklad.zip",
        }

        with mock.patch("taksklad.update_service.sys.frozen", True, create=True), \
                mock.patch("taksklad.update_service.get_runtime_package_type", return_value="onefile"):
            self.assertTrue(package_transition_required(update_info))
            self.assertFalse(package_transition_required({"package_type": "onefile_exe"}))
            self.assertFalse(package_transition_required({"package_type": "onedir_zip"}))

        with mock.patch("taksklad.update_service.sys.frozen", True, create=True), \
                mock.patch("taksklad.update_service.get_runtime_package_type", return_value="onedir"):
            self.assertFalse(package_transition_required(update_info))

        with mock.patch("taksklad.update_service.sys.frozen", False, create=True), \
                mock.patch("taksklad.update_service.get_runtime_package_type", return_value="onefile"):
            self.assertFalse(package_transition_required(update_info))

    def test_select_update_download_uses_package_specific_url(self):
        update_info = {
            "package_type": "onefile_exe",
            "download_url": "https://github.com/1fear/TakSklad/releases/download/v2.0.23/TakSklad.exe",
            "sha256": "a" * 64,
            "download_url_onedir": "https://github.com/1fear/TakSklad/releases/download/v2.0.23/TakSklad-windows-x64.zip",
            "sha256_onedir": "b" * 64,
        }

        self.assertEqual(
            select_update_download(update_info),
            ("https://github.com/1fear/TakSklad/releases/download/v2.0.23/TakSklad.exe", "a" * 64),
        )

        self.assertEqual(
            select_update_download({**update_info, "package_type": "onedir_zip"}),
            ("https://github.com/1fear/TakSklad/releases/download/v2.0.23/TakSklad-windows-x64.zip", "b" * 64),
        )


if __name__ == "__main__":
    unittest.main()
