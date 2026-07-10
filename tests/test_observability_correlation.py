import logging
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.imports_service import create_import
from backend.app.google_sheets_pending import process_pending_google_sheets_exports
from backend.app.models import Base, ImportJob, PendingEvent
from backend.app.observability_context import (
    CorrelationIdMiddleware,
    bind_pending_event,
    bind_correlation_id,
    current_correlation_id,
    reset_correlation_id,
)
from backend.app.schemas import ImportCreate
from backend.app.settings import load_settings


class ObservabilityCorrelationTests(unittest.TestCase):
    def test_required_worker_configuration_is_explicit_and_bounded(self):
        settings = load_settings({
            "TAKSKLAD_REQUIRED_WORKERS": "telegram,skladbot,telegram",
        })
        self.assertEqual(settings.worker_heartbeat_required_names, ("telegram", "skladbot", "telegram"))

    def test_http_middleware_sanitizes_and_returns_correlation_id(self):
        app = FastAPI()
        app.add_middleware(CorrelationIdMiddleware)

        @app.get("/trace")
        def trace():
            return {"correlation_id": current_correlation_id()}

        client = TestClient(app)
        expected = "11111111-1111-4111-8111-111111111111"
        with self.assertLogs("taksklad.request", logging.INFO) as request_logs:
            accepted = client.get("/trace", headers={"X-Correlation-ID": expected})
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["correlation_id"], expected)
        self.assertEqual(accepted.headers["X-Correlation-ID"], expected)
        joined_logs = "\n".join(request_logs.output)
        self.assertIn(f"event=request_start correlation_id={expected}", joined_logs)
        self.assertIn(f"event=request_end correlation_id={expected}", joined_logs)
        self.assertIn("method=get", joined_logs)
        self.assertIn("route_group=other", joined_logs)

        rejected = client.get("/trace", headers={"X-Correlation-ID": "phone=998901234567"})
        generated = rejected.json()["correlation_id"]
        self.assertEqual(len(generated), 36)
        self.assertNotIn("998901234567", generated)
        self.assertEqual(rejected.headers["X-Correlation-ID"], generated)

    def test_import_persists_same_correlation_and_logs_only_bounded_trace_fields(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        correlation_id = "22222222-2222-4222-8222-222222222222"
        token = bind_correlation_id(correlation_id)
        try:
            with Session(engine) as db, self.assertLogs("backend.app.imports_service", logging.INFO) as captured:
                create_import(db, ImportCreate(source="synthetic", filename="synthetic.xlsx", rows=[]))
                persisted = db.query(ImportJob).one()
                self.assertEqual(persisted.raw_payload["correlation_id"], correlation_id)
        finally:
            reset_correlation_id(token)
            engine.dispose()
        joined = "\n".join(captured.output)
        self.assertIn(f"correlation_id={correlation_id}", joined)
        self.assertIn("event=import_created", joined)
        self.assertIn("event=import_finished", joined)
        self.assertNotIn("synthetic.xlsx", joined)

    def test_direct_pending_event_producer_and_external_consumer_keep_one_id(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        expected = "33333333-3333-4333-8333-333333333333"
        token = bind_correlation_id(expected)
        try:
            with Session(engine) as db:
                event = PendingEvent(event_type="synthetic_external", status="pending", payload={})
                db.add(event)
                db.commit()
                db.refresh(event)
                self.assertEqual(event.payload["correlation_id"], expected)
                observed_by_external_client = []
                with bind_pending_event(event):
                    observed_by_external_client.append(current_correlation_id())
                self.assertEqual(observed_by_external_client, [expected])
        finally:
            reset_correlation_id(token)
            engine.dispose()

    def test_google_event_rebinds_producer_id_at_external_call_boundary(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        expected = "44444444-4444-4444-8444-444444444444"
        producer_token = bind_correlation_id(expected)
        try:
            with Session(engine) as db:
                event = PendingEvent(
                    event_type="google_sheets_export",
                    action="synthetic_export",
                    aggregate_type="synthetic",
                    aggregate_id="bounded",
                    status="pending",
                    payload={"action": "synthetic_export", "entity_type": "synthetic", "entity_id": "bounded"},
                )
                db.add(event)
                db.commit()
            reset_correlation_id(producer_token)
            producer_token = None

            observed = []

            def fake_external(_db, _event):
                observed.append(current_correlation_id())
                return {"status": "completed"}

            with Session(engine) as db, mock.patch(
                "backend.app.google_sheets_pending.run_google_sheets_export_event",
                side_effect=fake_external,
            ):
                result = process_pending_google_sheets_exports(db, limit=1)
            self.assertEqual(result["synced"], 1)
            self.assertEqual(observed, [expected])
        finally:
            if producer_token is not None:
                reset_correlation_id(producer_token)
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
