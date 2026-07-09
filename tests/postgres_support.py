"""Disposable PostgreSQL helpers shared by integration test modules."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_DATABASE_URL = os.environ.get("TAKSKLAD_TEST_DATABASE_URL", "")


def database_url(name):
    return make_url(BASE_DATABASE_URL).set(database=name).render_as_string(hide_password=False)


def psycopg_url(name="postgres"):
    return make_url(BASE_DATABASE_URL).set(drivername="postgresql", database=name).render_as_string(hide_password=False)


def create_database(name):
    with psycopg.connect(psycopg_url(), autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return database_url(name)


def drop_database(name):
    with psycopg.connect(psycopg_url(), autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def run_alembic(url, *arguments):
    environment = os.environ.copy()
    environment.update({
        "DATABASE_URL": url,
        "TAKSKLAD_ENV": "test",
        "TAKSKLAD_API_TOKEN": "synthetic-only-test-token",
    })
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "backend/alembic.ini", *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"alembic {' '.join(arguments)} failed:\n{completed.stdout[-4000:]}")
    return completed.stdout


def scalar(url, statement):
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            return connection.exec_driver_sql(statement).scalar_one()
    finally:
        engine.dispose()


class TwoSessionBarrier:
    def __init__(self):
        self.started = threading.Barrier(2)
        self.completed = threading.Event()
        self.errors = queue.Queue()

    def worker_started(self):
        self.started.wait(timeout=5)

    def wait_for_worker(self):
        self.started.wait(timeout=5)

    def capture_error(self, exc):
        self.errors.put(exc)

    def mark_completed(self):
        self.completed.set()

    def assert_no_errors(self, testcase):
        testcase.assertEqual(list(self.errors.queue), [])
