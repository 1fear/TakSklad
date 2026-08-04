import unittest
from unittest import mock

import httpx

from backend.app.telegram_report_processor import TelegramReportProcessor


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class LogisticsTelegramSplitTests(unittest.TestCase):
    def setUp(self):
        self.processor = TelegramReportProcessor.__new__(TelegramReportProcessor)
        self.sent_documents = []
        self.sent_messages = []
        self.processor.safe_send_document = lambda chat_id, content, filename, caption=None: (
            self.sent_documents.append((chat_id, content, filename, caption))
        )
        self.processor.safe_send_message = lambda chat_id, text, **kwargs: (
            self.sent_messages.append((chat_id, text))
        )

    def stub_backend(self, available_zones):
        def backend_get_bytes(path, params=None):
            zone = (params or {}).get("zone")
            if zone not in available_zones:
                raise httpx.HTTPStatusError(
                    "not found",
                    request=mock.Mock(),
                    response=FakeResponse(404),
                )
            return b"payload-" + zone.encode(), {}

        self.processor.backend_get_bytes = backend_get_bytes

    def test_sends_city_first_then_region(self):
        self.stub_backend({"city", "region"})
        result = self.processor.send_logistics_report(555, "2030-01-02")
        self.assertTrue(result)
        self.assertEqual(
            [item[2] for item in self.sent_documents],
            [
                "TakSklad_логистика_город_02.01.2030.xlsx",
                "TakSklad_логистика_область_02.01.2030.xlsx",
            ],
        )
        self.assertEqual(
            [item[3] for item in self.sent_documents],
            ["Отчет логистики город 02.01.2030", "Отчет логистики область 02.01.2030"],
        )

    def test_empty_zone_is_skipped_without_error_message(self):
        self.stub_backend({"city"})
        result = self.processor.send_logistics_report(555, "2030-01-02")
        self.assertTrue(result)
        self.assertEqual(len(self.sent_documents), 1)
        self.assertEqual(self.sent_messages, [])

    def test_both_zones_empty_reports_once(self):
        self.stub_backend(set())
        result = self.processor.send_logistics_report(555, "2030-01-02")
        self.assertFalse(result)
        self.assertEqual(self.sent_documents, [])
        self.assertEqual(len(self.sent_messages), 1)
        self.assertIn("02.01.2030", self.sent_messages[0][1])


if __name__ == "__main__":
    unittest.main()
