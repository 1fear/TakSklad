import logging
import json
import os
from logging.handlers import RotatingFileHandler

from .secret_store import (
    BACKEND_API_TOKEN_SECRET,
    GEOCODER_API_KEY_SECRET,
    GOOGLE_CREDENTIALS_SECRET,
    TELEGRAM_BOT_TOKEN_SECRET,
    SecretStoreError,
    load_secret,
)


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_SECRET_NAMES = (
    GOOGLE_CREDENTIALS_SECRET,
    TELEGRAM_BOT_TOKEN_SECRET,
    BACKEND_API_TOKEN_SECRET,
    GEOCODER_API_KEY_SECRET,
)


def redact_known_secret_values(value):
    text = str(value or "")
    for name in LOG_SECRET_NAMES:
        try:
            secret = load_secret(name)
        except SecretStoreError:
            continue
        if not secret:
            continue
        fragments = {secret}
        if name == GOOGLE_CREDENTIALS_SECRET:
            try:
                payload = json.loads(secret)
            except (TypeError, ValueError):
                payload = None
            stack = [payload]
            while stack:
                current = stack.pop()
                if isinstance(current, dict):
                    stack.extend(current.values())
                elif isinstance(current, list):
                    stack.extend(current)
                elif isinstance(current, str):
                    fragments.add(current)
        encoded_fragments = set()
        for fragment in fragments:
            if len(fragment) < 4:
                continue
            encoded_fragments.add(fragment)
            encoded_fragments.add(repr(fragment)[1:-1])
            encoded_fragments.add(json.dumps(fragment, ensure_ascii=False)[1:-1])
        for fragment in sorted(encoded_fragments, key=len, reverse=True):
            if fragment:
                text = text.replace(fragment, "[redacted-secret]")
    return text


class SecretRedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact_known_secret_values(super().format(record))


def configure_app_logging(log_file, max_bytes, backup_count, level=logging.INFO):
    os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
    absolute_log_file = os.path.abspath(log_file)
    root_logger = logging.getLogger()

    for handler in root_logger.handlers:
        handler_file = getattr(handler, "baseFilename", None)
        if handler_file and os.path.abspath(handler_file) == absolute_log_file:
            return handler

    handler = RotatingFileHandler(
        absolute_log_file,
        maxBytes=max(1, int(max_bytes)),
        backupCount=max(0, int(backup_count)),
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(SecretRedactingFormatter(LOG_FORMAT))
    root_logger.addHandler(handler)
    if root_logger.level == logging.NOTSET or root_logger.level > level:
        root_logger.setLevel(level)
    return handler
