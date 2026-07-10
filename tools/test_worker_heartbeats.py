#!/usr/bin/env python3
"""Run the deterministic worker heartbeat hang/failure/recovery fault matrix."""

import argparse
import ast
from datetime import datetime, timedelta, timezone
import inspect
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.models import Base, WorkerHeartbeat
from backend.app.worker_observability import KNOWN_WORKERS, build_worker_readiness, observed_worker_cycle
from backend.app import google_sheets_sync_worker, skladbot_worker_runner, smartup_auto_import_worker, telegram_worker


def verify_real_main_loop_sources() -> int:
    entrypoints = {
        "google_sheets_sync": google_sheets_sync_worker.main,
        "skladbot": skladbot_worker_runner.main,
        "smartup_auto_import": smartup_auto_import_worker.main,
        "telegram": telegram_worker.main,
    }
    for worker_name, entrypoint in entrypoints.items():
        tree = ast.parse(inspect.getsource(entrypoint))
        observed_names = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "observed_worker_cycle"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        }
        if worker_name not in observed_names:
            raise AssertionError(f"{worker_name} main loop does not originate its heartbeat")
    return len(entrypoints)


def run_fault_matrix() -> dict:
    source_loops = verify_real_main_loop_sources()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    for worker_name in KNOWN_WORKERS:
        with observed_worker_cycle(worker_name, 10, grace_seconds=3, session_factory=session_factory):
            pass

    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        healthy = build_worker_readiness(db, required_workers=KNOWN_WORKERS, now=now)
        if healthy["status"] != "ok":
            raise AssertionError("initial worker matrix is not healthy")
        target = db.get(WorkerHeartbeat, "telegram")
        target.status = "running"
        target.last_cycle_started_at = now - timedelta(seconds=24)
        db.commit()
        hung = build_worker_readiness(db, required_workers=KNOWN_WORKERS, now=now)
        if hung["unhealthy"] != ["telegram"]:
            raise AssertionError("hung loop did not become unhealthy after 2*interval+grace")

    with observed_worker_cycle("telegram", 10, grace_seconds=3, session_factory=session_factory):
        pass
    with Session(engine) as db:
        recovered = build_worker_readiness(db, required_workers=KNOWN_WORKERS)
        if recovered["status"] != "ok":
            raise AssertionError("recovered loop remained unhealthy")

    try:
        with observed_worker_cycle("skladbot", 10, grace_seconds=3, session_factory=session_factory):
            raise RuntimeError("synthetic")
    except RuntimeError:
        pass
    with Session(engine) as db:
        failed = build_worker_readiness(db, required_workers=KNOWN_WORKERS)
        if failed["unhealthy"] != ["skladbot"]:
            raise AssertionError("failed loop did not become unhealthy")
        failure_row = db.get(WorkerHeartbeat, "skladbot")
        if failure_row.last_error_class != "RuntimeError":
            raise AssertionError("failure evidence stores anything except bounded exception class")

    with observed_worker_cycle("skladbot", 10, grace_seconds=3, session_factory=session_factory):
        pass
    with Session(engine) as db:
        final = build_worker_readiness(db, required_workers=KNOWN_WORKERS)
        if final["status"] != "ok":
            raise AssertionError("failure recovery did not close worker health")
    engine.dispose()
    return {
        "workers": len(KNOWN_WORKERS),
        "source_loops": source_loops,
        "hung_unhealthy_after_seconds": 23,
        "hung_observed_at_seconds": 24,
        "failure_status": "unhealthy",
        "recovery_status": "ok",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fault-matrix", action="store_true", required=True)
    parser.parse_args(argv)
    result = run_fault_matrix()
    sys.stdout.write(
        "worker-heartbeats: "
        + " ".join(f"{key}={value}" for key, value in result.items())
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
