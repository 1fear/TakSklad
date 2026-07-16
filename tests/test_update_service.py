import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from backend.app.settings import APP_VERSION as BACKEND_APP_VERSION
from taksklad.config import APP_VERSION
from taksklad.update_service import (
    MAX_UPDATE_DOWNLOAD_BYTES,
    TRUSTED_WINDOWS_SIGNER_CERT_SHA256,
    WINDOWS_AUTHENTICODE_PINNED_CHAIN_STATUSES,
    WINDOWS_AUTHENTICODE_PINNED_STATUSES,
    create_windows_exe_updater,
    create_windows_onedir_updater,
    download_update_file,
    package_transition_required,
    select_update_download,
    validate_update_manifest,
    validate_update_download_url,
    validate_update_sha256,
    verify_windows_authenticode_signature,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_SIGNER_CERT_SHA256 = "1" * 64


class FakeDownloadResponse(io.BytesIO):
    def __init__(self, payload, *, content_length=None):
        super().__init__(payload)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False


class UpdateServiceTests(unittest.TestCase):
    def _write_onedir_zip(self, zip_path):
        with zipfile.ZipFile(zip_path, "w") as zip_file:
            zip_file.writestr("TakSklad/TakSklad.exe", "fake exe")
            zip_file.writestr("TakSklad/lib/module.pyd", "fake module")

    def _release_manifest(self, payload=b"signed synthetic exe", **overrides):
        manifest = {
            "latest_version": "9.8.7",
            "release_tag": "v9.8.7",
            "package_type": "onefile_exe",
            "download_url": "https://github.com/1fear/TakSklad/releases/download/v9.8.7/TakSklad.exe",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "signature_type": "authenticode",
            "signature_required": True,
            "signer_certificate_sha256": SYNTHETIC_SIGNER_CERT_SHA256,
        }
        manifest.update(overrides)
        return manifest

    def test_forced_release_manifest_is_current_or_three_patches_behind_app_versions(self):
        payload = json.loads((REPO_ROOT / "version.json").read_text(encoding="utf-8"))

        self.assertEqual(APP_VERSION, "2.0.42")
        self.assertEqual(BACKEND_APP_VERSION, APP_VERSION)
        app_version = tuple(int(part) for part in APP_VERSION.split("."))
        published_version = tuple(int(part) for part in payload["latest_version"].split("."))
        self.assertEqual(published_version[:2], app_version[:2])
        # Server-only hotfix releases may advance without promoting the forced
        # desktop channel. Keep that freeze bounded and never allow it ahead.
        self.assertIn(app_version[2] - published_version[2], (0, 1, 2, 3, 4))
        self.assertEqual(payload["release_tag"], f"v{payload['latest_version']}")
        self.assertEqual(payload["min_supported_version"], payload["latest_version"])
        self.assertIs(payload["mandatory"], True)
        self.assertIs(payload["block_workflow"], True)
        self.assertEqual(payload["package_type"], "onefile_exe")
        self.assertEqual(payload["entrypoint"], "TakSklad.exe")
        self.assertEqual(payload["signature_type"], "authenticode")
        self.assertIs(payload["signature_required"], True)
        approved_signer = next(iter(TRUSTED_WINDOWS_SIGNER_CERT_SHA256))
        self.assertEqual(payload["signer_certificate_sha256"], approved_signer)

        for url_field in ("download_url", "download_url_onedir"):
            with self.subTest(url_field=url_field):
                validate_update_download_url(payload[url_field])

        for sha_field in ("sha256", "sha256_onedir"):
            with self.subTest(sha_field=sha_field):
                validate_update_sha256(payload[sha_field])
        self.assertEqual(validate_update_manifest(payload)[2], approved_signer)

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
            "https://github.com/1fear/TakSklad/releases/download/main/TakSklad.exe",
            "https://github.com/1fear/TakSklad/releases/download/master/TakSklad.exe",
            "https://github.com/1fear/TakSklad/releases/download/v2.0.15/TakSklad.exe?raw=1",
        ]

        for url in bad_urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_update_download_url(url)

    def test_update_sha256_rejects_bad_shape(self):
        validate_update_sha256("a" * 64)

        for checksum in ("", "A" * 64, "short", "g" * 64):
            with self.subTest(checksum=checksum):
                with self.assertRaises(ValueError):
                    validate_update_sha256(checksum)

    def test_update_manifest_requires_matching_immutable_tag_and_authenticode(self):
        manifest = self._release_manifest()
        self.assertEqual(
            validate_update_manifest(
                manifest,
                trusted_signers={SYNTHETIC_SIGNER_CERT_SHA256},
            ),
            (
                manifest["download_url"],
                manifest["sha256"],
                SYNTHETIC_SIGNER_CERT_SHA256,
            ),
        )

        rejected = (
            {**manifest, "download_url": "https://github.com/1fear/TakSklad/releases/download/main/TakSklad.exe"},
            {**manifest, "release_tag": "main"},
            {**manifest, "download_url": "https://github.com/1fear/TakSklad/releases/download/v9.8.6/TakSklad.exe"},
            {**manifest, "sha256": ""},
            {**manifest, "signature_type": ""},
            {**manifest, "signature_required": False},
            {**manifest, "signer_certificate_sha256": ""},
            {**manifest, "signer_certificate_sha256": "2" * 64},
        )
        for candidate in rejected:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    validate_update_manifest(
                        candidate,
                        trusted_signers={SYNTHETIC_SIGNER_CERT_SHA256},
                    )

        with self.assertRaisesRegex(ValueError, "allowlist"):
            validate_update_manifest(manifest)

        approved_signer = next(iter(TRUSTED_WINDOWS_SIGNER_CERT_SHA256))
        approved_manifest = {
            **manifest,
            "signer_certificate_sha256": approved_signer,
        }
        self.assertEqual(
            validate_update_manifest(approved_manifest)[2],
            approved_signer,
        )

    def test_download_rejects_missing_sha_before_network(self):
        manifest = self._release_manifest(sha256="")
        with mock.patch("taksklad.update_service.open_https_url") as open_url:
            with self.assertRaisesRegex(ValueError, "SHA256"):
                download_update_file(
                    manifest,
                    trusted_signers={SYNTHETIC_SIGNER_CERT_SHA256},
                )
        open_url.assert_not_called()

    def test_download_rejects_oversize_declared_or_streamed_artifact(self):
        payload = b"abcd"
        manifest = self._release_manifest(payload)
        cases = (
            (FakeDownloadResponse(payload, content_length=MAX_UPDATE_DOWNLOAD_BYTES + 1), None),
            (FakeDownloadResponse(payload), 3),
        )
        for response, patched_limit in cases:
            with self.subTest(patched_limit=patched_limit):
                patches = [mock.patch("taksklad.update_service.open_https_url", return_value=response)]
                if patched_limit is not None:
                    patches.append(mock.patch("taksklad.update_service.MAX_UPDATE_DOWNLOAD_BYTES", patched_limit))
                with patches[0]:
                    if len(patches) == 2:
                        with patches[1]:
                            with self.assertRaisesRegex(ValueError, "превышает"):
                                download_update_file(
                                    manifest,
                                    trusted_signers={SYNTHETIC_SIGNER_CERT_SHA256},
                                )
                    else:
                        with self.assertRaisesRegex(ValueError, "превышает"):
                            download_update_file(
                                manifest,
                                trusted_signers={SYNTHETIC_SIGNER_CERT_SHA256},
                            )

    def test_download_rejects_bad_authenticode_signature(self):
        payload = b"badly signed synthetic exe"
        manifest = self._release_manifest(payload)
        response = FakeDownloadResponse(payload, content_length=len(payload))
        with mock.patch("taksklad.update_service.open_https_url", return_value=response), \
                mock.patch(
                    "taksklad.update_service.verify_windows_authenticode_signature",
                    side_effect=ValueError("Authenticode-подпись обновления недействительна"),
                ):
            with self.assertRaisesRegex(ValueError, "Authenticode"):
                download_update_file(
                    manifest,
                    trusted_signers={SYNTHETIC_SIGNER_CERT_SHA256},
                )

    def test_download_accepts_hash_and_valid_authenticode_signature(self):
        payload = b"valid signed synthetic exe"
        manifest = self._release_manifest(payload)
        response = FakeDownloadResponse(payload, content_length=len(payload))
        with mock.patch("taksklad.update_service.open_https_url", return_value=response), \
                mock.patch(
                    "taksklad.update_service.verify_windows_authenticode_signature",
                    return_value=True,
                ) as verify_signature:
            downloaded_path = download_update_file(
                manifest,
                trusted_signers={SYNTHETIC_SIGNER_CERT_SHA256},
            )
        try:
            self.assertEqual(Path(downloaded_path).read_bytes(), payload)
            verify_signature.assert_called_once_with(
                downloaded_path,
                SYNTHETIC_SIGNER_CERT_SHA256,
            )
        finally:
            Path(downloaded_path).unlink(missing_ok=True)

    def test_authenticode_verifier_rejects_invalid_status(self):
        completed = mock.Mock(
            returncode=0,
            stdout=f"HashMismatch\n{SYNTHETIC_SIGNER_CERT_SHA256}\n",
            stderr="",
        )
        with mock.patch("taksklad.update_service.os.name", "nt"), \
                mock.patch("taksklad.update_service.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(ValueError, "Authenticode"):
                verify_windows_authenticode_signature(
                    "TakSklad.synthetic.exe",
                    SYNTHETIC_SIGNER_CERT_SHA256,
                )

    def test_authenticode_verifier_accepts_exact_pinned_valid_status(self):
        completed = mock.Mock(
            returncode=0,
            stdout=f"Valid\n{SYNTHETIC_SIGNER_CERT_SHA256}\n",
            stderr="",
        )
        with mock.patch("taksklad.update_service.os.name", "nt"), \
                mock.patch("taksklad.update_service.subprocess.run", return_value=completed) as run:
            self.assertTrue(
                verify_windows_authenticode_signature(
                    "TakSklad.synthetic.exe",
                    SYNTHETIC_SIGNER_CERT_SHA256,
                )
            )
        command = run.call_args.args[0]
        self.assertIn("Get-AuthenticodeSignature", command[-2])
        self.assertEqual(command[-1], "TakSklad.synthetic.exe")

    def test_authenticode_verifier_accepts_exact_pinned_not_trusted_status(self):
        completed = mock.Mock(
            returncode=0,
            stdout=f"NotTrusted\n{SYNTHETIC_SIGNER_CERT_SHA256}\nCHAIN:PartialChain\n",
            stderr="",
        )
        with mock.patch("taksklad.update_service.os.name", "nt"), \
                mock.patch("taksklad.update_service.subprocess.run", return_value=completed):
            self.assertTrue(
                verify_windows_authenticode_signature(
                    "TakSklad.synthetic.exe",
                    SYNTHETIC_SIGNER_CERT_SHA256,
                )
            )
        self.assertEqual(
            WINDOWS_AUTHENTICODE_PINNED_STATUSES,
            frozenset({"Valid", "NotTrusted", "UnknownError"}),
        )
        self.assertEqual(
            WINDOWS_AUTHENTICODE_PINNED_CHAIN_STATUSES,
            frozenset({"PartialChain", "UntrustedRoot"}),
        )

    def test_authenticode_verifier_accepts_pinned_unknown_error_partial_chain(self):
        completed = mock.Mock(
            returncode=0,
            stdout=f"UnknownError\n{SYNTHETIC_SIGNER_CERT_SHA256}\nCHAIN:PartialChain\n",
            stderr="",
        )
        with mock.patch("taksklad.update_service.os.name", "nt"), \
                mock.patch("taksklad.update_service.subprocess.run", return_value=completed):
            self.assertTrue(
                verify_windows_authenticode_signature(
                    "TakSklad.synthetic.exe",
                    SYNTHETIC_SIGNER_CERT_SHA256,
                )
            )

    def test_authenticode_verifier_accepts_pinned_unknown_error_untrusted_root(self):
        completed = mock.Mock(
            returncode=0,
            stdout=f"UnknownError\n{SYNTHETIC_SIGNER_CERT_SHA256}\nCHAIN:UntrustedRoot\n",
            stderr="",
        )
        with mock.patch("taksklad.update_service.os.name", "nt"), \
                mock.patch("taksklad.update_service.subprocess.run", return_value=completed):
            self.assertTrue(
                verify_windows_authenticode_signature(
                    "TakSklad.synthetic.exe",
                    SYNTHETIC_SIGNER_CERT_SHA256,
                )
            )

    def test_authenticode_verifier_rejects_unknown_error_without_partial_chain(self):
        for chain_statuses in (
            "",
            "NotTimeValid",
            "PartialChain,NotTimeValid",
            "PartialChain,UntrustedRoot",
            "UntrustedRoot,NotTimeValid",
            "Revoked",
        ):
            with self.subTest(chain_statuses=chain_statuses):
                completed = mock.Mock(
                    returncode=0,
                    stdout=(
                        f"UnknownError\n{SYNTHETIC_SIGNER_CERT_SHA256}\n"
                        f"CHAIN:{chain_statuses}\n"
                    ),
                    stderr="",
                )
                with mock.patch("taksklad.update_service.os.name", "nt"), \
                        mock.patch("taksklad.update_service.subprocess.run", return_value=completed):
                    with self.assertRaisesRegex(ValueError, "Authenticode"):
                        verify_windows_authenticode_signature(
                            "TakSklad.synthetic.exe",
                            SYNTHETIC_SIGNER_CERT_SHA256,
                        )

    def test_authenticode_verifier_rejects_not_trusted_other_publisher(self):
        completed = mock.Mock(
            returncode=0,
            stdout=f"NotTrusted\n{'2' * 64}\nCHAIN:PartialChain\n",
            stderr="",
        )
        with mock.patch("taksklad.update_service.os.name", "nt"), \
                mock.patch("taksklad.update_service.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(ValueError, "недоверенным издателем"):
                verify_windows_authenticode_signature(
                    "TakSklad.synthetic.exe",
                    SYNTHETIC_SIGNER_CERT_SHA256,
                )

    def test_authenticode_verifier_rejects_other_valid_publisher(self):
        completed = mock.Mock(
            returncode=0,
            stdout=f"Valid\n{'2' * 64}\n",
            stderr="",
        )
        with mock.patch("taksklad.update_service.os.name", "nt"), \
                mock.patch("taksklad.update_service.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(ValueError, "недоверенным издателем"):
                verify_windows_authenticode_signature(
                    "TakSklad.synthetic.exe",
                    SYNTHETIC_SIGNER_CERT_SHA256,
                )

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

    def test_onefile_updater_failure_path_does_not_restart_old_exe_loop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            current_exe = temp_path / "TakSklad.exe"
            new_exe = temp_path / "TakSklad_new.exe"
            current_exe.write_text("old", encoding="utf-8")
            new_exe.write_text("new", encoding="utf-8")

            with mock.patch("taksklad.update_service.sys.frozen", True, create=True), \
                    mock.patch("taksklad.update_service.os.name", "nt"), \
                    mock.patch("taksklad.update_service.sys.executable", str(current_exe)), \
                    mock.patch("taksklad.update_service.UPDATE_LOG_FILE", str(temp_path / "TakSklad_update.log")), \
                    mock.patch("taksklad.update_service.tempfile.gettempdir", return_value=str(temp_path)), \
                    mock.patch("taksklad.update_service.os.getpid", return_value=4321):
                updater_path = create_windows_exe_updater(str(new_exe))

            script = Path(updater_path).read_text(encoding="utf-8")
            self.assertIn("перезапуск старого exe отключён", script)
            failure_path = script.split("Не удалось заменить приложение", 1)[1]
            self.assertNotIn('start "" "%APP%"', failure_path)

    def test_onedir_updater_uses_staged_swap_and_excludes_runtime_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            app_dir = temp_path / "current"
            app_dir.mkdir()
            current_exe = app_dir / "TakSklad.exe"
            current_exe.write_text("old", encoding="utf-8")
            zip_path = temp_path / "TakSklad.zip"
            self._write_onedir_zip(zip_path)

            with mock.patch("taksklad.update_service.sys.frozen", True, create=True), \
                    mock.patch("taksklad.update_service.os.name", "nt"), \
                    mock.patch("taksklad.update_service.sys.executable", str(current_exe)), \
                    mock.patch("taksklad.update_service.APP_DIR", str(app_dir)), \
                    mock.patch("taksklad.update_service.UPDATE_LOG_FILE", str(temp_path / "TakSklad_update.log")), \
                    mock.patch("taksklad.update_service.tempfile.gettempdir", return_value=str(temp_path)), \
                    mock.patch("taksklad.update_service.os.getpid", return_value=1234):
                updater_path = create_windows_onedir_updater(
                    str(zip_path),
                    self._release_manifest(
                        entrypoint="TakSklad.exe",
                        package_type="onedir_zip",
                        download_url_onedir="https://github.com/1fear/TakSklad/releases/download/v9.8.7/TakSklad-windows-x64.zip",
                        sha256_onedir="a" * 64,
                    ),
                    trusted_signers={SYNTHETIC_SIGNER_CERT_SHA256},
                )

            script = Path(updater_path).read_text(encoding="utf-8-sig")

            self.assertIn("$NewDir = Join-Path $ParentDir", script)
            self.assertIn("$PreviousDir = Join-Path $ParentDir", script)
            self.assertIn("robocopy $SourceDir $NewDir", script)
            self.assertIn("Move-Item -LiteralPath $AppDir -Destination $PreviousDir", script)
            self.assertIn("Move-Item -LiteralPath $NewDir -Destination $AppDir", script)
            self.assertNotIn("robocopy $SourceDir $AppDir", script)
            signature_check = script.index("Get-AuthenticodeSignature")
            staged_copy = script.index("robocopy $SourceDir $NewDir")
            self.assertLess(signature_check, staged_copy)
            self.assertIn("SignatureStatus]::Valid", script)
            self.assertIn("SignatureStatus]::NotTrusted", script)
            self.assertIn("SignatureStatus]::UnknownError", script)
            self.assertIn("X509RevocationMode]::NoCheck", script)
            self.assertIn("@('PartialChain', 'UntrustedRoot')", script)
            self.assertIn("$AcceptedChainStatuses -notcontains $ChainStatuses[0]", script)
            self.assertIn("$AcceptedSignatureStatuses -notcontains $Signature.Status", script)
            self.assertIn("$ExpectedSignerCertificateSha256", script)
            self.assertIn(SYNTHETIC_SIGNER_CERT_SHA256, script)
            self.assertIn("SignerCertificate.RawData", script)
            self.assertIn("недоверенным издателем", script)
            self.assertLess(
                script.index("$SignerCertificateSha256 -ne $ExpectedSignerCertificateSha256"),
                script.index("$AcceptedSignatureStatuses -notcontains $Signature.Status"),
            )

            for fragment in (
                "'TakSklad_data.json'",
                "'TakSklad_data.json.last_good.*.bak'",
                "'TakSklad_data.json.*.tmp'",
                "'TakSklad_queues.sqlite3'",
                "'TakSklad_queues.sqlite3-wal'",
                "'TakSklad_queues.sqlite3-shm'",
                "'telegram_settings.json'",
                "'yandex_geocoder_key.txt'",
                "'.env.taksklad-vds-2.0.generated.json'",
                "'secret-store-v1.json'",
                "'secret_store.v1.dpapi'",
                "'pending_saves.json'",
                "'pending_prints.json'",
                "'pending_telegram.json'",
                "'pending_backend_events.json'",
                "'telegram_state.json'",
                "'product_catalog.json'",
                "'import_history.json'",
                "'print_settings.json'",
                "'*.log'",
                "'scan_backups'",
                "'reports'",
                "'outputs'",
                "'backups'",
                "'diagnostics'",
            ):
                with self.subTest(fragment=fragment):
                    self.assertIn(fragment, script)

            preserve_assignment = next(
                line for line in script.splitlines() if line.startswith("$RuntimePreserveFiles =")
            )
            for secret_name in (
                "telegram_settings.json",
                "yandex_geocoder_key.txt",
                ".env.taksklad-vds-2.0.generated.json",
                "secret-store-v1.json",
                "secret_store.v1.dpapi",
            ):
                self.assertNotIn(secret_name, preserve_assignment)

            success_block = script.split('Start-Process -FilePath $NewExe', 1)[1].split('} catch {', 1)[0]
            self.assertIn("Previous app dir retained", success_block)
            self.assertNotIn("Remove-Item -LiteralPath $PreviousDir", success_block)

    def test_onedir_updater_failure_restores_previous_without_starting_old_exe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            app_dir = temp_path / "current"
            app_dir.mkdir()
            current_exe = app_dir / "TakSklad.exe"
            current_exe.write_text("old", encoding="utf-8")
            zip_path = temp_path / "TakSklad.zip"
            self._write_onedir_zip(zip_path)

            with mock.patch("taksklad.update_service.sys.frozen", True, create=True), \
                    mock.patch("taksklad.update_service.os.name", "nt"), \
                    mock.patch("taksklad.update_service.sys.executable", str(current_exe)), \
                    mock.patch("taksklad.update_service.APP_DIR", str(app_dir)), \
                    mock.patch("taksklad.update_service.UPDATE_LOG_FILE", str(temp_path / "TakSklad_update.log")), \
                    mock.patch("taksklad.update_service.tempfile.gettempdir", return_value=str(temp_path)), \
                    mock.patch("taksklad.update_service.os.getpid", return_value=1235):
                updater_path = create_windows_onedir_updater(
                    str(zip_path),
                    self._release_manifest(
                        entrypoint="TakSklad.exe",
                        package_type="onedir_zip",
                        download_url_onedir="https://github.com/1fear/TakSklad/releases/download/v9.8.7/TakSklad-windows-x64.zip",
                        sha256_onedir="a" * 64,
                    ),
                    trusted_signers={SYNTHETIC_SIGNER_CERT_SHA256},
                )

            script = Path(updater_path).read_text(encoding="utf-8-sig")
            catch_block = script.split("} catch {", 1)[1]

            self.assertIn("Previous app dir restored after failed update", catch_block)
            self.assertIn("Перезапуск старого exe отключён", catch_block)
            self.assertIn("Безопасное действие: установите свежий Windows-архив вручную", catch_block)
            self.assertIn("exit 1", catch_block)
            self.assertNotIn("Start-Process", catch_block)


if __name__ == "__main__":
    unittest.main()
