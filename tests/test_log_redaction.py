"""Токен Telegram не должен попадать в логи.

`CLAUDE.md`, правило client-facing 4. Утечка приходит не из нашего кода, а из httpx,
который пишет URL целиком, а Telegram держит токен прямо в пути.
06.08.2026 токен лежал открытым в docker logs воркера Smartup за каждую отправку.
"""

import logging
import unittest

from backend.app.log_redaction import (
    SecretRedactingFilter,
    install_secret_redaction,
    redact_secrets,
)

SAMPLE_URL = (
    "HTTP Request: POST https://api.telegram.org/"
    "bot1234567890:AAExampleTokenValueForTestsOnly0123456789/sendMessage"
)


class LogRedactionTests(unittest.TestCase):
    def test_token_is_removed_from_text(self):
        cleaned = redact_secrets(SAMPLE_URL)

        self.assertNotIn("AAExampleTokenValueForTestsOnly0123456789", cleaned)
        self.assertNotIn("1234567890:", cleaned)
        self.assertIn("bot<redacted>", cleaned)
        # Остальная строка остаётся читаемой, наблюдаемость не теряется.
        self.assertIn("api.telegram.org", cleaned)
        self.assertIn("sendMessage", cleaned)

    def test_text_without_token_is_untouched(self):
        plain = "HTTP Request: POST https://smartup.online/b/trade/txs/tdeal/order$export"

        self.assertEqual(redact_secrets(plain), plain)

    def test_filter_cleans_record_built_from_arguments(self):
        record = logging.LogRecord(
            name="httpx",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="%s",
            args=(SAMPLE_URL,),
            exc_info=None,
        )

        self.assertTrue(SecretRedactingFilter().filter(record))
        self.assertNotIn("AAExampleTokenValueForTestsOnly0123456789", record.getMessage())
        self.assertIn("bot<redacted>", record.getMessage())

    def test_install_is_idempotent(self):
        root = logging.getLogger()
        before = len(root.filters)
        install_secret_redaction()
        install_secret_redaction()
        after = len([f for f in root.filters if isinstance(f, SecretRedactingFilter)])

        self.assertEqual(after, 1)
        self.assertLessEqual(len(root.filters), before + 1)
        root.filters = [f for f in root.filters if not isinstance(f, SecretRedactingFilter)]


if __name__ == "__main__":
    unittest.main()
