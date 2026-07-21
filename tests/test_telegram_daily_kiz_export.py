import unittest
from datetime import date

from backend.app.telegram_daily_kiz_export import send_daily_kiz_export


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeSender:
    def __init__(self, builder):
        self.kiz_date_report_builder = builder
        self.sent = []
        self.safe_sent = []

    def _scheduled_session_factory(self):
        return _FakeSession

    def send_document(self, chat_id, content, filename, caption=""):
        self.sent.append((chat_id, content, filename, caption))
        return {"message_id": 1}

    def safe_send_document(self, chat_id, content, filename, caption=""):
        self.safe_sent.append((chat_id, content, filename, caption))
        return {"message_id": 2}


class DailyKizExportTests(unittest.TestCase):
    def test_scheduled_send_uses_send_document_with_plain_caption(self):
        sender = _FakeSender(lambda db, day: (b"xlsx", f"TakSklad_КИЗ_{day}.xlsx"))

        result = send_daily_kiz_export(sender, "123", date(2026, 6, 20), True)

        self.assertIsNotNone(result)
        self.assertEqual(len(sender.sent), 1)
        chat_id, content, filename, caption = sender.sent[0]
        self.assertEqual(chat_id, "123")
        self.assertEqual(content, b"xlsx")
        self.assertEqual(filename, "TakSklad_КИЗ_2026-06-20.xlsx")
        self.assertEqual(caption, "Коды маркировки 20.06.2026")

    def test_builder_receives_iso_report_date(self):
        seen = []

        def builder(db, day):
            seen.append(day)
            return b"xlsx", "kiz.xlsx"

        send_daily_kiz_export(_FakeSender(builder), "123", date(2026, 7, 21), True)

        self.assertEqual(seen, ["2026-07-21"])

    def test_manual_send_uses_safe_send_document(self):
        sender = _FakeSender(lambda db, day: (b"xlsx", "kiz.xlsx"))

        send_daily_kiz_export(sender, "123", date(2026, 6, 20), False)

        self.assertEqual(sender.sent, [])
        self.assertEqual(len(sender.safe_sent), 1)

    def test_missing_kiz_does_not_break_daily_delivery(self):
        def builder(db, day):
            raise RuntimeError("No KIZ scans for shipment date")

        sender = _FakeSender(builder)
        progress = []

        result = send_daily_kiz_export(
            sender,
            "123",
            date(2026, 6, 20),
            True,
            lambda stage, **fields: progress.append((stage, fields)),
        )

        self.assertIsNone(result)
        self.assertEqual(sender.sent, [])
        self.assertEqual(progress[0][0], "kiz export skipped")
        self.assertEqual(progress[0][1]["reason"], "RuntimeError")

    def test_failure_without_progress_callback_is_silent(self):
        def builder(db, day):
            raise RuntimeError("boom")

        self.assertIsNone(send_daily_kiz_export(_FakeSender(builder), "123", date(2026, 6, 20), True))


if __name__ == "__main__":
    unittest.main()
