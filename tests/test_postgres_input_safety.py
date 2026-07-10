import os
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import main as backend_main
from backend.app.db import get_db
from backend.app.input_safety import MAX_IMPORT_ROWS, MAX_REQUEST_BODY_BYTES
from backend.app.models import (
    AuditLog,
    ClientPoint,
    ImportFile,
    ImportJob,
    Incident,
    Order,
    OrderItem,
    PendingEvent,
)
from tests.postgres_support import create_database, drop_database, run_alembic


POSTGRES_AVAILABLE = bool(os.environ.get("TAKSKLAD_TEST_DATABASE_URL"))


@unittest.skipUnless(POSTGRES_AVAILABLE, "disposable PostgreSQL URL not provided")
class PostgresInputSafetyTests(unittest.TestCase):
    database = "taksklad_input_safety"
    models = (ImportJob, ImportFile, Order, OrderItem, PendingEvent, AuditLog, Incident, ClientPoint)

    @classmethod
    def setUpClass(cls):
        cls.url = create_database(cls.database)
        run_alembic(cls.url, "upgrade", "head")
        cls.engine = create_engine(cls.url, pool_pre_ping=True)
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)
        cls.db_dependency_calls = 0

        def override_db():
            cls.db_dependency_calls += 1
            db = cls.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        backend_main.app.dependency_overrides[get_db] = override_db
        backend_main.app.dependency_overrides[backend_main.require_service_token] = lambda: None
        backend_main.app.dependency_overrides[backend_main.require_admin_write_permission] = lambda: None
        cls.client = TestClient(backend_main.app)

    @classmethod
    def tearDownClass(cls):
        if not POSTGRES_AVAILABLE:
            return
        backend_main.app.dependency_overrides.clear()
        cls.engine.dispose()
        drop_database(cls.database)

    @classmethod
    def snapshot(cls):
        with cls.SessionLocal() as db:
            return {model.__tablename__: db.query(model).count() for model in cls.models}

    def assert_rejected_without_writes(self, response, expected_status, forbidden_values=()):
        self.assertEqual(response.status_code, expected_status, response.text[:500])
        text = response.text
        for forbidden in forbidden_values:
            self.assertNotIn(forbidden, text)
        self.assertEqual(self.snapshot(), self.before)

    def setUp(self):
        self.before = self.snapshot()

    def test_declared_oversized_request_rejected_before_parser_and_db(self):
        before_calls = self.db_dependency_calls
        response = self.client.post(
            "/api/v1/imports",
            content=b'{"secret":"raw-customer-marker"}',
            headers={"Content-Type": "application/json", "Content-Length": str(MAX_REQUEST_BODY_BYTES + 1)},
        )
        self.assert_rejected_without_writes(response, 413, ("raw-customer-marker",))
        self.assertEqual(response.json(), {"detail": "request_too_large"})
        self.assertEqual(self.db_dependency_calls, before_calls)

    def test_streamed_oversized_request_rejected_before_parser_and_db(self):
        before_calls = self.db_dependency_calls
        chunk = b"x" * (1024 * 1024)
        response = self.client.post(
            "/api/v1/imports",
            content=(chunk for _ in range(25)),
            headers={"Content-Type": "application/json"},
        )
        self.assert_rejected_without_writes(response, 413)
        self.assertEqual(response.json(), {"detail": "request_too_large"})
        self.assertEqual(self.db_dependency_calls, before_calls)

    def test_structural_import_rejections_leave_all_tables_unchanged_and_redacted(self):
        fixtures = (
            (
                {"source": "excel", "filename": "../secret-client.xlsx", "rows": []},
                "secret-client.xlsx",
            ),
            (
                {
                    "source": "excel",
                    "filename": "synthetic.xlsx",
                    "rows": [{"Клиент": {"secret": "raw-customer-marker"}}],
                },
                "raw-customer-marker",
            ),
            (
                {
                    "source": "excel",
                    "filename": "synthetic.xlsx",
                    "rows": [{} for _ in range(MAX_IMPORT_ROWS + 1)],
                },
                "raw-customer-marker",
            ),
            (
                {
                    "source": "excel",
                    "filename": "synthetic.xlsx",
                    "rows": [{"x" * 129: "raw-customer-marker"}],
                },
                "raw-customer-marker",
            ),
            (
                {
                    "source": "excel",
                    "filename": "synthetic.xlsx",
                    "rows": [{"unexpected": "raw-customer-marker"}],
                },
                "raw-customer-marker",
            ),
            ({"source": "excel", "filename": "folder／secret-client.xlsx", "rows": []}, "secret-client"),
            ({"source": "excel", "filename": "client\u202esecret.xlsx", "rows": []}, "secret"),
        )
        for payload, forbidden in fixtures:
            with self.subTest(forbidden=forbidden):
                response = self.client.post("/api/v1/imports", json=payload)
                self.assert_rejected_without_writes(response, 422, (forbidden,))
                self.assertEqual(response.json(), {"detail": "invalid_request"})

    def test_raw_payload_depth_key_and_byte_rejections_are_redacted(self):
        payloads = (
            {"a": {"b": {"c": {"d": {"secret-customer-depth": "x"}}}}},
            {f"key-{index}": index for index in range(257)},
            {"secret-customer-bytes": "x" * 65_536},
        )
        for raw_payload in payloads:
            response = self.client.post(
                "/api/v1/admin/incidents",
                json={"source": "synthetic", "title": "Synthetic", "raw_payload": raw_payload},
            )
            self.assert_rejected_without_writes(response, 422, ("secret-customer",))
            self.assertEqual(response.json(), {"detail": "invalid_request"})


if __name__ == "__main__":
    unittest.main()
