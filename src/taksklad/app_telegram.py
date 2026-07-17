import logging
import threading

from .config import APP_NAME
from .telegram_service import (
    DesktopTelegramMessageKind,
    send_telegram_message,
    sync_pending_telegram,
    telegram_is_configured,
    telegram_reports_keyboard,
)


def desktop_telegram_polling_enabled(settings=None):
    """Telegram updates are consumed by the server worker, never by desktop."""

    return False


class TelegramActionsMixin:
    def sync_pending_telegram_async(self):
        def worker():
            result = sync_pending_telegram()
            if result.get("sent"):
                logging.info("Telegram: отправлено из локальной очереди: %s", result["sent"])

        threading.Thread(target=worker, daemon=True).start()

    def send_telegram_alert_async(self, message, with_keyboard=True):
        if not telegram_is_configured():
            return

        def worker():
            ok, result = send_telegram_message(
                message,
                reply_markup=telegram_reports_keyboard() if with_keyboard else None,
                message_kind=DesktopTelegramMessageKind.SERVICE_ERROR,
            )
            if not ok:
                logging.warning("Telegram: сообщение не отправлено: %s", result)

        threading.Thread(target=worker, daemon=True).start()

    def log_duplicate_code_async(self, code):
        current_order = dict(self.current_order or {})
        logging.warning(
            "%s: найден дублирующийся КИЗ %s (backend item=%s, client=%s)",
            APP_NAME,
            code,
            current_order.get("_backend_order_item_id") or "",
            current_order.get("Клиент") or "",
        )

    def check_daily_reports_async(self):
        logging.info("Дневные отчёты формирует server Telegram worker")

    def poll_telegram_bot_async(self):
        logging.info("Desktop Telegram polling отключён; используется server worker")
