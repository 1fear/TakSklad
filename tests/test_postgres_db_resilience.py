import json
import os
import time
import unittest
from unittest import mock

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from backend.app import db as db_module
from backend.app.db_errors import classify_database_error, database_error_response
from backend.app.settings import ConfigurationError, load_settings
from tests.postgres_support import create_database, drop_database


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


def settings_for(database_url, **overrides):
    environ = {
        "DATABASE_URL": database_url,
        "TAKSKLAD_DB_POOL_SIZE": "2",
        "TAKSKLAD_DB_MAX_OVERFLOW": "1",
        "TAKSKLAD_DB_POOL_TIMEOUT_SECONDS": "2",
        "TAKSKLAD_DB_POOL_RECYCLE_SECONDS": "1800",
        "TAKSKLAD_DB_CONNECT_TIMEOUT_SECONDS": "5",
        "TAKSKLAD_DB_STATEMENT_TIMEOUT_MS": "5000",
        "TAKSKLAD_DB_LOCK_TIMEOUT_MS": "2000",
        "TAKSKLAD_DB_IDLE_TRANSACTION_TIMEOUT_MS": "10000",
    }
    environ.update({key: str(value) for key, value in overrides.items()})
    return load_settings(environ)


class SyntheticDatabaseError(Exception):
    def __init__(self, sqlstate):
        self.sqlstate = sqlstate
        super().__init__("synthetic database detail must not escape")


class DatabaseConfigurationTests(unittest.TestCase):
    def test_conservative_defaults_are_explicit(self):
        settings = load_settings({})

        self.assertEqual(settings.db_pool_size, 2)
        self.assertEqual(settings.db_max_overflow, 1)
        self.assertEqual(settings.db_pool_timeout_seconds, 2)
        self.assertEqual(settings.db_pool_recycle_seconds, 1800)
        self.assertEqual(settings.db_connect_timeout_seconds, 5)
        self.assertEqual(settings.db_statement_timeout_ms, 5000)
        self.assertEqual(settings.db_lock_timeout_ms, 2000)
        self.assertEqual(settings.db_idle_transaction_timeout_ms, 10000)

    def test_invalid_explicit_database_budgets_fail_closed(self):
        invalid_values = (
            ("TAKSKLAD_DB_POOL_SIZE", "0"),
            ("TAKSKLAD_DB_MAX_OVERFLOW", "-1"),
            ("TAKSKLAD_DB_POOL_TIMEOUT_SECONDS", "not-a-number"),
            ("TAKSKLAD_DB_STATEMENT_TIMEOUT_MS", "0"),
        )
        for name, value in invalid_values:
            with self.subTest(name=name), self.assertRaises(ConfigurationError) as captured:
                load_settings({name: value})
            self.assertIn(name, captured.exception.setting_names)

        with self.assertRaises(ConfigurationError) as captured:
            load_settings({
                "TAKSKLAD_DB_STATEMENT_TIMEOUT_MS": "1000",
                "TAKSKLAD_DB_LOCK_TIMEOUT_MS": "1000",
            })
        self.assertEqual(
            set(captured.exception.setting_names),
            {"TAKSKLAD_DB_LOCK_TIMEOUT_MS", "TAKSKLAD_DB_STATEMENT_TIMEOUT_MS"},
        )

    def test_postgresql_engine_kwargs_have_exact_budgets(self):
        settings = settings_for("postgresql+psycopg://synthetic@localhost/synthetic")

        kwargs = db_module.database_engine_kwargs(settings.database_url, settings)

        self.assertEqual(kwargs["pool_size"], 2)
        self.assertEqual(kwargs["max_overflow"], 1)
        self.assertEqual(kwargs["pool_timeout"], 2)
        self.assertEqual(kwargs["pool_recycle"], 1800)
        self.assertTrue(kwargs["pool_pre_ping"])
        self.assertTrue(kwargs["pool_use_lifo"])
        self.assertEqual(kwargs["pool_reset_on_return"], "rollback")
        self.assertEqual(kwargs["connect_args"]["connect_timeout"], 5)
        self.assertEqual(
            kwargs["connect_args"]["options"],
            "-c statement_timeout=5000 -c lock_timeout=2000 "
            "-c idle_in_transaction_session_timeout=10000",
        )

    def test_sqlite_engine_kwargs_do_not_receive_postgresql_options(self):
        settings = settings_for("sqlite+pysqlite:///:memory:")

        kwargs = db_module.database_engine_kwargs(settings.database_url, settings)

        self.assertEqual(kwargs["connect_args"], {"check_same_thread": False})
        self.assertIs(kwargs["poolclass"], StaticPool)
        self.assertNotIn("pool_size", kwargs)
        self.assertNotIn("max_overflow", kwargs)
        self.assertNotIn("pool_timeout", kwargs)

    def test_dependency_rolls_back_and_closes_on_database_error(self):
        session = mock.Mock()
        failure = SQLAlchemyTimeoutError("synthetic pool timeout")
        with mock.patch.object(db_module, "SessionLocal", return_value=session):
            dependency = db_module.get_db()
            self.assertIs(next(dependency), session)
            with self.assertRaises(SQLAlchemyTimeoutError):
                dependency.throw(failure)

        session.rollback.assert_called_once_with()
        session.close.assert_called_once_with()

    def test_sanitized_error_mapping_never_returns_database_details(self):
        cases = (
            (SQLAlchemyTimeoutError("postgresql://user:secret@host/db"), "database_busy", 503),
            (
                OperationalError("SELECT pg_sleep(99)", {}, SyntheticDatabaseError("57014")),
                "database_timeout",
                503,
            ),
            (
                OperationalError("UPDATE secret", {}, SyntheticDatabaseError("55P03")),
                "database_busy",
                503,
            ),
            (
                OperationalError("SELECT secret", {}, SyntheticDatabaseError("08006")),
                "database_unavailable",
                503,
            ),
        )
        for error, expected_code, expected_status in cases:
            with self.subTest(expected_code=expected_code):
                classification = classify_database_error(error)
                response = database_error_response(error)
                body = response.body.decode("utf-8")
                self.assertEqual(classification.code, expected_code)
                self.assertEqual(classification.http_status, expected_status)
                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(json.loads(body), {"detail": expected_code})
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["retry-after"], "1")
                self.assertNotIn("secret", body)
                self.assertNotIn("SELECT", body)


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresDatabaseResilienceTests(unittest.TestCase):
    database_name = "taksklad_phase17_db_resilience"

    @classmethod
    def setUpClass(cls):
        cls.url = create_database(cls.database_name)

    @classmethod
    def tearDownClass(cls):
        if POSTGRES_AVAILABLE:
            drop_database(cls.database_name)

    def make_engine(self, **overrides):
        return db_module.create_db_engine(settings_for(self.url, **overrides))

    def assert_sanitized(self, error, expected_code):
        response = database_error_response(error)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body.decode("utf-8")), {"detail": expected_code})

    def test_pool_exhaustion_is_bounded_sanitized_and_recovers(self):
        engine = self.make_engine(
            TAKSKLAD_DB_POOL_SIZE=1,
            TAKSKLAD_DB_MAX_OVERFLOW=0,
            TAKSKLAD_DB_POOL_TIMEOUT_SECONDS=1,
        )
        held = engine.connect()
        try:
            started = time.monotonic()
            with self.assertRaises(SQLAlchemyTimeoutError) as captured:
                engine.connect()
            elapsed = time.monotonic() - started

            self.assertGreaterEqual(elapsed, 0.8)
            self.assertLess(elapsed, 2.5)
            self.assertEqual(classify_database_error(captured.exception).code, "database_busy")
            self.assert_sanitized(captured.exception, "database_busy")
        finally:
            held.close()

        try:
            with engine.connect() as connection:
                self.assertEqual(connection.execute(text("SELECT 1")).scalar_one(), 1)
            self.assertEqual(engine.pool.checkedout(), 0)
        finally:
            engine.dispose()

    def test_statement_timeout_is_bounded_sanitized_and_recovers(self):
        engine = self.make_engine(
            TAKSKLAD_DB_STATEMENT_TIMEOUT_MS=200,
            TAKSKLAD_DB_LOCK_TIMEOUT_MS=100,
        )
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            with sessions() as session:
                started = time.monotonic()
                with self.assertRaises(SQLAlchemyError) as captured:
                    session.execute(text("SELECT pg_sleep(2)"))
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 1.5)
                self.assertEqual(classify_database_error(captured.exception).code, "database_timeout")
                self.assert_sanitized(captured.exception, "database_timeout")
                session.rollback()
                self.assertEqual(session.execute(text("SELECT 1")).scalar_one(), 1)
            self.assertEqual(engine.pool.checkedout(), 0)
        finally:
            engine.dispose()

    def test_lock_timeout_is_bounded_sanitized_and_recovers(self):
        engine = self.make_engine(
            TAKSKLAD_DB_POOL_SIZE=2,
            TAKSKLAD_DB_MAX_OVERFLOW=0,
            TAKSKLAD_DB_STATEMENT_TIMEOUT_MS=2000,
            TAKSKLAD_DB_LOCK_TIMEOUT_MS=200,
        )
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE resilience_lock_probe (id integer PRIMARY KEY, value integer NOT NULL)"))
            connection.execute(text("INSERT INTO resilience_lock_probe (id, value) VALUES (1, 0)"))

        holder = engine.connect()
        holder_transaction = holder.begin()
        holder.execute(text("UPDATE resilience_lock_probe SET value = value + 1 WHERE id = 1"))
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            with sessions() as session:
                started = time.monotonic()
                with self.assertRaises(SQLAlchemyError) as captured:
                    session.execute(text("UPDATE resilience_lock_probe SET value = value + 1 WHERE id = 1"))
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 1.5)
                self.assertEqual(classify_database_error(captured.exception).code, "database_busy")
                self.assert_sanitized(captured.exception, "database_busy")
                session.rollback()
        finally:
            holder_transaction.rollback()
            holder.close()

        try:
            with engine.begin() as connection:
                connection.execute(text("UPDATE resilience_lock_probe SET value = value + 1 WHERE id = 1"))
            with engine.connect() as connection:
                self.assertEqual(
                    connection.execute(text("SELECT value FROM resilience_lock_probe WHERE id = 1")).scalar_one(),
                    1,
                )
            self.assertEqual(engine.pool.checkedout(), 0)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
