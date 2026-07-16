"""Scheduling-only entrypoint for SkladBot queue and sync processors."""

import logging
import os
import time
from collections.abc import Callable

from sqlalchemy.orm import Session

from .db import SessionLocal
from .skladbot_client import env_int, notify_skladbot_progress
from .skladbot_contracts import normalize_lookup_text
from .skladbot_request_dry_run import process_pending_skladbot_request_creates
from .skladbot_return_requests import process_pending_skladbot_return_request_creates
from .skladbot_worker import update_orders_from_skladbot
from .worker_observability import observed_worker_cycle, record_cycle_progress


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def worker_interval_seconds() -> int:
    return max(60, env_int("SKLADBOT_WORKER_INTERVAL_SECONDS", 60))


def run_worker_cycle(
    *,
    session_factory: Callable[[], Session] | None = None,
    create_processor=None,
    return_processor=None,
    sync_processor=None,
    progress_callback: Callable[[str], None] | None = None,
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
            if create_processor is process_pending_skladbot_request_creates:
                create_result = create_processor(db, progress_callback=progress_callback)
            else:
                create_result = create_processor(db)
            if progress_callback is not None:
                notify_skladbot_progress(progress_callback, "create_processor_finished")
            if create_result.get("checked"):
                logging.info("SkladBot create worker: %s", create_result)
            if return_processor is process_pending_skladbot_return_request_creates:
                return_result = return_processor(db, progress_callback=progress_callback)
            else:
                return_result = return_processor(db)
            if progress_callback is not None:
                notify_skladbot_progress(progress_callback, "return_processor_finished")
            if return_result.get("checked"):
                logging.info("SkladBot return create worker: %s", return_result)
    except Exception as exc:
        cycle_errors.append(exc)
        logging.exception("SkladBot create worker failed")

    sync_result: dict[str, object] = {}
    try:
        if sync_processor is update_orders_from_skladbot:
            sync_result = sync_processor(progress_callback=progress_callback)
        else:
            sync_result = sync_processor()
        if progress_callback is not None:
            notify_skladbot_progress(progress_callback, "sync_processor_finished")
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
            with observed_worker_cycle("skladbot", interval) as cycle_id:
                last_progress_write = 0.0

                def persist_progress(phase: str) -> None:
                    nonlocal last_progress_write
                    current = time.monotonic()
                    if current - last_progress_write < 5.0:
                        return
                    last_progress_write = current
                    record_cycle_progress(
                        "skladbot",
                        phase,
                        correlation_id=cycle_id,
                    )

                run_worker_cycle(
                    progress_callback=persist_progress,
                    raise_on_error=True,
                )
        except Exception:
            logging.exception("SkladBot observed worker cycle failed")
        if once:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
