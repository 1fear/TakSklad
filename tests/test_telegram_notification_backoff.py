import unittest

from backend.app.telegram_admin_processor import (
    TELEGRAM_NOTIFICATION_MAX_PER_POLL,
    TELEGRAM_NOTIFICATION_RETRY_BACKOFF,
    TelegramAdminProcessor,
)


class TelegramNotificationLoopTests(unittest.TestCase):
    def make_processor(self):
        processor = TelegramAdminProcessor.__new__(TelegramAdminProcessor)
        processor.reset_stale_telegram_notification_events = lambda: 0
        processor.admin_chat_ids = {"1001"}
        processor.is_admin_chat = lambda chat_id: chat_id == "1001"
        return processor

    def test_poll_returns_control_when_one_event_keeps_failing(self):
        # Аудит 05.08.2026: упавшее уведомление сразу возвращалось в выборку,
        # while True не имел предела, poll_once переставал завершаться,
        # и вместе с уведомлениями вставал весь telegram-воркер: не было
        # getUpdates, не принимались команды и Excel-файлы из бота
        takes = []
        finishes = []

        def take_next():
            takes.append(1)
            return {
                "id": "stuck-notification",
                "payload": {
                    "kind": "daily_reconciliation_alert",
                    "chat_id": "1001",
                    "text": "Synthetic notification",
                },
                "lease_owner": "",
            }

        processor = self.make_processor()
        processor.take_next_telegram_notification_event = take_next

        def always_failing_send(chat_id, text):
            raise RuntimeError("telegram unavailable")

        processor.send_message = always_failing_send
        processor.finish_telegram_notification_event = (
            lambda event_id, success, error="", failure_status="failed", lease_owner="":
            finishes.append((event_id, success, failure_status))
        )

        processed = processor.process_pending_telegram_notifications()

        # Главное: управление вернулось. До правки это был while True без
        # предела, и метод не завершался никогда
        self.assertLessEqual(len(takes), TELEGRAM_NOTIFICATION_MAX_PER_POLL)
        self.assertLessEqual(processed, TELEGRAM_NOTIFICATION_MAX_PER_POLL)
        self.assertTrue(finishes, "неудача обязана быть зафиксирована")
        self.assertTrue(all(success is False for _id, success, _status in finishes))

    def test_empty_queue_stops_immediately(self):
        takes = []

        def take_next():
            takes.append(1)
            return None

        processor = self.make_processor()
        processor.take_next_telegram_notification_event = take_next
        self.assertEqual(processor.process_pending_telegram_notifications(), 0)
        self.assertEqual(len(takes), 1)

    def test_backoff_is_a_real_pause(self):
        self.assertGreaterEqual(TELEGRAM_NOTIFICATION_RETRY_BACKOFF.total_seconds(), 60)


if __name__ == "__main__":
    unittest.main()
