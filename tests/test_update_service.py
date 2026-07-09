import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from backend.app.settings import APP_VERSION as BACKEND_APP_VERSION
from taksklad.config import APP_VERSION
from taksklad.update_service import (
    create_windows_exe_updater,
    create_windows_onedir_updater,
    package_transition_required,
    select_update_download,
    validate_update_download_url,
    validate_update_sha256,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class UpdateServiceTests(unittest.TestCase):
    def _write_onedir_zip(self, zip_path):
        with zipfile.ZipFile(zip_path, "w") as zip_file:
            zip_file.writestr("TakSklad/TakSklad.exe", "fake exe")
            zip_file.writestr("TakSklad/lib/module.pyd", "fake module")

    def test_current_forced_release_manifest_matches_app_versions(self):
        payload = json.loads((REPO_ROOT / "version.json").read_text(encoding="utf-8"))

        self.assertEqual(APP_VERSION, "2.0.25")
        self.assertEqual(BACKEND_APP_VERSION, APP_VERSION)
        self.assertEqual(payload["latest_version"], APP_VERSION)
        self.assertEqual(payload["min_supported_version"], APP_VERSION)
        self.assertIs(payload["mandatory"], True)
        self.assertIs(payload["block_workflow"], True)
        self.assertEqual(payload["package_type"], "onefile_exe")
        self.assertEqual(payload["entrypoint"], "TakSklad.exe")

        for url_field in ("download_url", "download_url_onedir"):
            with self.subTest(url_field=url_field):
                validate_update_download_url(payload[url_field])

        for sha_field in ("sha256", "sha256_onedir"):
            with self.subTest(sha_field=sha_field):
                validate_update_sha256(payload[sha_field])

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
                    {"entrypoint": "TakSklad.exe", "package_type": "onedir_zip"},
                )

            script = Path(updater_path).read_text(encoding="utf-8-sig")

            self.assertIn("$NewDir = Join-Path $ParentDir", script)
            self.assertIn("$PreviousDir = Join-Path $ParentDir", script)
            self.assertIn("robocopy $SourceDir $NewDir", script)
            self.assertIn("Move-Item -LiteralPath $AppDir -Destination $PreviousDir", script)
            self.assertIn("Move-Item -LiteralPath $NewDir -Destination $AppDir", script)
            self.assertNotIn("robocopy $SourceDir $AppDir", script)

            for fragment in (
                "'TakSklad_data.json'",
                "'TakSklad_data.json.last_good.*.bak'",
                "'TakSklad_data.json.*.tmp'",
                "'TakSklad_queues.sqlite3'",
                "'TakSklad_queues.sqlite3-wal'",
                "'TakSklad_queues.sqlite3-shm'",
                "'credentials.json'",
                "'telegram_settings.json'",
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
                    {"entrypoint": "TakSklad.exe", "package_type": "onedir_zip"},
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
