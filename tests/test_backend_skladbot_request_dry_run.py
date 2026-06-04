import unittest
import uuid
from datetime import date
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import get_db
from backend.app.main import app, require_service_token
from backend.app.models import AuditLog, Base, ImportJob, Order, OrderItem, PendingEvent
from backend.app.skladbot_request_dry_run import (
    SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE,
    create_skladbot_dry_run_for_import,
    list_skladbot_dry_runs,
)


class BackendSkladBotRequestDryRunTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "dry_run"}, clear=False)
        self.env_patch.start()
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[require_service_token] = lambda: None
        self.client = TestClient(app)

    def tearDown(self):
        self.env_patch.stop()
        app.dependency_overrides.clear()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def seed_import_order(self, *, products=None, linked=False, payment_type="Перечисление"):
        products = products or [
            ("Chapman RED OP 20", 2),
            ("Chapman Brown OP 20", 3),
        ]
        with self.SessionLocal() as db:
            import_job = ImportJob(source="telegram", status="completed", rows_total=len(products), rows_imported=len(products))
            db.add(import_job)
            db.flush()
            order = Order(
                source="telegram",
                external_id="order-key",
                order_date=date(2026, 6, 5),
                payment_type=payment_type,
                client='"TEST CLIENT" MCHJ',
                address="Ташкент, улица Тестовая, 1",
                representative="ТП1",
                status="not_completed",
                raw_payload={
                    "source": "telegram",
                    "skladbot_request_number": "WH-R-1" if linked else "",
                    "skladbot_request_id": "123" if linked else "",
                },
            )
            db.add(order)
            db.flush()
            for index, (product, blocks) in enumerate(products, start=1):
                db.add(OrderItem(
                    order_id=order.id,
                    product=product,
                    quantity_pieces=blocks * 10,
                    quantity_blocks=blocks,
                    pieces_per_block=10,
                    scanned_blocks=0,
                    status="not_completed",
                    raw_payload={
                        "backend_import_id": str(import_job.id),
                        "source_file": "orders.xlsx",
                        "source_row": index,
                    },
                ))
            db.commit()
            return str(import_job.id), str(order.id)

    def test_builds_one_ready_payload_for_order_with_multiple_sku(self):
        import_id, order_id = self.seed_import_order()

        with self.SessionLocal() as db:
            summary = create_skladbot_dry_run_for_import(db, import_id)
            db.commit()
            dry_runs = list_skladbot_dry_runs(db, import_id)

        self.assertEqual(summary["ready"], 1)
        self.assertEqual(len(dry_runs), 1)
        row = dry_runs[0]
        self.assertEqual(row["order_id"], order_id)
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["blocks"], 5)
        self.assertEqual([product["quantity_blocks"] for product in row["products"]], [2, 3])
        self.assertEqual(row["payload"]["customer_id"], 6211)
        self.assertEqual(row["payload"]["request_type_id"], 3389)
        self.assertEqual(row["payload"]["fields"]["comment"]["value"], "Перечисление")
        self.assertEqual(row["payload"]["fields"]["unloading_date"]["value"], "2026-06-05")
        self.assertEqual(
            [product["product_data_id"] for product in row["payload"]["products"]],
            [2189390, 2189391],
        )
        self.assertEqual(
            [product["amount"] for product in row["payload"]["products"]],
            [2, 3],
        )

    def test_terminal_payment_payload_uses_payment_type_as_comment(self):
        import_id, _order_id = self.seed_import_order(payment_type="Терминал")

        with self.SessionLocal() as db:
            create_skladbot_dry_run_for_import(db, import_id)
            db.commit()
            row = list_skladbot_dry_runs(db, import_id)[0]

        self.assertEqual(row["payload"]["fields"]["comment"]["value"], "Терминал")

    def test_payload_uses_all_order_items_even_if_import_added_one_item(self):
        with self.SessionLocal() as db:
            import_job = ImportJob(source="telegram", status="completed", rows_total=1, rows_imported=1)
            db.add(import_job)
            db.flush()
            order = Order(
                source="telegram",
                external_id="order-key",
                order_date=date(2026, 6, 5),
                payment_type="Перечисление",
                client='"TEST CLIENT" MCHJ',
                address="Ташкент, улица Тестовая, 1",
                representative="ТП1",
                status="not_completed",
                raw_payload={"source": "telegram"},
            )
            db.add(order)
            db.flush()
            db.add_all([
                OrderItem(
                    order_id=order.id,
                    product="Chapman RED OP 20",
                    quantity_pieces=20,
                    quantity_blocks=2,
                    pieces_per_block=10,
                    scanned_blocks=0,
                    status="not_completed",
                    raw_payload={"backend_import_id": "older-import"},
                ),
                OrderItem(
                    order_id=order.id,
                    product="Chapman Brown OP 20",
                    quantity_pieces=30,
                    quantity_blocks=3,
                    pieces_per_block=10,
                    scanned_blocks=0,
                    status="not_completed",
                    raw_payload={"backend_import_id": str(import_job.id)},
                ),
            ])
            db.commit()
            import_id = str(import_job.id)

        with self.SessionLocal() as db:
            create_skladbot_dry_run_for_import(db, import_id)
            db.commit()
            row = list_skladbot_dry_runs(db, import_id)[0]

        self.assertEqual(row["blocks"], 5)
        self.assertEqual(
            [product["product_data_id"] for product in row["payload"]["products"]],
            [2189390, 2189391],
        )

    def test_unknown_sku_blocks_without_breaking_import(self):
        import_id, _order_id = self.seed_import_order(products=[("Unknown SKU", 1)])

        with self.SessionLocal() as db:
            summary = create_skladbot_dry_run_for_import(db, import_id)
            db.commit()
            row = list_skladbot_dry_runs(db, import_id)[0]

        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(row["status"], "blocked")
        self.assertIn("SKU не найден", row["error"])
        self.assertEqual(row["payload"], {})

    def test_already_linked_order_is_not_prepared_again(self):
        import_id, _order_id = self.seed_import_order(linked=True)

        with self.SessionLocal() as db:
            summary = create_skladbot_dry_run_for_import(db, import_id)
            db.commit()
            row = list_skladbot_dry_runs(db, import_id)[0]

        self.assertEqual(summary["already_linked"], 1)
        self.assertEqual(row["status"], "already_linked")
        self.assertEqual(row["payload"], {})

    def test_repeated_dry_run_does_not_create_duplicate_event(self):
        import_id, _order_id = self.seed_import_order()

        with self.SessionLocal() as db:
            first = create_skladbot_dry_run_for_import(db, import_id)
            second = create_skladbot_dry_run_for_import(db, import_id)
            db.commit()
            events = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE)
            ).scalars().all()

        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(second["status"], "deduplicated")
        self.assertEqual(len(events), 1)

    def test_enabled_mode_still_keeps_dry_run_effective_mode(self):
        import_id, _order_id = self.seed_import_order()

        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with self.SessionLocal() as db:
                summary = create_skladbot_dry_run_for_import(db, import_id)
                db.commit()
                event = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE)
                ).scalar_one()

        self.assertEqual(summary["mode"], "dry_run")
        self.assertEqual(event.payload["configured_mode"], "enabled")
        self.assertFalse(event.payload["would_post"])

    def test_import_endpoint_creates_dry_run_event_audit_and_keeps_would_post_false(self):
        response = self.client.post("/api/v1/imports", json={
            "source": "telegram",
            "filename": "orders.xlsx",
            "sha256": "a" * 64,
            "rows": [
                {
                    "Дата отгрузки": "05.06.2026",
                    "Тип оплаты": "Перечисление",
                    "Клиент": '"TEST CLIENT" MCHJ',
                    "Адрес": "Ташкент, улица Тестовая, 1",
                    "Торговый представитель": "ТП1",
                    "Товары": "Chapman Gold SSL 100`20",
                    "Кол-во ШТ": 10,
                    "Кол-во блок": 1,
                }
            ],
        })

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["skladbot_dry_run_status"], "ok")
        self.assertEqual(payload["skladbot_dry_run_ready"], 1)

        with self.SessionLocal() as db:
            events = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE)
            ).scalars().all()
            audit = db.execute(
                select(AuditLog).where(AuditLog.action == "skladbot_request_dry_run_built")
            ).scalars().all()

        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].payload["would_post"])
        self.assertEqual(events[0].payload["summary"]["ready"], 1)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0].payload["import_id"], payload["id"])

    def test_import_endpoint_survives_dry_run_failure(self):
        with mock.patch(
            "backend.app.imports_service.create_skladbot_dry_run_for_import",
            side_effect=RuntimeError("dry-run temporary failure"),
        ):
            response = self.client.post("/api/v1/imports", json={
                "source": "telegram",
                "filename": "orders.xlsx",
                "sha256": "b" * 64,
                "rows": [
                    {
                        "Дата отгрузки": "05.06.2026",
                        "Тип оплаты": "Перечисление",
                        "Клиент": '"TEST CLIENT" MCHJ',
                        "Адрес": "Ташкент, улица Тестовая, 1",
                        "Торговый представитель": "ТП1",
                        "Товары": "Chapman Brown OP 20",
                        "Кол-во ШТ": 10,
                        "Кол-во блок": 1,
                    }
                ],
            })

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["rows_imported"], 1)
        self.assertEqual(payload["skladbot_dry_run_status"], "error")

        with self.SessionLocal() as db:
            google_events = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")
            ).scalars().all()
            failed_audit = db.execute(
                select(AuditLog).where(AuditLog.action == "skladbot_request_dry_run_failed")
            ).scalars().all()
            import_job = db.get(ImportJob, uuid.UUID(payload["id"]))

        self.assertEqual(len(google_events), 1)
        self.assertEqual(google_events[0].payload["action"], "google_sheets_import_export")
        self.assertEqual(len(failed_audit), 1)
        self.assertEqual(import_job.raw_payload["skladbot_dry_run"]["status"], "error")

    def test_admin_api_lists_and_rebuilds_dry_runs(self):
        import_id, _order_id = self.seed_import_order()

        with self.SessionLocal() as db:
            summary = create_skladbot_dry_run_for_import(db, import_id)
            db.commit()

        list_response = self.client.get(f"/api/v1/admin/skladbot/dry-runs?import_id={import_id}")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.json()), 1)

        rebuild_response = self.client.post(
            f"/api/v1/admin/skladbot/dry-runs/{summary['event_id']}/rebuild"
        )
        self.assertEqual(rebuild_response.status_code, 200)
        self.assertEqual(len(rebuild_response.json()), 1)
