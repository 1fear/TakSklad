"""Вычистка секретов из логов.

`CLAUDE.md`, правило client-facing 4: Telegram-токены не коммитить и не логировать.
На практике токен попадает в логи не через наш код, а через `httpx`, который пишет
строку запроса целиком, а у Telegram Bot API токен стоит прямо в пути URL:

    INFO:httpx:HTTP Request: POST https://api.telegram.org/bot<токен>/sendMessage

06.08.2026 такой токен лежал открытым в `docker logs` воркера Smartup за каждую
отправку. Часть воркеров глушила логгер `httpx` целиком, часть нет, и наблюдаемость
при этом терялась вместе с утечкой.

Фильтр вычищает сам секрет и оставляет остальную строку читаемой, поэтому его можно
ставить всем воркерам, не жертвуя логами.
"""

from __future__ import annotations

import logging
import re

TELEGRAM_BOT_TOKEN_RE = re.compile(r"bot\d{5,}:[A-Za-z0-9_\-]{20,}")
REDACTED = "bot<redacted>"


def redact_secrets(text: str) -> str:
    return TELEGRAM_BOT_TOKEN_RE.sub(REDACTED, text)


class SecretRedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        if TELEGRAM_BOT_TOKEN_RE.search(message):
            # Готовое сообщение подставляется целиком: аргументы уже развёрнуты в него,
            # поэтому повторное форматирование не нужно и может уронить запись.
            record.msg = redact_secrets(message)
            record.args = ()
        return True


def install_secret_redaction() -> None:
    """Поставить фильтр на корневой логгер и на уже существующие обработчики."""
    log_filter = SecretRedactingFilter()
    root = logging.getLogger()
    if not any(isinstance(item, SecretRedactingFilter) for item in root.filters):
        root.addFilter(log_filter)
    for handler in root.handlers:
        if not any(isinstance(item, SecretRedactingFilter) for item in handler.filters):
            handler.addFilter(log_filter)
