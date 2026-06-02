import unittest

from taksklad.app_updates import UpdateMixin


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


if __name__ == "__main__":
    unittest.main()
