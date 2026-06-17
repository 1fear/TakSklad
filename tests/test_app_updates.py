import unittest
from unittest import mock

from taksklad.app_updates import UpdateMixin, format_update_recovery_message


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
        self.started_update = False

    def auto_update_supported(self):
        return True

    def safe_config(self, widget, **kwargs):
        widget.config(**kwargs)

    def show_error(self, message, popup=True):
        self.errors.append(message)
        self.status_var.set(message)

    def start_auto_update(self, update_info):
        self.started_update = True


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

        self.assertTrue(app.update_required)
        self.assertFalse(app.started_update)
        self.assertEqual(app.refresh_btn.options["state"], "disabled")
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
                    "package_type": "onefile_exe",
                    "download_url": "https://example.com/TakSklad.exe",
                }
            )

        self.assertTrue(app.update_required)
        self.assertFalse(app.started_update)
        self.assertEqual(app.refresh_btn.options["state"], "disabled")
        self.assertIn("TakSklad_update.log", app.status_var.value)

    def test_update_recovery_message_points_to_update_log_and_safe_action(self):
        message = format_update_recovery_message("download failed")

        self.assertIn("download failed", message)
        self.assertIn("TakSklad_update.log", message)
        self.assertIn("установите свежий Windows-архив", message)


if __name__ == "__main__":
    unittest.main()
