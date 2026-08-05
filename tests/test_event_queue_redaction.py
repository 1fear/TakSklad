import unittest

from backend.app.event_queue_service import is_secret_key, redact_payload


class EventQueueRedactionTests(unittest.TestCase):
    def test_chat_id_is_masked_like_other_secrets(self):
        # Аудит 05.08.2026: sanitize_audit_payload смартап-импорта маскирует
        # chat_id, а диагностика очереди отдавала его как есть в /admin/events
        for key in ("chat_id", "logistics_chat_id", "CHAT_ID", "admin_chat_id"):
            with self.subTest(key=key):
                self.assertTrue(is_secret_key(key))

    def test_known_secret_markers_still_masked(self):
        for key in ("token", "telegram_bot_token", "password", "secret", "authorization"):
            with self.subTest(key=key):
                self.assertTrue(is_secret_key(key))

    def test_ordinary_keys_are_not_masked(self):
        for key in ("delivery_date", "status", "order_id", "filenames"):
            with self.subTest(key=key):
                self.assertFalse(is_secret_key(key))

    def test_nested_chat_id_is_masked_in_payload(self):
        payload = {
            "kind": "smartup_logistics_report",
            "route": {"logistics_chat_id": "-1001234567890", "role": "logistics"},
            "items": [{"chat_id": "-1009876543210"}],
            "delivery_date": "2026-08-06",
        }
        redacted = redact_payload(payload)
        self.assertEqual(redacted["route"]["logistics_chat_id"], "***")
        self.assertEqual(redacted["items"][0]["chat_id"], "***")
        self.assertEqual(redacted["route"]["role"], "logistics")
        self.assertEqual(redacted["delivery_date"], "2026-08-06")


if __name__ == "__main__":
    unittest.main()
