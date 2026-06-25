import json
import unittest
import uuid
from datetime import date
from unittest import mock

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import get_db
from backend.app.main import app, require_service_token
from backend.app.models import AuditLog, Base, ImportJob, Incident, Order, OrderItem, PendingEvent, ScanCode
from backend.app.skladbot_request_dry_run import (
    SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
    SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE,
    create_skladbot_dry_run_for_import,
    list_skladbot_dry_runs,
    process_pending_skladbot_request_creates,
)
from backend.app.skladbot_return_requests import (
    SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE,
    process_pending_skladbot_return_request_creates,
    queue_skladbot_return_request_create,
)
from backend.app.skladbot_worker import SkladBotClient


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

    def seed_import_order(self, *, products=None, linked=False, payment_type="Перечисление", telegram_chat_id=""):
        products = products or [
            ("Chapman RED OP 20", 2),
            ("Chapman Brown OP 20", 3),
        ]
        with self.SessionLocal() as db:
            import_job = ImportJob(
                source="telegram",
                status="completed",
                rows_total=len(products),
                rows_imported=len(products),
                raw_payload={"telegram_chat_id": telegram_chat_id} if telegram_chat_id else {},
            )
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

    def confirmed_items_for_order(self, db, order_id):
        order = db.get(Order, uuid.UUID(order_id))
        return [
            {
                "item_id": str(item.id),
                "product": item.product,
                "sku": item.product,
                "quantity_blocks": item.quantity_blocks,
                "quantity_pieces": item.quantity_pieces,
            }
            for item in order.items
        ]

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
        self.assertIs(row["payload"]["notify"], True)
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

    def test_builds_payload_for_extended_chapman_sku(self):
        import_id, order_id = self.seed_import_order(products=[
            ("Chapman Brown SSL 100`20", 20),
            ("Chapman Green OP 20", 10),
            ("Chapman RED SSL 100 20", 15),
        ])

        with self.SessionLocal() as db:
            summary = create_skladbot_dry_run_for_import(db, import_id)
            db.commit()
            dry_runs = list_skladbot_dry_runs(db, import_id)

        self.assertEqual(summary["ready"], 1)
        row = dry_runs[0]
        self.assertEqual(row["order_id"], order_id)
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["blocks"], 45)
        self.assertEqual(
            [product["product_data_id"] for product in row["payload"]["products"]],
            [2189392, 2430805, 2189393],
        )
        self.assertEqual(
            [product["barcode"] for product in row["payload"]["products"]],
            ["4006396054067", "4006396104441", "4006396054036"],
        )
        self.assertEqual(
            [product["amount"] for product in row["payload"]["products"]],
            [20, 10, 15],
        )

    def test_sku_mapping_can_be_overridden_from_env_json(self):
        import_id, _order_id = self.seed_import_order(products=[("Chapman RED OP 20", 2)])
        mapping_override = json.dumps({
            "red:op": {
                "product_data_id": 999001,
                "barcode": "OVERRIDE-RED-OP",
                "is_main_barcode": True,
            }
        })

        with mock.patch.dict("os.environ", {"SKLADBOT_SKU_MAPPING_JSON": mapping_override}, clear=False):
            with self.SessionLocal() as db:
                summary = create_skladbot_dry_run_for_import(db, import_id)
                db.commit()
                row = list_skladbot_dry_runs(db, import_id)[0]

        self.assertEqual(summary["ready"], 1)
        product = row["payload"]["products"][0]
        self.assertEqual(product["product_data_id"], 999001)
        self.assertEqual(product["barcode"], "OVERRIDE-RED-OP")
        self.assertIs(product["is_main_barcode"], True)

    def test_invalid_sku_mapping_blocks_dry_run_without_create_event(self):
        import_id, _order_id = self.seed_import_order(products=[("Chapman RED OP 20", 2)])
        broken_mapping = json.dumps({
            "red:op": {
                "product_data_id": "broken",
                "barcode": "4006396053947",
                "is_main_barcode": False,
            }
        })

        with mock.patch.dict(
            "os.environ",
            {
                "SKLADBOT_CREATE_REQUESTS_MODE": "enabled",
                "SKLADBOT_SKU_MAPPING_JSON": broken_mapping,
            },
            clear=False,
        ):
            with self.SessionLocal() as db:
                summary = create_skladbot_dry_run_for_import(db, import_id)
                db.commit()
                row = list_skladbot_dry_runs(db, import_id)[0]
                create_events = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                ).scalars().all()

        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(summary["queued"], 0)
        self.assertEqual(row["status"], "blocked")
        self.assertIn("Ошибка настройки SKU mapping", row["error"])
        self.assertIn("SKLADBOT_SKU_MAPPING_JSON.red:op.product_data_id", row["error"])
        self.assertEqual(row["payload"], {})
        self.assertEqual(create_events, [])

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

    def test_enabled_mode_queues_create_event_without_posting_inline(self):
        import_id, _order_id = self.seed_import_order()

        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with self.SessionLocal() as db:
                summary = create_skladbot_dry_run_for_import(db, import_id)
                db.commit()
                dry_run_event = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE)
                ).scalar_one()
                create_event = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                ).scalar_one()

        self.assertEqual(summary["mode"], "enabled")
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(summary["ready"], 0)
        self.assertEqual(dry_run_event.payload["configured_mode"], "enabled")
        self.assertTrue(dry_run_event.payload["would_post"])
        self.assertEqual(create_event.status, "pending")
        self.assertEqual(create_event.payload["create_status"], "queued")

    def test_enabled_mode_creates_request_and_updates_order(self):
        import_id, order_id = self.seed_import_order()

        class FakeSkladBotClient:
            def __init__(self):
                self.created_payloads = []

            @property
            def configured(self):
                return True

            def create_request(self, payload):
                self.created_payloads.append(payload)
                return {
                    "data": {
                        "id": 777,
                        "delivery_number": "WH-R-777",
                        "created_at": "2026-06-04T12:00:00.000000Z",
                    }
                }

            def get_request_detail(self, request_id):
                self.request_id = request_id
                return {
                    "id": request_id,
                    "delivery_number": "WH-R-777",
                    "fields": [
                        {"field": "address", "value": "Ташкент, улица Тестовая, 1"},
                        {"field": "company_name", "value": '"TEST CLIENT" MCHJ'},
                        {"field": "unloading_date", "value": "2026-06-05"},
                        {"field": "comment", "value": "Перечисление"},
                    ],
                    "products": [
                        {"name": "Chapman RED OP 20", "barcode": "4006396053947", "amount": 2},
                        {"name": "Chapman Brown OP 20", "barcode": "4006396053978", "amount": 3},
                    ],
                }

            def list_requests(self):
                return []

        fake_client = FakeSkladBotClient()
        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with mock.patch("backend.app.skladbot_request_dry_run.SkladBotClient", return_value=fake_client):
                with self.SessionLocal() as db:
                    summary = create_skladbot_dry_run_for_import(db, import_id)
                    process_result = process_pending_skladbot_request_creates(db, client=fake_client)
                    db.commit()
                    order = db.get(Order, uuid.UUID(order_id))
                    dry_run_event = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE)
                    ).scalar_one()
                    create_event = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                    ).scalar_one()
                    google_event = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")
                    ).scalar_one()

        self.assertEqual(summary["mode"], "enabled")
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(summary["ready"], 0)
        self.assertEqual(process_result["created"], 1)
        self.assertEqual(len(fake_client.created_payloads), 1)
        self.assertIs(fake_client.created_payloads[0]["notify"], True)
        self.assertEqual(fake_client.request_id, 777)
        self.assertEqual(order.raw_payload["skladbot_request_number"], "WH-R-777")
        self.assertEqual(order.raw_payload["skladbot_request_id"], "777")
        self.assertEqual(order.raw_payload["skladbot_status"], "created")
        self.assertTrue(order.raw_payload["skladbot_created_by_taksklad"])
        self.assertEqual(google_event.payload["action"], "google_sheets_skladbot_export")
        self.assertIn(order_id, google_event.payload["order_ids"])
        self.assertEqual(google_event.payload["entity_id"], order_id)
        self.assertTrue(dry_run_event.payload["would_post"])
        self.assertEqual(create_event.status, "completed")
        self.assertEqual(create_event.payload["create_status"], "created")

    def test_enabled_mode_does_not_save_wh_r_without_canonical_detail(self):
        import_id, order_id = self.seed_import_order()

        class FakeSkladBotClient:
            def __init__(self):
                self.create_calls = 0

            @property
            def configured(self):
                return True

            def create_request(self, payload):
                self.create_calls += 1
                return {"data": {"id": 780, "delivery_number": "WH-R-STALE"}}

            def get_request_detail(self, request_id):
                raise RuntimeError("show endpoint unavailable")

            def list_requests(self):
                return []

        fake_client = FakeSkladBotClient()
        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with mock.patch("backend.app.skladbot_request_dry_run.SkladBotClient", return_value=fake_client):
                with self.SessionLocal() as db:
                    summary = create_skladbot_dry_run_for_import(db, import_id)
                    process_result = process_pending_skladbot_request_creates(db, client=fake_client)
                    db.commit()
                    order = db.get(Order, uuid.UUID(order_id))
                    create_event = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                    ).scalar_one()

        self.assertEqual(summary["queued"], 1)
        self.assertEqual(process_result["failed"], 1)
        self.assertEqual(fake_client.create_calls, 1)
        self.assertEqual(create_event.status, "failed")
        self.assertEqual(order.raw_payload["skladbot_status"], "create_failed")
        self.assertNotEqual(order.raw_payload.get("skladbot_request_number"), "WH-R-STALE")

    def test_enabled_mode_deduplicates_after_created_order_number(self):
        import_id, order_id = self.seed_import_order()

        class FakeSkladBotClient:
            def __init__(self):
                self.created_payloads = []

            @property
            def configured(self):
                return True

            def create_request(self, payload):
                self.created_payloads.append(payload)
                return {"data": {"id": 778, "delivery_number": "WH-R-778"}}

            def get_request_detail(self, request_id):
                return {"id": request_id, "delivery_number": "WH-R-778", "fields": [], "products": []}

            def list_requests(self):
                return []

        fake_client = FakeSkladBotClient()
        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with mock.patch("backend.app.skladbot_request_dry_run.SkladBotClient", return_value=fake_client):
                with self.SessionLocal() as db:
                    first = create_skladbot_dry_run_for_import(db, import_id)
                    process_pending_skladbot_request_creates(db, client=fake_client)
                    second = create_skladbot_dry_run_for_import(db, import_id, rebuild=True)
                    db.commit()
                    order = db.get(Order, uuid.UUID(order_id))

        self.assertEqual(first["queued"], 1)
        self.assertEqual(second["already_linked"], 1)
        self.assertEqual(len(fake_client.created_payloads), 1)
        self.assertEqual(order.raw_payload["skladbot_request_number"], "WH-R-778")

    def test_enabled_mode_recovers_created_request_after_timeout(self):
        import_id, order_id = self.seed_import_order()

        class FakeSkladBotClient:
            @property
            def configured(self):
                return True

            def create_request(self, payload):
                raise TimeoutError("temporary timeout")

            def list_requests(self):
                return [{"id": 779, "delivery_number": "WH-R-779", "type": "3PL отгрузка"}]

            def get_request_detail(self, request_id):
                return {
                    "id": request_id,
                    "delivery_number": "WH-R-779",
                    "type": "3PL отгрузка",
                    "fields": [
                        {"field": "address", "value": "Ташкент, улица Тестовая, 1"},
                        {"field": "company_name", "value": '"TEST CLIENT" MCHJ'},
                        {"field": "unloading_date", "value": "2026-06-05"},
                        {"field": "comment", "value": "Перечисление"},
                    ],
                    "products": [
                        {"name": "Chapman RED OP 20", "barcode": "4006396053947", "amount": 2},
                        {"name": "Chapman Brown OP 20", "barcode": "4006396053978", "amount": 3},
                    ],
                }

        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with mock.patch("backend.app.skladbot_request_dry_run.SkladBotClient", return_value=FakeSkladBotClient()):
                with self.SessionLocal() as db:
                    summary = create_skladbot_dry_run_for_import(db, import_id)
                    process_result = process_pending_skladbot_request_creates(db, client=FakeSkladBotClient())
                    db.commit()
                    order = db.get(Order, uuid.UUID(order_id))

        self.assertEqual(summary["queued"], 1)
        self.assertEqual(process_result["recovered"], 1)
        self.assertEqual(order.raw_payload["skladbot_request_number"], "WH-R-779")
        self.assertEqual(order.raw_payload["skladbot_status"], "created_recovered")

    def test_enabled_mode_records_error_without_breaking_import(self):
        import_id, order_id = self.seed_import_order()

        class FakeSkladBotClient:
            @property
            def configured(self):
                return True

            def create_request(self, payload):
                raise RuntimeError("SkladBot unavailable")

            def list_requests(self):
                return []

        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with mock.patch("backend.app.skladbot_request_dry_run.SkladBotClient", return_value=FakeSkladBotClient()):
                with self.SessionLocal() as db:
                    summary = create_skladbot_dry_run_for_import(db, import_id)
                    process_result = process_pending_skladbot_request_creates(db, client=FakeSkladBotClient())
                    db.commit()
                    order = db.get(Order, uuid.UUID(order_id))

        self.assertEqual(summary["queued"], 1)
        self.assertEqual(process_result["failed"], 1)
        self.assertEqual(order.raw_payload["skladbot_status"], "create_failed")
        self.assertNotIn("skladbot_request_number", {k: v for k, v in order.raw_payload.items() if v})

    def test_stock_shortage_create_failure_removes_unscanned_order_and_queues_cleanup(self):
        import_id, order_id = self.seed_import_order(telegram_chat_id="123")

        class FakeSkladBotClient:
            @property
            def configured(self):
                return True

            def create_request(self, payload):
                raise RuntimeError("Недостаточно товара на складе для создания заявки")

            def list_requests(self):
                return []

        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with mock.patch("backend.app.skladbot_request_dry_run.SkladBotClient", return_value=FakeSkladBotClient()):
                with self.SessionLocal() as db:
                    summary = create_skladbot_dry_run_for_import(db, import_id)
                    process_result = process_pending_skladbot_request_creates(db, client=FakeSkladBotClient())
                    db.commit()
                    order = db.get(Order, uuid.UUID(order_id))
                    item_count = db.execute(select(OrderItem)).scalars().all()
                    create_event = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                    ).scalar_one()
                    google_events = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")
                    ).scalars().all()
                    telegram_event = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == "telegram_notification")
                    ).scalar_one()
                    incident = db.execute(select(Incident).where(Incident.source == "skladbot_create")).scalar_one()

        self.assertEqual(summary["queued"], 1)
        self.assertEqual(process_result["stock_shortage_cancelled"], 1)
        self.assertEqual(process_result["failed"], 0)
        self.assertIsNone(order)
        self.assertEqual(item_count, [])
        self.assertEqual(create_event.status, "completed")
        self.assertEqual(create_event.payload["create_status"], "cancelled_stock_shortage")
        self.assertEqual(
            sorted(event.payload["action"] for event in google_events),
            ["google_sheets_delete_import_records_export"],
        )
        self.assertEqual(len(google_events[0].payload["records"]), 2)
        self.assertEqual(telegram_event.payload["chat_id"], "123")
        self.assertIn("Заказ отменён из-за недостатка товара", telegram_event.payload["text"])
        self.assertEqual(incident.status, "open")
        self.assertEqual(incident.pending_event_id, create_event.id)
        self.assertEqual(str(incident.import_id), import_id)
        self.assertEqual(incident.raw_payload["source_file"], "orders.xlsx")
        self.assertEqual(incident.raw_payload["products"][0]["sku"], "red:op")
        self.assertEqual(incident.raw_payload["products"][1]["sku"], "brown:op")

    def test_stock_shortage_create_failure_keeps_order_with_existing_scans(self):
        import_id, order_id = self.seed_import_order(telegram_chat_id="123")

        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            item = order.items[0]
            item.scanned_blocks = 1
            db.add(ScanCode(order_item_id=item.id, code="0104006396053947217TEST", source="desktop"))
            db.commit()

        class FakeSkladBotClient:
            @property
            def configured(self):
                return True

            def create_request(self, payload):
                raise RuntimeError("Недостаточно товара на складе для создания заявки")

            def list_requests(self):
                return []

        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with self.SessionLocal() as db:
                create_skladbot_dry_run_for_import(db, import_id)
                process_result = process_pending_skladbot_request_creates(db, client=FakeSkladBotClient())
                db.commit()
                order = db.get(Order, uuid.UUID(order_id))
                google_events = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")
                ).scalars().all()
                telegram_events = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == "telegram_notification")
                ).scalars().all()
                incident = db.execute(select(Incident).where(Incident.source == "skladbot_create")).scalar_one()
                create_event = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                ).scalar_one()

        self.assertEqual(process_result["failed"], 1)
        self.assertEqual(process_result["stock_shortage_cancelled"], 0)
        self.assertIsNotNone(order)
        self.assertEqual(order.raw_payload["skladbot_status"], "create_failed")
        self.assertEqual(google_events, [])
        self.assertEqual(telegram_events, [])
        self.assertEqual(create_event.status, "failed")
        self.assertEqual(incident.status, "manual_review")
        self.assertEqual(incident.pending_event_id, create_event.id)
        self.assertEqual(str(incident.order_id), order_id)
        self.assertIn("автоотмена пропущена", incident.message)

    def test_logistics_report_excludes_stock_shortage_blocked_order(self):
        _import_id, order_id = self.seed_import_order()
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            order.raw_payload = {
                **(order.raw_payload or {}),
                "coordinates": "41.31,69.27",
                "skladbot_status": "create_failed",
                "skladbot_error": "Недостаточно товара на складе для создания заявки",
            }
            db.commit()

        dates = self.client.get("/api/v1/logistics/dates")
        report = self.client.get("/api/v1/logistics/report?shipment_date=2026-06-05")

        self.assertEqual(dates.status_code, 200)
        self.assertEqual(dates.json(), [])
        self.assertEqual(report.status_code, 404)
        self.assertIn("No logistics delivery orders", report.json()["detail"])

    def test_skladbot_client_post_uses_bearer_without_printing_token(self):
        captured = {}

        class FakeHttpClient:
            def __init__(self, timeout):
                self.timeout = timeout

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url, json=None, headers=None):
                captured["url"] = url
                captured["json"] = json
                captured["headers"] = headers

                class Response:
                    status_code = 201
                    headers = {}

                    def raise_for_status(self):
                        return None

                    def json(self):
                        return {"data": {"id": 1, "delivery_number": "WH-R-1"}}

                return Response()

        with mock.patch.dict("os.environ", {"SKLADBOT_API_TOKEN": "secret-token", "SKLADBOT_API_TOKENS": ""}, clear=False):
            with mock.patch("backend.app.skladbot_worker.httpx.Client", FakeHttpClient):
                payload = {"customer_id": 6211, "request_type_id": 3389, "products": []}
                result = SkladBotClient().create_request(payload)

        self.assertEqual(result["data"]["delivery_number"], "WH-R-1")
        self.assertEqual(captured["url"], "https://api.skladbot.ru/v1/requests")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer secret-token")
        self.assertEqual(captured["json"], payload)

    def test_skladbot_client_post_does_not_retry_after_timeout(self):
        calls = {"count": 0}

        class FakeHttpClient:
            def __init__(self, timeout):
                self.timeout = timeout

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url, json=None, headers=None):
                calls["count"] += 1
                raise httpx.TimeoutException("ambiguous timeout")

        with mock.patch.dict(
            "os.environ",
            {
                "SKLADBOT_API_TOKEN": "",
                "SKLADBOT_API_TOKENS": "first-token,second-token",
                "SKLADBOT_API_MAX_RETRIES": "5",
            },
            clear=False,
        ):
            with mock.patch("backend.app.skladbot_worker.httpx.Client", FakeHttpClient):
                with self.assertRaises(httpx.TimeoutException):
                    SkladBotClient().create_request({"customer_id": 6211})

        self.assertEqual(calls["count"], 1)

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

    def test_return_create_event_is_idempotent_for_same_order(self):
        _import_id, order_id = self.seed_import_order()

        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            order.status = "returned"
            confirmed_items = self.confirmed_items_for_order(db, order_id)
            first = queue_skladbot_return_request_create(db, order, confirmed_items)
            second = queue_skladbot_return_request_create(db, order, confirmed_items)
            db.commit()
            events = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE)
            ).scalars().all()

        self.assertEqual(str(first.id), str(second.id))
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].idempotency_key.startswith("skladbot:return_create:v1:order:"))
        self.assertFalse(events[0].idempotency_key.startswith("skladbot:create:v1:order:"))

    def test_return_create_worker_creates_return_3pl_request_and_keeps_outgoing_wh_r(self):
        _import_id, order_id = self.seed_import_order()

        class FakeSkladBotClient:
            def __init__(self):
                self.created_payloads = []

            @property
            def configured(self):
                return True

            def create_request(self, payload):
                self.created_payloads.append(payload)
                return {"data": {"id": 190707, "delivery_number": "WH-R-190707"}}

            def get_request_detail(self, request_id):
                self.request_id = request_id
                return {
                    "id": request_id,
                    "delivery_number": "WH-R-190707",
                    "type": "Возврат 3PL",
                    "fields": [
                        {"field": "address", "value": "Ташкент, улица Тестовая, 1"},
                        {"field": "company_name", "value": '"TEST CLIENT" MCHJ'},
                        {"field": "unloading_date", "value": "2026-06-05"},
                        {"field": "comment", "value": "Перечисление"},
                    ],
                    "products": [
                        {"name": "Chapman RED OP 20", "barcode": "4006396053947", "amount": 2},
                        {"name": "Chapman Brown OP 20", "barcode": "4006396053978", "amount": 3},
                    ],
                }

        fake_client = FakeSkladBotClient()
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            order.status = "returned"
            order.raw_payload = {
                **(order.raw_payload or {}),
                "skladbot_request_number": "WH-R-OUT",
                "skladbot_request_id": "180000",
            }
            confirmed_items = self.confirmed_items_for_order(db, order_id)
            order.raw_payload = {
                **(order.raw_payload or {}),
                "skladbot_return_confirmed_items": confirmed_items,
            }
            queue_skladbot_return_request_create(db, order, confirmed_items)
            process_result = process_pending_skladbot_return_request_creates(db, client=fake_client)
            db.commit()
            order = db.get(Order, uuid.UUID(order_id))
            event = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE)
            ).scalar_one()
            google_event = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == "google_sheets_export")
            ).scalar_one()

        self.assertEqual(process_result["created"], 1)
        self.assertEqual(len(fake_client.created_payloads), 1)
        payload = fake_client.created_payloads[0]
        self.assertEqual(payload["request_type_id"], 3403)
        self.assertEqual(payload["customer_id"], 6211)
        self.assertIs(payload["notify"], True)
        self.assertEqual(payload["fields"]["company_name"]["value"], '"TEST CLIENT" MCHJ')
        self.assertEqual(payload["fields"]["unloading_date"]["value"], "2026-06-05")
        self.assertEqual([product["amount"] for product in payload["products"]], [2, 3])
        self.assertEqual(order.raw_payload["skladbot_request_number"], "WH-R-OUT")
        self.assertEqual(order.raw_payload["skladbot_request_id"], "180000")
        self.assertEqual(order.raw_payload["skladbot_return_request_number"], "WH-R-190707")
        self.assertEqual(order.raw_payload["skladbot_return_request_id"], "190707")
        self.assertEqual(order.raw_payload["skladbot_return_request_status"], "created")
        self.assertEqual(event.status, "completed")
        self.assertEqual(event.payload["create_status"], "created")
        self.assertEqual(google_event.payload["action"], "google_sheets_return_export")

    def test_return_create_worker_recovers_existing_request_on_retry_without_duplicate_post(self):
        _import_id, order_id = self.seed_import_order()

        class FakeSkladBotClient:
            @property
            def configured(self):
                return True

            def create_request(self, payload):
                raise AssertionError("retry must reconcile before POST /requests")

            def list_requests(self, type_id=None):
                self.request_type_id = type_id
                return [{"id": 190708, "delivery_number": "WH-R-190708", "type": "Возврат 3PL"}]

            def get_request_detail(self, request_id):
                return {
                    "id": request_id,
                    "delivery_number": "WH-R-190708",
                    "type": "Возврат 3PL",
                    "fields": [
                        {"field": "address", "value": "Ташкент, улица Тестовая, 1"},
                        {"field": "company_name", "value": '"TEST CLIENT" MCHJ'},
                        {"field": "unloading_date", "value": "2026-06-05"},
                        {"field": "comment", "value": "Перечисление"},
                    ],
                    "products": [
                        {"name": "Chapman RED OP 20", "barcode": "4006396053947", "amount": 2},
                        {"name": "Chapman Brown OP 20", "barcode": "4006396053978", "amount": 3},
                    ],
                }

        fake_client = FakeSkladBotClient()
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            order.status = "returned"
            order.raw_payload = {
                **(order.raw_payload or {}),
                "skladbot_request_number": "WH-R-OUT",
                "skladbot_request_id": "180000",
            }
            confirmed_items = self.confirmed_items_for_order(db, order_id)
            order.raw_payload = {
                **(order.raw_payload or {}),
                "skladbot_return_confirmed_items": confirmed_items,
            }
            event = queue_skladbot_return_request_create(db, order, confirmed_items)
            event.status = "failed"
            event.attempts = 1
            process_result = process_pending_skladbot_return_request_creates(db, client=fake_client)
            db.commit()
            order = db.get(Order, uuid.UUID(order_id))
            event = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE)
            ).scalar_one()

        self.assertEqual(fake_client.request_type_id, 3403)
        self.assertEqual(process_result["recovered"], 1)
        self.assertEqual(order.raw_payload["skladbot_return_request_number"], "WH-R-190708")
        self.assertEqual(order.raw_payload["skladbot_return_request_id"], "190708")
        self.assertEqual(order.raw_payload["skladbot_return_request_status"], "created_recovered")
        self.assertEqual(order.raw_payload["skladbot_request_number"], "WH-R-OUT")
        self.assertEqual(event.status, "completed")
        self.assertEqual(event.payload["create_status"], "created_recovered")

    def test_return_create_worker_blocks_unknown_sku_without_posting(self):
        _import_id, order_id = self.seed_import_order(products=[("Unknown SKU", 1)])

        class FakeSkladBotClient:
            @property
            def configured(self):
                return True

            def create_request(self, payload):
                raise AssertionError("POST /requests must not be called for blocked return")

        fake_client = FakeSkladBotClient()
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            order.status = "returned"
            confirmed_items = self.confirmed_items_for_order(db, order_id)
            order.raw_payload = {
                **(order.raw_payload or {}),
                "skladbot_return_confirmed_items": confirmed_items,
            }
            queue_skladbot_return_request_create(db, order, confirmed_items)
            process_result = process_pending_skladbot_return_request_creates(db, client=fake_client)
            db.commit()
            order = db.get(Order, uuid.UUID(order_id))
            event = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE)
            ).scalar_one()

        self.assertEqual(process_result["blocked"], 1)
        self.assertEqual(event.status, "blocked")
        self.assertIn("SKU не найден", event.last_error)
        self.assertEqual(order.raw_payload["skladbot_return_request_status"], "blocked")
        self.assertNotIn("skladbot_return_request_number", order.raw_payload)
