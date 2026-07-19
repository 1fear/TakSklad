import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.release_preflight import (
    ACCEPTANCE_DIR,
    MANIFEST_NAME,
    PHASE_CANDIDATE,
    PHASE_FINAL,
    configured_app_version,
    REQUIRED_FILES,
    VERSION_JSON,
    check_acceptance_kit,
    check_deploy_runbook_contract,
    check_deployment_readiness_contract,
    check_required_files,
    check_update_manifest_downloads,
    check_version_json,
    check_windows_acceptance_flow,
    previous_patch_version,
    run_checks,
    sha256_file,
)

EXPECTED_RELEASE_VERSION = configured_app_version()
EXPECTED_RELEASE_TAG = f"v{EXPECTED_RELEASE_VERSION}"


def release_url(asset):
    return f"https://github.com/1fear/TakSklad/releases/download/{EXPECTED_RELEASE_TAG}/{asset}"


class ReleasePreflightTests(unittest.TestCase):
    def final_version_manifest(self, **overrides):
        value = {
            "latest_version": EXPECTED_RELEASE_VERSION,
            "release_tag": EXPECTED_RELEASE_TAG,
            "min_supported_version": EXPECTED_RELEASE_VERSION,
            "mandatory": True,
            "block_workflow": True,
            "package_type": "onefile_exe",
            "download_url": release_url("TakSklad.exe"),
            "sha256": "a" * 64,
            "download_url_onedir": release_url("TakSklad-windows-x64.zip"),
            "sha256_onedir": "b" * 64,
            "auth_helper": "TakSkladAuth.exe",
            "auth_helper_download_url": release_url("TakSkladAuth.exe"),
            "auth_helper_sha256": "c" * 64,
            "signature_type": "authenticode",
            "signature_required": True,
            "signer_certificate_sha256": "d" * 64,
            "source_sha": "e" * 40,
            "dependency_lock_sha256": "f" * 64,
        }
        value.update(overrides)
        return value

    def make_root(self):
        tmp_dir = tempfile.TemporaryDirectory()
        root = Path(tmp_dir.name)
        for path in REQUIRED_FILES:
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.fixture_file_text(path), encoding="utf-8")
            if path == Path("tools/verify_release_attestations.sh"):
                target.chmod(0o755)
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
                    "latest_version": "2.0.40",
                    "release_tag": "v2.0.40",
                    "min_supported_version": "2.0.40",
                    "mandatory": True,
                    "block_workflow": True,
                    "package_type": "onefile_exe",
                    "download_url": "https://github.com/1fear/TakSklad/releases/download/v2.0.40/TakSklad.exe",
                    "sha256": "a" * 64,
                    "download_url_onedir": "https://github.com/1fear/TakSklad/releases/download/v2.0.40/TakSklad-windows-x64.zip",
                    "sha256_onedir": "b" * 64,
                    "signature_type": "authenticode",
                    "signature_required": True,
                    "signer_certificate_sha256": "c" * 64,
                    "source_sha": "d" * 40,
                    "dependency_lock_sha256": "e" * 64,
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
                "Cannot verify TakSklad.exe because its signed package build_manifest.json was not found.\n"
                "TakSkladAuth.exe\n"
                "$PinnedProductionSignerCertificateSha256\n"
                "$MinAppVersion = \"2.0.0\"\n"
                "$ExpectedBuildLabel = \"MVP 2.0\"\n"
                "APP_BUILD_LABEL\n"
                "app_build_label\n"
                "Compare-TakSkladVersion\n"
                "TAKSKLAD_BACKEND_ENABLED\n"
                "TAKSKLAD_BACKEND_READ_ORDERS_ENABLED\n"
                "TAKSKLAD_BACKEND_ONLY_REFRESH\n"
                "TELEGRAM_DESKTOP_POLLING_ENABLED\n"
            )
        if path_text.endswith("tools/verify_release_attestations.sh"):
            return "#!/usr/bin/env bash\nexit 0\n"
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
                "paused 1.1.7 nor forced $MinAppVersion rollout manifest\n"
            )
        if path_text.endswith("src/taksklad/config.py"):
            return (
                "TAKSKLAD_BACKEND_ENABLED = True\n"
                "TAKSKLAD_BACKEND_READ_ORDERS_ENABLED = True\n"
                "TAKSKLAD_BACKEND_ONLY_REFRESH = True\n"
                "TELEGRAM_DESKTOP_POLLING_ENABLED = False\n"
            )
        if path_text.endswith("src/taksklad/startup_check.py"):
            return "telegram_desktop_polling backend_only_refresh pending_backend_events\n"
        if path_text.endswith("src/taksklad/desktop_refresh_service.py"):
            return (
                "def backend_only_refresh_enabled(): pass\n"
                "Backend refresh недоступен\n"
                "pending_backend_events\n"
            )
        if path_text.endswith("src/taksklad/app_runtime.py"):
            return "if getattr(self, \"telegram_lock_owned_until\", 0) > time.time(): release_telegram_poll_lock()\n"
        if path_text.endswith("src/taksklad/desktop_diagnostics.py"):
            return "primary_source backend_only_refresh pending_backend_events\n"
        if path_text.endswith("backend/app/operations_service.py"):
            return "shadow_diagnostics backend_active_orders_source hot_path_stale_processing telegram_worker_state\n"
        if path_text.endswith("backend/app/health_service.py"):
            return (
                'EXPECTED_HEAD_REVISION = "20260719_0020"\n'
                'report["ready"] = True\n'
                'report["status"] = "unhealthy"\n'
            )
        if path_text.endswith("deploy/vds/docker-compose.yml"):
            return (
                "http://127.0.0.1:8000/health\n"
                "payload.get('status') == 'ok'\n"
                "payload.get('service') == expected_service\n"
                "payload.get('commit_sha') == expected_sha\n"
                "payload.get('image_digest') == expected_digest\n"
                "json.load(response)\n"
            )
        if path_text.endswith("deploy/vds/deploy_from_git.sh"):
            return (
                "tools/release_artifacts.py\n"
                "tools/server_release_artifacts.py\n"
                "alembic -c alembic.ini upgrade head\n"
                "--no-build --pull never\n"
                "--wait --wait-timeout\n"
                '--expected-sha "$RELEASE_SOURCE_SHA"\n'
                '--expected-digest "$RELEASE_BACKEND_DIGEST"\n'
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
                "TakSkladAuth.exe\n"
                "/api/v1/returns/auth-canary/desktop\n"
                f"{EXPECTED_RELEASE_VERSION} candidate; public channel remains separately verified\n"
            )
        if path_text.endswith("docs/deploy-rollback-runbook.md"):
            return (
                "release.json image@sha256 current-release alembic downgrade\n"
            )
        if path_text.endswith("docs/manual-acceptance-runbook.md"):
            return (
                f"--phase candidate --phase final {EXPECTED_RELEASE_VERSION} public channel TakSkladAuth.exe\n"
            )
        return "ok"

    def test_preflight_passes_without_network_for_valid_fixture(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(self.final_version_manifest(), ensure_ascii=False),
                encoding="utf-8",
            )
            summary = run_checks(
                root,
                health_url="https://example.invalid/health",
                timeout_seconds=1,
                phase=PHASE_CANDIDATE,
                skip_network=True,
            )

        self.assertEqual(summary["status"], "ok")
        self.assertTrue(all(check["ok"] for check in summary["checks"]))

    def test_current_deployment_readiness_contract_is_fail_closed(self):
        check = check_deployment_readiness_contract(Path(__file__).resolve().parents[1])

        self.assertTrue(check["ok"], check.get("problems"))

    def test_candidate_accepts_current_supported_published_channel(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            check = check_version_json(root, phase=PHASE_CANDIDATE)

        self.assertTrue(check["ok"])
        self.assertEqual(check["rollout_state"], "published-supported")
        self.assertEqual(check["candidate_version"], EXPECTED_RELEASE_VERSION)

    def test_version_json_rejects_bad_url_and_sha_format(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(
                    self.final_version_manifest(
                        download_url="http://example.com/TakSklad.exe",
                        sha256="A" * 64,
                        download_url_onedir="https://github.com/1fear/TakSklad/releases/download/v1.1.7/TakSklad.zip",
                        sha256_onedir="short",
                        auth_helper_download_url="http://example.com/TakSkladAuth.exe",
                        auth_helper_sha256="short",
                    ),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            check = check_version_json(root, phase=PHASE_FINAL, source_sha="e" * 40)

        self.assertFalse(check["ok"])
        self.assertIn(
            f"download_url must be an HTTPS release URL for {EXPECTED_RELEASE_TAG}",
            check["problems"],
        )
        self.assertIn(
            f"download_url_onedir must be an HTTPS release URL for {EXPECTED_RELEASE_TAG}",
            check["problems"],
        )
        self.assertIn("sha256 must be a lowercase SHA256 hex digest", check["problems"])
        self.assertIn("sha256_onedir must be a lowercase SHA256 hex digest", check["problems"])

    def test_version_json_rejects_release_url_from_wrong_host(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(
                    self.final_version_manifest(
                        download_url=f"https://mirror.example.com/1fear/TakSklad/releases/download/{EXPECTED_RELEASE_TAG}/TakSklad.exe"
                    ),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            check = check_version_json(root, phase=PHASE_FINAL, source_sha="e" * 40)

        self.assertFalse(check["ok"])
        self.assertIn(
            f"download_url must be an HTTPS release URL for {EXPECTED_RELEASE_TAG}",
            check["problems"],
        )

    def test_version_json_rejects_invalid_rollout_manifest(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(
                    self.final_version_manifest(
                        min_supported_version="2.0.40",
                        mandatory=False,
                        block_workflow=False,
                    ),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            check = check_version_json(root, phase=PHASE_FINAL, source_sha="e" * 40)

        self.assertFalse(check["ok"])
        self.assertIn(f"final channel must require exact {EXPECTED_RELEASE_VERSION}", check["problems"])
        self.assertIn("final channel must be mandatory and block unsupported workflows", check["problems"])

    def test_verify_downloads_hashes_update_artifacts(self):
        tmp_dir, root = self.make_root()
        onefile = b"onefile artifact"
        onedir = b"onedir artifact"
        auth_helper = b"auth helper artifact"
        onefile_sha = sha256_file(self.write_bytes(root / "onefile.bin", onefile))
        onedir_sha = sha256_file(self.write_bytes(root / "onedir.bin", onedir))
        auth_helper_sha = sha256_file(self.write_bytes(root / "auth-helper.bin", auth_helper))
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(
                    {
                        "package_type": "onedir_zip",
                        "download_url": release_url("TakSklad.exe"),
                        "sha256": onefile_sha,
                        "download_url_onedir": release_url("TakSklad-windows-x64.zip"),
                        "sha256_onedir": onedir_sha,
                        "auth_helper": "TakSkladAuth.exe",
                        "auth_helper_download_url": release_url("TakSkladAuth.exe"),
                        "auth_helper_sha256": auth_helper_sha,
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
                if url.endswith("TakSkladAuth.exe"):
                    return FakeResponse([auth_helper])
                raise AssertionError(url)

            import unittest.mock

            with unittest.mock.patch("tools.release_preflight.urllib.request.urlopen", side_effect=fake_urlopen):
                check = check_update_manifest_downloads(root, timeout_seconds=3)

        self.assertTrue(check["ok"], check.get("problems"))
        self.assertEqual(
            [asset["actual_sha256"] for asset in check["assets"]],
            [onefile_sha, onedir_sha, auth_helper_sha],
        )

    def test_verify_downloads_reports_sha_mismatch(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(
                    {
                        "package_type": "onedir_zip",
                        "download_url": release_url("TakSklad.exe"),
                        "sha256": "a" * 64,
                        "download_url_onedir": release_url("TakSklad-windows-x64.zip"),
                        "sha256_onedir": "b" * 64,
                        "auth_helper": "TakSkladAuth.exe",
                        "auth_helper_download_url": release_url("TakSkladAuth.exe"),
                        "auth_helper_sha256": "c" * 64,
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
        self.assertIn("auth_helper SHA mismatch", check["problems"])

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

    def test_backend_only_contract_rejects_missing_offline_queue_guard(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / "src/taksklad/desktop_refresh_service.py").write_text(
                "def backend_only_refresh_enabled(): pass\n"
                "Backend refresh недоступен\n",
                encoding="utf-8",
            )
            summary = run_checks(
                root,
                health_url="https://example.invalid/health",
                timeout_seconds=1,
                phase=PHASE_CANDIDATE,
                skip_network=True,
            )

        backend_contract = next(check for check in summary["checks"] if check["name"] == "backend_only_hot_path_contract")
        self.assertFalse(backend_contract["ok"])
        self.assertIn(
            "src/taksklad/desktop_refresh_service.py: missing fragment: pending_backend_events",
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
            summary = run_checks(
                root,
                health_url="https://example.invalid/health",
                timeout_seconds=1,
                phase=PHASE_CANDIDATE,
                skip_network=True,
            )

        deploy_contract = next(check for check in summary["checks"] if check["name"] == "deploy_runbook_contract")
        self.assertFalse(deploy_contract["ok"])
        self.assertIn(
            "docs/deploy-rollback-runbook.md: missing fragment: release.json",
            deploy_contract["problems"],
        )

    def test_deploy_runbook_contract_rejects_stale_previous_release_version(self):
        tmp_dir, root = self.make_root()
        stale_version = previous_patch_version(EXPECTED_RELEASE_VERSION)
        with tmp_dir:
            path = root / "docs/windows-backend-acceptance.md"
            path.write_text(path.read_text(encoding="utf-8") + f"\n{stale_version}\n", encoding="utf-8")

            check = check_deploy_runbook_contract(root)

        self.assertFalse(check["ok"])
        self.assertIn(
            f"docs/windows-backend-acceptance.md: stale operator release version: {stale_version}",
            check["problems"],
        )

    def test_windows_acceptance_flow_passes_for_current_scripts(self):
        check = check_windows_acceptance_flow(Path(__file__).resolve().parents[1])

        self.assertTrue(check["ok"], check.get("problems"))

    def test_final_version_contract_requires_exact_candidate_channel(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(self.final_version_manifest()), encoding="utf-8"
            )
            check = check_version_json(root, phase=PHASE_FINAL, source_sha="e" * 40)

        self.assertTrue(check["ok"], check.get("problems"))
        self.assertEqual(check["rollout_state"], "final-published")

    def test_final_version_contract_rejects_unsupported_package_type(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(self.final_version_manifest(package_type="msix")),
                encoding="utf-8",
            )
            check = check_version_json(root, phase=PHASE_FINAL, source_sha="e" * 40)

        self.assertFalse(check["ok"])
        self.assertIn("package_type must be onefile_exe or onedir_zip", check["problems"])
        self.assertIn("final package_type must be onefile_exe", check["problems"])

    def test_final_version_contract_rejects_onedir_public_channel(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(self.final_version_manifest(package_type="onedir_zip")),
                encoding="utf-8",
            )
            check = check_version_json(root, phase=PHASE_FINAL, source_sha="e" * 40)

        self.assertFalse(check["ok"])
        self.assertNotIn("package_type must be onefile_exe or onedir_zip", check["problems"])
        self.assertIn("final package_type must be onefile_exe", check["problems"])

    def test_final_version_contract_rejects_published_source_sha_mismatch(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            (root / VERSION_JSON).write_text(
                json.dumps(self.final_version_manifest(source_sha="e" * 40)),
                encoding="utf-8",
            )
            check = check_version_json(
                root,
                phase=PHASE_FINAL,
                source_sha="f" * 40,
            )

        self.assertFalse(check["ok"])
        self.assertIn(
            "published source_sha must match the explicit attested source SHA",
            check["problems"],
        )

    def test_final_run_contract_blocks_offline_or_unverified_invocation(self):
        tmp_dir, root = self.make_root()
        with tmp_dir:
            summary = run_checks(
                root,
                health_url="https://example.invalid/health",
                timeout_seconds=1,
                phase=PHASE_FINAL,
                skip_network=True,
                verify_downloads=False,
                source_sha=None,
            )

        phase = next(item for item in summary["checks"] if item["name"] == "phase_contract")
        attestation = next(
            item for item in summary["checks"] if item["name"] == "release_attestations"
        )
        self.assertFalse(phase["ok"])
        self.assertFalse(attestation["ok"])
        self.assertTrue(attestation["skipped"])

    def test_real_repo_root_candidate_preflight_is_green_without_network(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [
                sys.executable,
                str(root / "tools/release_preflight.py"),
                "--root",
                str(root),
                "--phase",
                PHASE_CANDIDATE,
                "--skip-network",
            ],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        payload = json.loads(completed.stdout)
        version = next(item for item in payload["checks"] if item["name"] == "version_json")
        published = tuple(int(part) for part in version["latest_version"].split("."))
        candidate = tuple(int(part) for part in EXPECTED_RELEASE_VERSION.split("."))
        self.assertLess(published, candidate)
        self.assertEqual(version["candidate_version"], EXPECTED_RELEASE_VERSION)
        self.assertEqual(version["rollout_state"], "published-supported")

    def write_bytes(self, path, content):
        path.write_bytes(content)
        return path


if __name__ == "__main__":
    unittest.main()
