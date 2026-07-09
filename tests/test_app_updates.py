import unittest
from unittest import mock

from taksklad.app_updates import UpdateMixin, format_update_recovery_message
from taksklad.config import APP_VERSION


class _StatusVar:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value


class _UnsupportedUpdateApp(UpdateMixin):
    def __init__(self):
        self.update_required = False
        self.update_info = None
        self.status_var = _StatusVar()
        self.started_update = False

    def auto_update_supported(self):
        return False

    def start_auto_update(self, update_info):
        self.started_update = True


class _Button:
    def __init__(self):
        self.options = {}

    def config(self, **kwargs):
        self.options.update(kwargs)


class _WindowsUpdateApp(UpdateMixin):
    def __init__(self):
        self.update_required = False
        self.update_info = None
        self.status_var = _StatusVar()
        self.status_label = _Button()
        self.refresh_btn = _Button()
        self.errors = []
        self.critical_errors = []
        self.started_update = False
        self.destroyed = False
        self.version_status_label = _Button()

    def auto_update_supported(self):
        return True

    def after(self, _delay, callback):
        callback()

    def safe_config(self, widget, **kwargs):
        widget.config(**kwargs)

    def show_error(self, message, popup=True):
        self.errors.append(message)
        self.status_var.set(message)

    def show_critical_error(self, title, message):
        self.critical_errors.append((title, message))
        self.status_var.set(f"{title}: {message}")

    def start_auto_update(self, update_info):
        self.started_update = True

    def destroy(self):
        self.destroyed = True


class AppUpdatesTest(unittest.TestCase):
    def test_unsupported_platform_update_is_not_blocking(self):
        app = _UnsupportedUpdateApp()

        app.handle_update_info(
            {
                "latest_version": "9.9.9",
                "min_supported_version": "1.1.7",
                "package_type": "onefile_exe",
                "download_url": "https://example.com/TakSklad.exe",
            }
        )

        self.assertFalse(app.update_required)
        self.assertFalse(app.started_update)
        self.assertEqual(app.update_info["latest_version"], "9.9.9")
        self.assertIn("На Mac установите свежий архив вручную", app.status_var.value)

    def test_forced_update_decline_locks_scanning_with_recovery_message(self):
        app = _WindowsUpdateApp()

        with mock.patch("taksklad.app_updates.load_data_section", return_value={}), \
                mock.patch("taksklad.app_updates.save_data_section"), \
                mock.patch("taksklad.app_updates.messagebox.askyesno", return_value=False):
            app.handle_update_info(
                {
                    "latest_version": "9.9.9",
                    "min_supported_version": "9.9.9",
                    "mandatory": True,
                    "block_workflow": True,
                    "package_type": "onefile_exe",
                    "download_url": "https://example.com/TakSklad.exe",
                }
            )

        self.assertTrue(app.update_required)
        self.assertFalse(app.started_update)
        self.assertEqual(app.refresh_btn.options["state"], "disabled")
        self.assertIn("заблокирована", app.version_status_label.options["text"])
        self.assertIn("TakSklad_update.log", app.status_var.value)
        self.assertIn("Старую версию для сканирования не используйте", app.status_var.value)

    def test_forced_update_cooldown_locks_scanning_instead_of_normal_mode(self):
        app = _WindowsUpdateApp()

        with mock.patch("taksklad.app_updates.time.time", return_value=1_000_100), \
                mock.patch("taksklad.app_updates.load_data_section", return_value={
                    "last_attempt_ts": 1_000_000,
                    "last_attempt_version": "9.9.9",
                }):
            app.handle_update_info(
                {
                    "latest_version": "9.9.9",
                    "min_supported_version": "9.9.9",
                    "mandatory": True,
                    "block_workflow": True,
                    "package_type": "onefile_exe",
                    "download_url": "https://example.com/TakSklad.exe",
                }
            )

        self.assertTrue(app.update_required)
        self.assertFalse(app.started_update)
        self.assertEqual(app.refresh_btn.options["state"], "disabled")
        self.assertIn("TakSklad_update.log", app.status_var.value)

    def test_forced_update_after_accepted_attempt_can_retry_without_cooldown_lock(self):
        app = _WindowsUpdateApp()

        with mock.patch("taksklad.app_updates.time.time", return_value=1_000_100), \
                mock.patch("taksklad.app_updates.load_data_section", return_value={
                    "last_attempt_ts": 1_000_000,
                    "last_attempt_version": "9.9.9",
                    "last_user_action": "accepted",
                }), \
                mock.patch("taksklad.app_updates.save_data_section") as save_section, \
                mock.patch("taksklad.app_updates.messagebox.askyesno", return_value=True):
            app.handle_update_info(
                {
                    "latest_version": "9.9.9",
                    "min_supported_version": "9.9.9",
                    "mandatory": True,
                    "block_workflow": True,
                    "package_type": "onefile_exe",
                    "download_url": "https://example.com/TakSklad.exe",
                }
            )

        self.assertTrue(app.update_required)
        self.assertTrue(app.started_update)
        self.assertEqual(app.refresh_btn.options["state"], "disabled")
        save_section.assert_called_once()

    def test_mandatory_update_without_workflow_block_decline_keeps_scanning_available(self):
        app = _WindowsUpdateApp()

        with mock.patch("taksklad.app_updates.messagebox.askyesno", return_value=False):
            app.handle_update_info(
                {
                    "latest_version": "9.9.9",
                    "min_supported_version": "9.9.9",
                    "mandatory": True,
                    "package_type": "onefile_exe",
                    "download_url": "https://example.com/TakSklad.exe",
                }
            )

        self.assertFalse(app.update_required)
        self.assertFalse(app.started_update)
        self.assertNotEqual(app.refresh_btn.options.get("state"), "disabled")
        self.assertIn("отложено", app.status_var.value.lower())

    def test_mandatory_update_without_workflow_block_accept_starts_update_without_lock(self):
        app = _WindowsUpdateApp()

        with mock.patch("taksklad.app_updates.load_data_section", return_value={}), \
                mock.patch("taksklad.app_updates.save_data_section"), \
                mock.patch("taksklad.app_updates.messagebox.askyesno", return_value=True):
            app.handle_update_info(
                {
                    "latest_version": "9.9.9",
                    "min_supported_version": "9.9.9",
                    "mandatory": True,
                    "package_type": "onefile_exe",
                    "download_url": "https://example.com/TakSklad.exe",
                }
            )

        self.assertFalse(app.update_required)
        self.assertTrue(app.started_update)
        self.assertNotEqual(app.refresh_btn.options.get("state"), "disabled")

    def test_mandatory_update_without_workflow_block_cooldown_keeps_scanning_available(self):
        app = _WindowsUpdateApp()

        with mock.patch("taksklad.app_updates.time.time", return_value=1_000_100), \
                mock.patch("taksklad.app_updates.load_data_section", return_value={
                    "last_attempt_ts": 1_000_000,
                    "last_attempt_version": "9.9.9",
                    "last_user_action": "declined",
                }), \
                mock.patch("taksklad.app_updates.messagebox.askyesno") as askyesno:
            app.handle_update_info(
                {
                    "latest_version": "9.9.9",
                    "min_supported_version": "9.9.9",
                    "mandatory": True,
                    "package_type": "onefile_exe",
                    "download_url": "https://example.com/TakSklad.exe",
                }
            )

        self.assertFalse(app.update_required)
        self.assertFalse(app.started_update)
        self.assertNotEqual(app.refresh_btn.options.get("state"), "disabled")
        self.assertIn("Откладываю до перезапуска", app.status_var.value)
        askyesno.assert_not_called()

    def test_current_forced_version_with_stale_cooldown_does_not_lock(self):
        app = _WindowsUpdateApp()

        with mock.patch("taksklad.app_updates.load_data_section") as load_section, \
                mock.patch("taksklad.app_updates.messagebox.askyesno") as askyesno:
            app.handle_update_info(
                {
                    "latest_version": APP_VERSION,
                    "min_supported_version": APP_VERSION,
                    "mandatory": True,
                    "package_type": "onefile_exe",
                    "download_url": "https://example.com/TakSklad.exe",
                }
            )

        self.assertFalse(app.update_required)
        self.assertFalse(app.started_update)
        self.assertIsNone(app.update_info)
        self.assertIn("актуальная", app.version_status_label.options["text"])
        load_section.assert_not_called()
        askyesno.assert_not_called()

    def test_update_check_failure_shows_unavailable_status_without_error_detail(self):
        app = _WindowsUpdateApp()

        class ImmediateThread:
            def __init__(self, target, daemon=None):
                self.target = target

            def start(self):
                self.target()

        with mock.patch("taksklad.app_updates.fetch_update_info", side_effect=RuntimeError("token=secret")), \
                mock.patch("taksklad.app_updates.threading.Thread", ImmediateThread), \
                mock.patch("taksklad.app_updates.logging.info") as log_info:
            app.check_for_updates()

        label = app.version_status_label.options["text"]
        self.assertIn("недоступен", label)
        self.assertIn("RuntimeError", label)
        self.assertNotIn("secret", label)
        self.assertTrue(
            any(call.args[:2] == ("Не удалось проверить обновления: %s", "RuntimeError") for call in log_info.call_args_list)
        )

    def test_current_version_package_transition_cooldown_is_non_blocking(self):
        app = _WindowsUpdateApp()

        with mock.patch("taksklad.app_updates.package_transition_required", return_value=True), \
                mock.patch("taksklad.app_updates.time.time", return_value=1_000_100), \
                mock.patch("taksklad.app_updates.load_data_section", return_value={
                    "last_attempt_ts": 1_000_000,
                    "last_attempt_version": APP_VERSION,
                    "last_user_action": "declined",
                }), \
                mock.patch("taksklad.app_updates.messagebox.askyesno") as askyesno:
            app.handle_update_info(
                {
                    "latest_version": APP_VERSION,
                    "min_supported_version": APP_VERSION,
                    "mandatory": True,
                    "package_type": "onedir_zip",
                    "download_url_onedir": "https://example.com/TakSklad.zip",
                }
            )

        self.assertFalse(app.update_required)
        self.assertFalse(app.started_update)
        self.assertNotEqual(app.refresh_btn.options.get("state"), "disabled")
        self.assertIn("Откладываю до перезапуска", app.status_var.value)
        askyesno.assert_not_called()

    def test_current_version_package_transition_decline_does_not_lock(self):
        app = _WindowsUpdateApp()

        with mock.patch("taksklad.app_updates.package_transition_required", return_value=True), \
                mock.patch("taksklad.app_updates.messagebox.askyesno", return_value=False):
            app.handle_update_info(
                {
                    "latest_version": APP_VERSION,
                    "min_supported_version": APP_VERSION,
                    "mandatory": True,
                    "package_type": "onedir_zip",
                    "download_url_onedir": "https://example.com/TakSklad.zip",
                }
            )

        self.assertFalse(app.update_required)
        self.assertFalse(app.started_update)
        self.assertNotEqual(app.refresh_btn.options.get("state"), "disabled")
        self.assertIn("отложено", app.status_var.value)

    def test_update_recovery_message_points_to_update_log_and_safe_action(self):
        message = format_update_recovery_message("download failed")

        self.assertIn("download failed", message)
        self.assertIn("TakSklad_update.log", message)
        self.assertIn("установите свежий Windows-архив", message)

    def test_run_update_installer_starts_powershell_script_and_closes_app(self):
        app = _WindowsUpdateApp()

        with mock.patch("taksklad.app_updates.subprocess.Popen") as popen:
            result = app.run_update_installer("C:\\Temp\\TakSklad_updater.ps1")

        self.assertTrue(result)
        self.assertTrue(app.destroyed)
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertEqual(command[:4], ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass"])
        self.assertEqual(command[-1], "C:\\Temp\\TakSklad_updater.ps1")

    def test_run_update_installer_failure_keeps_app_open_with_recovery_message(self):
        app = _WindowsUpdateApp()

        with mock.patch("taksklad.app_updates.subprocess.Popen", side_effect=OSError("powershell missing")), \
                mock.patch("taksklad.app_updates.logging.exception") as log_exception:
            result = app.run_update_installer("C:\\Temp\\TakSklad_updater.ps1")

        self.assertFalse(result)
        self.assertFalse(app.destroyed)
        log_exception.assert_called_once_with("Не удалось запустить установщик обновления")
        self.assertEqual(app.critical_errors[0][0], "Не удалось запустить установщик обновления")
        self.assertIn("powershell missing", app.critical_errors[0][1])
        self.assertIn("TakSklad_update.log", app.critical_errors[0][1])
        self.assertIn("Старую версию для сканирования не используйте", app.critical_errors[0][1])


if __name__ == "__main__":
    unittest.main()
