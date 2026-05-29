import unittest

from taksklad import sheets
from taksklad.sheets import format_google_sheets_error


class GoogleErrorMessageTests(unittest.TestCase):
    def test_permission_error_gets_actionable_message(self):
        message = format_google_sheets_error(PermissionError())

        self.assertIn("Нет доступа к Google-таблице", message)
        self.assertIn("service account", message)

    def test_invalid_jwt_gets_actionable_message(self):
        message = format_google_sheets_error(
            RuntimeError("invalid_grant: Invalid JWT Signature.")
        )

        self.assertIn("Google-ключ", message)
        self.assertIn("Invalid JWT Signature", message)

    def test_quota_error_gets_actionable_message(self):
        message = format_google_sheets_error(RuntimeError("APIError: [429]: Quota exceeded"))

        self.assertIn("временно ограничил запросы", message)
        self.assertIn("сканирование можно продолжать", message)

    def test_network_error_gets_actionable_message(self):
        message = format_google_sheets_error(RuntimeError("getaddrinfo failed"))

        self.assertIn("Нет стабильной связи", message)
        self.assertIn("уже загруженный список", message)

    def test_transient_google_error_starts_background_cooldown(self):
        sheets.reset_google_backoff_for_tests()

        self.assertTrue(
            sheets.note_google_transient_error(
                RuntimeError("APIError: [429]: Quota exceeded"),
                now_ts=1000,
            )
        )

        self.assertGreater(sheets.google_backoff_remaining(now_ts=1001), 0)
        with self.assertRaisesRegex(RuntimeError, "Google Sheets временно на паузе"):
            sheets.ensure_google_background_allowed("test", now_ts=1001)
        sheets.reset_google_backoff_for_tests()

    def test_non_transient_google_error_does_not_start_cooldown(self):
        sheets.reset_google_backoff_for_tests()

        self.assertFalse(sheets.note_google_transient_error(PermissionError(), now_ts=1000))
        self.assertEqual(sheets.google_backoff_remaining(now_ts=1001), 0)


if __name__ == "__main__":
    unittest.main()
