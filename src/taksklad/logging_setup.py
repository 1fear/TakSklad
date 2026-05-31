import logging
import os
from logging.handlers import RotatingFileHandler


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


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
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root_logger.addHandler(handler)
    if root_logger.level == logging.NOTSET or root_logger.level > level:
        root_logger.setLevel(level)
    return handler
