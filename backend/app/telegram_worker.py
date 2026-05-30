import logging
import os
import time
from datetime import datetime

import httpx
from sqlalchemy import select

from .db import SessionLocal
from .models import PendingEvent


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def normalize_text(value):
    return str(value or "").strip()


def parse_chat_ids(value):
    result = set()
    for part in str(value or "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            result.add(part)
    return result


class TelegramWorker:
    def __init__(self):
        self.token = normalize_text(os.environ.get("TELEGRAM_BOT_TOKEN"))
        self.allowed_chat_ids = parse_chat_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS"))
        self.backend_url = normalize_text(os.environ.get("TAKSKLAD_BACKEND_INTERNAL_URL")) or "http://backend-api:8000"
        self.backend_token = normalize_text(os.environ.get("TAKSKLAD_API_TOKEN"))
        self.timeout = int(os.environ.get("TELEGRAM_WORKER_TIMEOUT_SECONDS", "20") or "20")
        self.offset = self.load_offset() or int(os.environ.get("TELEGRAM_WORKER_INITIAL_OFFSET", "0") or "0")

    @property
    def configured(self):
        return bool(self.token)

    def telegram_request(self, method, payload=None):
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload or {})
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(data)
            return data.get("result")

    def backend_get(self, path, params=None):
        headers = {}
        if self.backend_token:
            headers["Authorization"] = f"Bearer {self.backend_token}"
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.backend_url}{path}", params=params or {}, headers=headers)
            response.raise_for_status()
            return response.json()

    def send_message(self, chat_id, text):
        return self.telegram_request("sendMessage", {
            "chat_id": chat_id,
            "text": text[:3900],
            "parse_mode": "HTML",
        })

    def poll_once(self):
        if not self.configured:
            logging.info("Telegram worker disabled: TELEGRAM_BOT_TOKEN is not configured")
            return

        updates = self.telegram_request("getUpdates", {
            "offset": self.offset + 1 if self.offset else None,
            "timeout": 25,
            "allowed_updates": ["message"],
        }) or []
        for update in updates:
            self.offset = max(self.offset, int(update.get("update_id") or 0))
            self.handle_update(update)
        if updates:
            self.save_offset()

    def handle_update(self, update):
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
            logging.warning("Telegram worker denied chat_id=%s", chat_id)
            return

        text = normalize_text(message.get("text"))
        if text.startswith("/start") or text.startswith("/help"):
            self.send_message(
                chat_id,
                "TakSklad backend online.\n\nКоманды:\n/health - проверить backend\n/report - дневной отчёт за сегодня",
            )
            return
        if text.startswith("/health"):
            payload = self.backend_get("/health")
            self.send_message(chat_id, f"Backend: {payload.get('status')} / {payload.get('version')}")
            return
        if text.startswith("/report"):
            payload = self.backend_get("/api/v1/reports/day")
            totals = payload.get("totals") or {}
            report_date = payload.get("report_date") or datetime.now().strftime("%Y-%m-%d")
            self.send_message(
                chat_id,
                "\n".join([
                    f"Отчёт TakSklad за {report_date}",
                    f"Заказов: {totals.get('orders', 0)}",
                    f"Выполнено заказов: {totals.get('completed_orders', 0)}",
                    f"Активных заказов: {totals.get('active_orders', 0)}",
                    f"План блоков: {totals.get('planned_blocks', 0)}",
                    f"Отсканировано: {totals.get('scanned_blocks', 0)}",
                    f"Осталось: {totals.get('remaining_blocks', 0)}",
                ]),
            )
            return

        document = message.get("document") or {}
        if document:
            self.send_message(
                chat_id,
                "Excel-файл получен. Авто-импорт вложений через серверный Telegram worker требует финальной приёмки 2.0; пока используйте desktop/backend импорт.",
            )
            return

        self.send_message(chat_id, "Команда не распознана. Используйте /help")

    def load_offset(self):
        try:
            with SessionLocal() as db:
                state = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == "telegram_worker_state")
                ).scalars().first()
                return int((state.payload or {}).get("offset") or 0) if state else 0
        except Exception:
            logging.info("Telegram worker: offset not loaded from database", exc_info=True)
            return 0

    def save_offset(self):
        try:
            with SessionLocal() as db:
                state = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == "telegram_worker_state")
                ).scalars().first()
                if state is None:
                    state = PendingEvent(event_type="telegram_worker_state", status="active", payload={})
                    db.add(state)
                state.payload = {"offset": self.offset}
                db.commit()
        except Exception:
            logging.info("Telegram worker: offset not saved to database", exc_info=True)


def main():
    worker = TelegramWorker()
    if not worker.configured:
        while True:
            logging.info("Telegram worker waiting for TELEGRAM_BOT_TOKEN")
            time.sleep(300)

    while True:
        try:
            worker.poll_once()
        except Exception:
            logging.exception("Telegram worker failed")
            time.sleep(10)


if __name__ == "__main__":
    main()
