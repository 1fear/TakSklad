"""Scheduling-only entrypoint for SkladBot queue and sync processors."""

import logging
import os
import time
from collections.abc import Callable

from sqlalchemy.orm import Session

from .db import SessionLocal
from .skladbot_client import env_int
from .skladbot_contracts import normalize_lookup_text
from .skladbot_request_dry_run import process_pending_skladbot_request_creates
from .skladbot_return_requests import process_pending_skladbot_return_request_creates
from .skladbot_worker import update_orders_from_skladbot
from .worker_observability import observed_worker_cycle


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def worker_interval_seconds() -> int:
    return max(60, env_int("SKLADBOT_WORKER_INTERVAL_SECONDS", 60))


def run_worker_cycle(
    *,
    session_factory: Callable[[], Session] | None = None,
    create_processor=None,
    return_processor=None,
    sync_processor=None,
    raise_on_error: bool = False,
) -> dict[str, object]:
    session_factory = session_factory or SessionLocal
    create_processor = create_processor or process_pending_skladbot_request_creates
    return_processor = return_processor or process_pending_skladbot_return_request_creates
    sync_processor = sync_processor or update_orders_from_skladbot
    cycle_errors = []
    create_result: dict[str, object] = {}
    return_result: dict[str, object] = {}
    try:
        with session_factory() as db:
            create_result = create_processor(db)
            if create_result.get("checked"):
                logging.info("SkladBot create worker: %s", create_result)
            return_result = return_processor(db)
            if return_result.get("checked"):
                logging.info("SkladBot return create worker: %s", return_result)
    except Exception as exc:
        cycle_errors.append(exc)
        logging.exception("SkladBot create worker failed")

    sync_result: dict[str, object] = {}
    try:
        sync_result = sync_processor()
    except Exception as exc:
        cycle_errors.append(exc)
        logging.exception("SkladBot worker failed")
    if cycle_errors and raise_on_error:
        raise RuntimeError("SkladBot worker cycle failed") from cycle_errors[0]
    return {
        "create": create_result,
        "return": return_result,
        "sync": sync_result,
    }


def main() -> None:
    interval = worker_interval_seconds()
    once = normalize_lookup_text(os.environ.get("SKLADBOT_WORKER_ONCE")) in {"1", "true", "yes", "да"}
    while True:
        try:
            with observed_worker_cycle("skladbot", interval):
                run_worker_cycle(raise_on_error=True)
        except Exception:
            logging.exception("SkladBot observed worker cycle failed")
        if once:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
