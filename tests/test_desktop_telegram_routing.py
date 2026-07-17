import unittest
from unittest import mock

from taksklad import telegram_service
from taksklad.telegram_service import DesktopTelegramMessageKind


ADMIN_ID = "1001"


class DesktopTelegramRoutingTests(unittest.TestCase):
    def settings(self):
        return {
            "enabled": True,
            "bot_token": "synthetic-token",
            "routing_contract_version": 1,
            "admin_chat_id": ADMIN_ID,
            "chat_id": "",
            "chat_ids": [],
        }

    def test_exact_single_admin_route_is_required(self):
        self.assertEqual(telegram_service.get_telegram_chat_ids(self.settings()), [ADMIN_ID])
        cases = (
            {**self.settings(), "routing_contract_version": 0},
            {**self.settings(), "admin_chat_id": "-1002001"},
            {**self.settings(), "admin_chat_id": ""},
            {**self.settings(), "chat_id": ADMIN_ID},
            {**self.settings(), "chat_ids": [ADMIN_ID, "1002"]},
        )
        for settings in cases:
            with self.subTest(settings=settings):
                self.assertEqual(telegram_service.get_telegram_chat_ids(settings), [])

    def test_service_error_sends_once_to_admin_without_network(self):
        calls = []
        with (
            mock.patch.object(telegram_service, "load_telegram_settings", return_value=self.settings()),
            mock.patch.object(
                telegram_service,
                "send_telegram_message_to_chat",
                side_effect=lambda chat_id, text, token, reply_markup=None: calls.append(
                    (chat_id, text, token, reply_markup)
                ),
            ),
        ):
            ok, _ = telegram_service.send_telegram_message(
                "synthetic service error",
                message_kind=DesktopTelegramMessageKind.SERVICE_ERROR,
            )
        self.assertTrue(ok)
        self.assertEqual(calls, [(ADMIN_ID, "synthetic service error", "synthetic-token", None)])

    def test_unknown_or_wrong_operation_kind_is_blocked_without_send(self):
        with (
            mock.patch.object(telegram_service, "load_telegram_settings", return_value=self.settings()),
            mock.patch.object(telegram_service, "send_telegram_message_to_chat") as send,
        ):
            self.assertFalse(telegram_service.send_telegram_message("x", message_kind="legacy")[0])
            self.assertFalse(
                telegram_service.send_telegram_message(
                    "x", message_kind=DesktopTelegramMessageKind.SERVICE_DOCUMENT
                )[0]
            )
        send.assert_not_called()

    def test_delivery_failures_do_not_expose_raw_admin_target(self):
        with (
            mock.patch.object(telegram_service, "load_telegram_settings", return_value=self.settings()),
            mock.patch.object(
                telegram_service,
                "send_telegram_message_to_chat",
                side_effect=RuntimeError("synthetic failure"),
            ),
        ):
            ok, error = telegram_service.send_telegram_message(
                "synthetic service error",
                message_kind=DesktopTelegramMessageKind.SERVICE_ERROR,
            )
        self.assertFalse(ok)
        self.assertNotIn(ADMIN_ID, error)

        with (
            mock.patch.object(telegram_service, "load_telegram_settings", return_value=self.settings()),
            mock.patch.object(telegram_service, "safe_telegram_document_path", return_value=True),
            mock.patch.object(
                telegram_service,
                "send_telegram_document_to_chat",
                side_effect=RuntimeError("synthetic failure"),
            ),
        ):
            ok, error = telegram_service.send_telegram_document(
                "/synthetic/report.xlsx",
                message_kind=DesktopTelegramMessageKind.SERVICE_DOCUMENT,
            )
        self.assertFalse(ok)
        self.assertNotIn(ADMIN_ID, error)

    def test_legacy_pending_item_has_no_silent_fallback(self):
        legacy_item = {
            "id": "legacy",
            "path": "/synthetic/report.xlsx",
            "caption": "legacy",
        }
        with (
            mock.patch.object(telegram_service, "telegram_is_configured", return_value=True),
            mock.patch.object(telegram_service, "load_pending_telegram", return_value=[legacy_item]),
            mock.patch.object(telegram_service, "safe_telegram_document_path", return_value=True),
            mock.patch.object(telegram_service, "send_telegram_document") as send,
            mock.patch.object(
                telegram_service,
                "reconcile_queue_section",
                side_effect=lambda section, pending, remaining: remaining,
            ),
        ):
            result = telegram_service.sync_pending_telegram()
        self.assertEqual(result, {"sent": 0, "failed": 1, "remaining": 1})
        self.assertIn("missing typed message kind", legacy_item["last_error"])
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
