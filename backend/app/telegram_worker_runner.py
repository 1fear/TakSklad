"""Production entrypoint that injects the Telegram worker runtime identity."""

import logging
import os

from .db import SessionLocal
from .telegram_worker import main as run_worker
from .worker_runtime_identity import (
    WorkerRuntimeIdentityError,
    issue_telegram_worker_runtime_token,
)


def main():
    backend_token = None
    if str(os.environ.get("TAKSKLAD_ENV") or "").strip().casefold() == "production":
        try:
            backend_token = issue_telegram_worker_runtime_token(SessionLocal)
        except WorkerRuntimeIdentityError:
            logging.error("Telegram worker runtime identity is unavailable")
            return 2
    return run_worker(backend_token=backend_token)


if __name__ == "__main__":
    raise SystemExit(main())
