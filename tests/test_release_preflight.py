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
                "no_version_json_change": True,
                "no_github_release": True,
                "no_push_notifications": True,
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
                "stable 1.1.7\n"
            )
        return "ok"

    def test_preflight_passes_without_network_for_valid_fixture(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            summary = run_checks(root, health_url="https://example.invalid/health", timeout_seconds=1, skip_network=True)

        self.assertEqual(summary["status"], "ok")
        self.assertTrue(all(check["ok"] for check in summary["checks"]))

    def test_version_json_rejects_rollout_manifest_before_acceptance(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(
                    {
                        "latest_version": "2.0.0",
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
        self.assertIn("latest_version is not pinned to 1.1.7", check["problems"])
        self.assertIn("download_url fields must stay empty before rollout", check["problems"])

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


if __name__ == "__main__":
    unittest.main()
