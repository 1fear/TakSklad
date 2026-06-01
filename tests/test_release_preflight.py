import json
import tempfile
import unittest
from pathlib import Path

from tools.release_preflight import (
    ACCEPTANCE_DIR,
    MANIFEST_NAME,
    REQUIRED_FILES,
    VERSION_JSON,
    check_acceptance_kit,
    check_required_files,
    check_update_manifest_downloads,
    check_version_json,
    check_windows_acceptance_flow,
    run_checks,
    sha256_file,
)


class ReleasePreflightTests(unittest.TestCase):
    def make_root(self):
        tmp_dir = tempfile.TemporaryDirectory()
        root = Path(tmp_dir.name)
        for path in REQUIRED_FILES:
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.fixture_file_text(path), encoding="utf-8")
        (root / ACCEPTANCE_DIR).mkdir(parents=True, exist_ok=True)
        excel_path = root / ACCEPTANCE_DIR / "acceptance.xlsx"
        excel_path.write_bytes(b"acceptance")
        manifest = {
            "marker": "ACCEPTANCE TEST",
            "excel_file": "acceptance.xlsx",
            "result_template": "ACCEPTANCE_RESULTS_TEMPLATE.md",
            "result_file": "ACCEPTANCE_RESULTS.md",
            "excel_sha256": sha256_file(excel_path),
            "expected": {"orders": 1},
            "safety": {
                "version_json_staged_rollout": True,
                "github_release_published": True,
                "push_notifications_allowed": True,
                "mandatory_update_disabled": True,
                "contains_secrets": False,
            },
        }
        (root / ACCEPTANCE_DIR / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        (root / ACCEPTANCE_DIR / "ACCEPTANCE_RESULTS_TEMPLATE.md").write_text("template", encoding="utf-8")
        (root / ACCEPTANCE_DIR / "ACCEPTANCE_RESULTS.md").write_text("results", encoding="utf-8")
        (root / VERSION_JSON).write_text(
            json.dumps(
                {
                    "latest_version": "1.1.7",
                    "min_supported_version": "1.1.7",
                    "mandatory": False,
                    "download_url": "",
                    "download_url_onedir": "",
                    "package_type": "",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return tmp_dir, root

    def fixture_file_text(self, path):
        path_text = str(path).replace("\\", "/")
        if path_text.endswith("windows_backend_acceptance.ps1"):
            return (
                "build_manifest.json\n"
                "Cannot verify TakSklad.exe version\n"
                "$MinAppVersion = \"2.0.0\"\n"
                "$ExpectedBuildLabel = \"MVP 2.0\"\n"
                "APP_BUILD_LABEL\n"
                "app_build_label\n"
                "Compare-TakSkladVersion\n"
                "TAKSKLAD_BACKEND_ENABLED\n"
                "TAKSKLAD_BACKEND_READ_ORDERS_ENABLED\n"
            )
        if path_text.endswith("build_windows_test_archive.ps1"):
            return (
                "build_manifest.json\n"
                "$ExpectedBuildLabel = \"MVP 2.0\"\n"
                "APP_BUILD_LABEL\n"
                "app_build_label\n"
                "ACCEPTANCE_RESULTS_TEMPLATE.md\n"
                "ACCEPTANCE_RESULTS.md\n"
                "Assert-TestPackageDoesNotContainLocalSecrets\n"
                "version.json has local changes\n"
                "safe non-mandatory 2.0.1 rollout manifest\n"
            )
        return "ok"

    def test_preflight_passes_without_network_for_valid_fixture(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(
                    {
                        "latest_version": "2.0.1",
                        "min_supported_version": "1.1.7",
                        "mandatory": False,
                        "package_type": "onefile_exe",
                        "download_url": "https://github.com/1fear/TakSklad/releases/download/v2.0.1/TakSklad.exe",
                        "sha256": "a" * 64,
                        "download_url_onedir": "https://github.com/1fear/TakSklad/releases/download/v2.0.1/TakSklad-windows-x64.zip",
                        "sha256_onedir": "b" * 64,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            summary = run_checks(root, health_url="https://example.invalid/health", timeout_seconds=1, skip_network=True)

        self.assertEqual(summary["status"], "ok")
        self.assertTrue(all(check["ok"] for check in summary["checks"]))

    def test_version_json_rejects_bad_url_and_sha_format(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(
                    {
                        "latest_version": "2.0.1",
                        "min_supported_version": "1.1.7",
                        "mandatory": False,
                        "package_type": "onefile_exe",
                        "download_url": "http://example.com/TakSklad.exe",
                        "sha256": "A" * 64,
                        "download_url_onedir": "https://github.com/1fear/TakSklad/releases/download/v1.1.7/TakSklad.zip",
                        "sha256_onedir": "short",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            check = check_version_json(root)

        self.assertFalse(check["ok"])
        self.assertIn("download_url must be an HTTPS release URL for v2.0.1", check["problems"])
        self.assertIn("download_url_onedir must be an HTTPS release URL for v2.0.1", check["problems"])
        self.assertIn("sha256 must be a lowercase SHA256 hex digest", check["problems"])
        self.assertIn("sha256_onedir must be a lowercase SHA256 hex digest", check["problems"])

    def test_version_json_rejects_forced_or_incomplete_rollout_manifest(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(
                    {
                        "latest_version": "2.0.1",
                        "min_supported_version": "2.0.0",
                        "mandatory": True,
                        "download_url": "https://example.com/TakSklad.zip",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            check = check_version_json(root)

        self.assertFalse(check["ok"])
        self.assertIn("min_supported_version must stay 1.1.7 for non-forced rollout", check["problems"])
        self.assertIn("mandatory must be false during staged rollout", check["problems"])
        self.assertIn("onefile download_url and sha256 must be set", check["problems"])
        self.assertIn("onedir download_url_onedir and sha256_onedir must be set", check["problems"])

    def test_verify_downloads_hashes_update_artifacts(self):
        tmp_dir, root = self.make_root()
        onefile = b"onefile artifact"
        onedir = b"onedir artifact"
        onefile_sha = sha256_file(self.write_bytes(root / "onefile.bin", onefile))
        onedir_sha = sha256_file(self.write_bytes(root / "onedir.bin", onedir))
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(
                    {
                        "download_url": "https://github.com/1fear/TakSklad/releases/download/v2.0.1/TakSklad.exe",
                        "sha256": onefile_sha,
                        "download_url_onedir": "https://github.com/1fear/TakSklad/releases/download/v2.0.1/TakSklad-windows-x64.zip",
                        "sha256_onedir": onedir_sha,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            class FakeResponse:
                def __init__(self, chunks):
                    self.chunks = list(chunks)

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self, _size):
                    if not self.chunks:
                        return b""
                    return self.chunks.pop(0)

            def fake_urlopen(request, timeout):
                self.assertEqual(timeout, 3)
                url = request.full_url
                if url.endswith("TakSklad.exe"):
                    return FakeResponse([onefile])
                if url.endswith("TakSklad-windows-x64.zip"):
                    return FakeResponse([onedir])
                raise AssertionError(url)

            import unittest.mock

            with unittest.mock.patch("tools.release_preflight.urllib.request.urlopen", side_effect=fake_urlopen):
                check = check_update_manifest_downloads(root, timeout_seconds=3)

        self.assertTrue(check["ok"], check.get("problems"))
        self.assertEqual([asset["actual_sha256"] for asset in check["assets"]], [onefile_sha, onedir_sha])

    def test_verify_downloads_reports_sha_mismatch(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(
                    {
                        "download_url": "https://github.com/1fear/TakSklad/releases/download/v2.0.1/TakSklad.exe",
                        "sha256": "a" * 64,
                        "download_url_onedir": "https://github.com/1fear/TakSklad/releases/download/v2.0.1/TakSklad-windows-x64.zip",
                        "sha256_onedir": "b" * 64,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self, _size):
                    if getattr(self, "done", False):
                        return b""
                    self.done = True
                    return b"different"

            import unittest.mock

            with unittest.mock.patch("tools.release_preflight.urllib.request.urlopen", return_value=FakeResponse()):
                check = check_update_manifest_downloads(root, timeout_seconds=3)

        self.assertFalse(check["ok"])
        self.assertIn("onefile SHA mismatch", check["problems"])
        self.assertIn("onedir SHA mismatch", check["problems"])

    def test_acceptance_kit_rejects_sha_mismatch(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / ACCEPTANCE_DIR / "acceptance.xlsx").write_bytes(b"changed")
            check = check_acceptance_kit(root)

        self.assertFalse(check["ok"])
        self.assertIn("acceptance Excel SHA mismatch", check["problems"])

    def test_acceptance_kit_requires_result_template(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / ACCEPTANCE_DIR / "ACCEPTANCE_RESULTS_TEMPLATE.md").unlink()
            check = check_acceptance_kit(root)

        self.assertFalse(check["ok"])
        self.assertIn("acceptance result template not found", check["problems"])

    def test_acceptance_kit_requires_result_file(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / ACCEPTANCE_DIR / "ACCEPTANCE_RESULTS.md").unlink()
            check = check_acceptance_kit(root)

        self.assertFalse(check["ok"])
        self.assertIn("acceptance result file not found", check["problems"])

    def test_required_files_are_checked(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            missing_path = root / REQUIRED_FILES[0]
            missing_path.unlink()
            check = check_required_files(root)

        self.assertFalse(check["ok"])
        self.assertIn(str(REQUIRED_FILES[0]), check["missing"])

    def test_windows_acceptance_flow_requires_manifest_guard(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / "tools/windows_backend_acceptance.ps1").write_text(
                "TAKSKLAD_BACKEND_ENABLED\n"
                "TAKSKLAD_BACKEND_READ_ORDERS_ENABLED\n"
                "$MinAppVersion = \"2.0.0\"\n",
                encoding="utf-8",
            )
            check = check_windows_acceptance_flow(root)

        self.assertFalse(check["ok"])
        self.assertIn(
            "windows acceptance helper missing fragment: build_manifest.json",
            check["problems"],
        )

    def test_windows_acceptance_flow_passes_for_current_scripts(self):
        check = check_windows_acceptance_flow(Path(__file__).resolve().parents[1])

        self.assertTrue(check["ok"], check.get("problems"))

    def write_bytes(self, path, content):
        path.write_bytes(content)
        return path


if __name__ == "__main__":
    unittest.main()
