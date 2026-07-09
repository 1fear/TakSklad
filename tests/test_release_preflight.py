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
    check_deployment_readiness_contract,
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
                "mandatory_update_enabled": True,
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
                "TAKSKLAD_BACKEND_ONLY_REFRESH\n"
                "TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED\n"
                "TELEGRAM_DESKTOP_POLLING_ENABLED\n"
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
                "paused 1.1.7 nor forced 2.0.25 rollout manifest\n"
            )
        if path_text.endswith("src/taksklad/config.py"):
            return (
                "TAKSKLAD_BACKEND_ONLY_REFRESH = _bool_setting(RUNTIME_CONFIG, \"TAKSKLAD_BACKEND_ONLY_REFRESH\", default=False)\n"
                "TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED = _bool_setting(RUNTIME_CONFIG, \"TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED\", default=False)\n"
                "TELEGRAM_DESKTOP_POLLING_ENABLED = _bool_setting(RUNTIME_CONFIG, \"TELEGRAM_DESKTOP_POLLING_ENABLED\", default=False)\n"
            )
        if path_text.endswith("src/taksklad/startup_check.py"):
            return "telegram_desktop_polling backend_only_refresh backend_emergency_google_fallback\n"
        if path_text.endswith("src/taksklad/desktop_refresh_service.py"):
            return (
                "def backend_only_refresh_enabled(): pass\n"
                "def backend_google_fallback_enabled(): pass\n"
                "TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED\n"
                "Backend refresh недоступен\n"
                "google_emergency_fallback\n"
            )
        if path_text.endswith("src/taksklad/app_runtime.py"):
            return "if getattr(self, \"telegram_lock_owned_until\", 0) > time.time(): release_telegram_poll_lock()\n"
        if path_text.endswith("src/taksklad/desktop_diagnostics.py"):
            return "primary_source backend_only_refresh emergency_google_fallback google_mirror_pending_exports\n"
        if path_text.endswith("backend/app/operations_service.py"):
            return "shadow_diagnostics backend_active_orders_source google_mirror_lag_seconds hot_path_stale_processing telegram_worker_state\n"
        if path_text.endswith("backend/app/health_service.py"):
            return (
                'EXPECTED_HEAD_REVISION = "20260710_0008"\n'
                'report["ready"] = True\n'
                'report["status"] = "unhealthy"\n'
                'report["status"] = "degraded"\n'
            )
        if path_text.endswith("deploy/vds/docker-compose.yml"):
            return "payload.get('ready') is True json.load(response)\n"
        if path_text.endswith("deploy/vds/deploy_from_git.sh"):
            return (
                "verify_migration_revision_before_activation\n"
                "--wait --wait-timeout\n"
                "readiness body contract failed\n"
                "acceptance_status.sh --require-go\n"
                "TAKSKLAD_DEPLOY_ACCEPTANCE:-required\n"
                "tools/validate_deploy_probe.py\n"
            )
        if path_text.endswith("tools/validate_deploy_probe.py"):
            return (
                "readiness database contract failed\n"
                "readiness migration revision failed\n"
                "readiness mandatory policy failed\n"
            )
        if path_text.endswith("docs/windows-backend-acceptance.md"):
            return (
                'TAKSKLAD_BACKEND_ONLY_REFRESH = "1"\n'
                'TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED = "0"\n'
                'TELEGRAM_DESKTOP_POLLING_ENABLED = "0"\n'
                "pending_backend_events pending_saves pending_prints pending_telegram /api/v1/admin/operations\n"
            )
        if path_text.endswith("docs/deploy-rollback-runbook.md"):
            return (
                "restore point\n"
                "git status --short\n"
                "Do not run broad rsync from a dirty tree\n"
                "selective deploy\n"
                "pending events\n"
                "/api/v1/admin/operations\n"
            )
        if path_text.endswith("docs/manual-acceptance-runbook.md"):
            return "startup diagnostics backend refresh network timeout Google 429 dirty tree\n"
        return "ok"

    def test_preflight_passes_without_network_for_valid_fixture(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(
                    {
                        "latest_version": "2.0.25",
                        "min_supported_version": "2.0.25",
                        "mandatory": True,
                        "package_type": "onefile_exe",
                        "download_url": "https://github.com/1fear/TakSklad/releases/download/v2.0.25/TakSklad.exe",
                        "sha256": "a" * 64,
                        "download_url_onedir": "https://github.com/1fear/TakSklad/releases/download/v2.0.25/TakSklad-windows-x64.zip",
                        "sha256_onedir": "b" * 64,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            summary = run_checks(root, health_url="https://example.invalid/health", timeout_seconds=1, skip_network=True)

        self.assertEqual(summary["status"], "ok")
        self.assertTrue(all(check["ok"] for check in summary["checks"]))

    def test_current_deployment_readiness_contract_is_fail_closed(self):
        check = check_deployment_readiness_contract(Path(__file__).resolve().parents[1])

        self.assertTrue(check["ok"], check.get("problems"))

    def test_version_json_accepts_paused_rollout_manifest(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            check = check_version_json(root)

        self.assertTrue(check["ok"])
        self.assertEqual(check["rollout_state"], "paused")

    def test_version_json_rejects_bad_url_and_sha_format(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(
                    {
                        "latest_version": "2.0.25",
                        "min_supported_version": "2.0.25",
                        "mandatory": True,
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
        self.assertIn("download_url must be an HTTPS release URL for v2.0.25", check["problems"])
        self.assertIn("download_url_onedir must be an HTTPS release URL for v2.0.25", check["problems"])
        self.assertIn("sha256 must be a lowercase SHA256 hex digest", check["problems"])
        self.assertIn("sha256_onedir must be a lowercase SHA256 hex digest", check["problems"])

    def test_version_json_rejects_release_url_from_wrong_host(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(
                    {
                        "latest_version": "2.0.25",
                        "min_supported_version": "2.0.25",
                        "mandatory": True,
                        "package_type": "onefile_exe",
                        "download_url": "https://mirror.example.com/1fear/TakSklad/releases/download/v2.0.25/TakSklad.exe",
                        "sha256": "a" * 64,
                        "download_url_onedir": "https://github.com/1fear/TakSklad/releases/download/v2.0.25/TakSklad-windows-x64.zip",
                        "sha256_onedir": "b" * 64,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            check = check_version_json(root)

        self.assertFalse(check["ok"])
        self.assertIn("download_url must be an HTTPS release URL for v2.0.25", check["problems"])

    def test_version_json_rejects_invalid_rollout_manifest(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(
                    {
                        "latest_version": "2.0.25",
                        "min_supported_version": "1.1.7",
                        "mandatory": False,
                        "download_url": "https://example.com/TakSklad.zip",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            check = check_version_json(root)

        self.assertFalse(check["ok"])
        self.assertIn("version.json must be either paused 1.1.7 rollout or forced 2.0.25 rollout", check["problems"])
        self.assertEqual(check["rollout_state"], "invalid")

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
                        "download_url": "https://github.com/1fear/TakSklad/releases/download/v2.0.25/TakSklad.exe",
                        "sha256": onefile_sha,
                        "download_url_onedir": "https://github.com/1fear/TakSklad/releases/download/v2.0.25/TakSklad-windows-x64.zip",
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
                        "download_url": "https://github.com/1fear/TakSklad/releases/download/v2.0.25/TakSklad.exe",
                        "sha256": "a" * 64,
                        "download_url_onedir": "https://github.com/1fear/TakSklad/releases/download/v2.0.25/TakSklad-windows-x64.zip",
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

    def test_backend_only_contract_rejects_missing_emergency_guard(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / "src/taksklad/desktop_refresh_service.py").write_text(
                "def backend_only_refresh_enabled(): pass\n"
                "Backend refresh недоступен\n",
                encoding="utf-8",
            )
            summary = run_checks(root, health_url="https://example.invalid/health", timeout_seconds=1, skip_network=True)

        backend_contract = next(check for check in summary["checks"] if check["name"] == "backend_only_hot_path_contract")
        self.assertFalse(backend_contract["ok"])
        self.assertIn(
            "src/taksklad/desktop_refresh_service.py: missing fragment: TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED",
            backend_contract["problems"],
        )

    def test_deploy_runbook_contract_rejects_dirty_tree_broad_deploy_gap(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / "docs/deploy-rollback-runbook.md").write_text(
                "restore point\n"
                "pending events\n"
                "/api/v1/admin/operations\n",
                encoding="utf-8",
            )
            summary = run_checks(root, health_url="https://example.invalid/health", timeout_seconds=1, skip_network=True)

        deploy_contract = next(check for check in summary["checks"] if check["name"] == "deploy_runbook_contract")
        self.assertFalse(deploy_contract["ok"])
        self.assertIn(
            "docs/deploy-rollback-runbook.md: missing fragment: Do not run broad rsync from a dirty tree",
            deploy_contract["problems"],
        )

    def test_windows_acceptance_flow_passes_for_current_scripts(self):
        check = check_windows_acceptance_flow(Path(__file__).resolve().parents[1])

        self.assertTrue(check["ok"], check.get("problems"))

    def write_bytes(self, path, content):
        path.write_bytes(content)
        return path


if __name__ == "__main__":
    unittest.main()
