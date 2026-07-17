import json
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import get_db
from backend.app.main import app, require_service_token
from backend.app.models import AuditLog, Base, ImportJob, Incident, Order, OrderItem, PendingEvent, RepresentativeContact, ScanCode, SmartupFulfillment, SmartupFulfillmentOrder
from backend.app.representative_contacts import normalize_representative_name
from backend.app.settings import load_settings
from backend.app.skladbot_client import SkladBotApiError, SkladBotErrorKind
from backend.app.skladbot_contracts import taksklad_marker_from_comment
from backend.app.skladbot_request_dry_run import (
    SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
    SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE,
    create_skladbot_dry_run_for_import,
    create_skladbot_dry_run_for_orders,
    list_skladbot_dry_runs,
    markerless_skladbot_request_payload,
    process_skladbot_create_event,
    process_pending_skladbot_request_creates,
    queue_skladbot_create_events,
    reset_stale_skladbot_create_events,
)
from backend.app.skladbot_return_requests import (
    SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE,
    process_pending_skladbot_return_request_creates,
    queue_skladbot_return_request_create,
)
from backend.app.skladbot_worker import SkladBotClient
from backend.app.smartup_saga import (
    get_or_create_fulfillment,
    link_fulfillment_order_event,
    link_fulfillment_orders,
    transition_fulfillment,
)


class BackendSkladBotRequestDryRunTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "dry_run"}, clear=False)
        self.env_patch.start()
        self.settings_patch = mock.patch(
            "backend.app.main.settings",
            load_settings({
                "TAKSKLAD_ENV": "local",
                "TAKSKLAD_INSECURE_LOCAL_ANONYMOUS": "true",
            }),
        )
        self.settings_patch.start()
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
        self.settings_patch.stop()
        self.env_patch.stop()
        app.dependency_overrides.clear()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def seed_import_order(
        self,
        *,
        products=None,
        linked=False,
        payment_type="Перечисление",
        telegram_chat_id="",
        representative="ТП1",
    ):
        products = products or [
            ("Chapman RED OP 20", 2),
            ("Chapman Brown OP 20", 3),
        ]
        with self.SessionLocal() as db:
            order_raw_payload = {
                "source": "telegram",
                "skladbot_request_number": "WH-R-1" if linked else "",
                "skladbot_request_id": "123" if linked else "",
            }
            if linked:
                order_raw_payload["skladbot_create_request_payload"] = {
                    "products": [{"amount": blocks} for _product, blocks in products]
                }
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
                representative=representative,
                status="not_completed",
                raw_payload=order_raw_payload,
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
        self.assertEqual(row["payload"]["comment"], "Перечисление\nТП1")
        self.assertEqual(row["payload"]["fields"]["comment"]["value"], row["payload"]["comment"])
        self.assertNotIn("TakSklad ref:", row["payload"]["comment"])
        self.assertEqual(row["smartup_id"], "")
        self.assertEqual(row["skladbot_request_number"], "")
        self.assertEqual(row["skladbot_request_id"], "")
        self.assertEqual(row["skladbot_return_request_number"], "")
        self.assertEqual(row["skladbot_return_request_id"], "")
        self.assertNotIn("TSF-", row["payload"]["comment"])
        self.assertEqual(row["payload"]["fields"]["unloading_date"]["value"], "2026-06-05")
        self.assertEqual(
            [product["product_data_id"] for product in row["payload"]["products"]],
            [2189390, 2189391],
        )
        self.assertEqual(
            [product["amount"] for product in row["payload"]["products"]],
            [2, 3],
        )

    def test_dry_run_projection_uses_persisted_order_correlations(self):
        import_id, order_id = self.seed_import_order(linked=True)
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            order.raw_payload = {
                **(order.raw_payload or {}),
                "source_order_id": "smartup:731",
                "skladbot_request_number": "WH-R-DRY-1",
                "skladbot_request_id": "1001",
                "skladbot_return_request_number": "WR-DRY-1",
                "skladbot_return_request_id": "2001",
            }
            order.items[0].raw_payload = {
                **(order.items[0].raw_payload or {}),
                "source_order_id": "smartup:732",
            }
            db.commit()
            create_skladbot_dry_run_for_import(db, import_id)
            db.commit()
            row = list_skladbot_dry_runs(db, import_id)[0]

        self.assertEqual(row["smartup_id"], "731, 732")
        self.assertEqual(row["skladbot_request_number"], "WH-R-DRY-1")
        self.assertEqual(row["skladbot_request_id"], "1001")
        self.assertEqual(row["skladbot_return_request_number"], "WR-DRY-1")
        self.assertEqual(row["skladbot_return_request_id"], "2001")

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

    def test_aggregates_duplicate_sku_before_skladbot_payload(self):
        import_id, order_id = self.seed_import_order(products=[
            ("Chapman Green OP 20", 1),
            ("Chapman Brown SSL 100`20", 1),
            ("Chapman Green OP 20", 1),
            ("Chapman Brown OP 20", 1),
            ("Chapman RED SSL 100 20", 1),
        ])

        with self.SessionLocal() as db:
            summary = create_skladbot_dry_run_for_import(db, import_id)
            db.commit()
            dry_runs = list_skladbot_dry_runs(db, import_id)

        self.assertEqual(summary["ready"], 1)
        row = dry_runs[0]
        self.assertEqual(row["order_id"], order_id)
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["blocks"], 5)
        self.assertEqual(
            [product["product_data_id"] for product in row["payload"]["products"]],
            [2430805, 2189392, 2189391, 2189393],
        )
        self.assertEqual(
            [product["amount"] for product in row["payload"]["products"]],
            [2, 1, 1, 1],
        )
        self.assertEqual(row["products"][0]["quantity_blocks"], 2)
        self.assertEqual(len(row["products"][0]["source_products"]), 2)

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

    def test_terminal_payment_payload_uses_payment_type_and_representative_as_comment(self):
        import_id, _order_id = self.seed_import_order(payment_type="Терминал")

        with self.SessionLocal() as db:
            create_skladbot_dry_run_for_import(db, import_id)
            db.commit()
            row = list_skladbot_dry_runs(db, import_id)[0]

        self.assertEqual(row["payload"]["comment"], "Терминал\nТП1")
        self.assertEqual(row["payload"]["fields"]["comment"]["value"], row["payload"]["comment"])

    def test_smartup_order_payload_keeps_internal_id_out_of_public_comments(self):
        import_id, order_id = self.seed_import_order()

        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            order.raw_payload = {
                **(order.raw_payload or {}),
                "source_order_id": "smartup:731",
            }
            db.commit()
            create_skladbot_dry_run_for_import(db, import_id)
            db.commit()
            row = list_skladbot_dry_runs(db, import_id)[0]

        self.assertEqual(row["payload"]["comment"], "Перечисление\nТП1")
        self.assertEqual(row["payload"]["fields"]["comment"]["value"], row["payload"]["comment"])
        self.assertNotIn("Smartup", row["payload"]["comment"])
        self.assertNotIn("731", row["payload"]["comment"])
        self.assertNotIn("TakSklad ref:", row["payload"]["comment"])
        self.assertNotIn("TSF-", row["payload"]["comment"])

    def test_skladbot_payload_uses_contact_for_tp_code_and_phones_without_work_zone(self):
        import_id, _order_id = self.seed_import_order(
            payment_type="Терминал",
            representative="Суюнбеков Умид",
        )

        with self.SessionLocal() as db:
            db.add(RepresentativeContact(
                name="ТП-1 Умид",
                normalized_name=normalize_representative_name("ТП-1 Умид"),
                work_phone="+998 91 111 11 11",
                personal_phone="+998 90 222 22 22",
                work_zone="Юнусабад",
                is_active=True,
            ))
            db.commit()
            create_skladbot_dry_run_for_import(db, import_id)
            db.commit()
            row = list_skladbot_dry_runs(db, import_id)[0]

        self.assertEqual(
            row["payload"]["fields"]["comment"]["value"],
            "Терминал\n"
            "ТП1 Суюнбеков Умид\n"
            "Рабочий номер: +998 91 111 11 11\n"
            "Личный номер: +998 90 222 22 22",
        )
        self.assertEqual(row["payload"]["fields"]["comment"]["value"], row["payload"]["comment"])

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

    def test_linked_order_with_late_repair_item_reports_skladbot_mismatch(self):
        _import_id, order_id = self.seed_import_order(linked=True)

        with self.SessionLocal() as db:
            repair_import = ImportJob(source="smartup_auto_repair", status="completed", rows_total=1, rows_imported=1)
            db.add(repair_import)
            db.flush()
            db.add(OrderItem(
                order_id=uuid.UUID(order_id),
                product="Chapman Green OP 20",
                quantity_pieces=10,
                quantity_blocks=1,
                pieces_per_block=10,
                scanned_blocks=0,
                status="not_completed",
                raw_payload={
                    "backend_import_id": str(repair_import.id),
                    "source_file": "repair.xlsx",
                    "source_row": 19,
                    "source_order_id": "smartup:257984858",
                    "source_import_id": "smartup:257984858:1541071310:1",
                },
            ))
            db.commit()
            repair_import_id = str(repair_import.id)

        with self.SessionLocal() as db:
            summary = create_skladbot_dry_run_for_import(db, repair_import_id)
            db.commit()
            row = list_skladbot_dry_runs(db, repair_import_id)[0]
            create_events = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
            ).scalars().all()

        self.assertEqual(summary["status"], "mismatch")
        self.assertEqual(summary["linked_mismatch"], 1)
        self.assertEqual(summary["already_linked"], 0)
        self.assertEqual(row["status"], "linked_mismatch")
        self.assertEqual(row["blocks"], 6)
        self.assertEqual(row["linked_skladbot_blocks"], 5)
        self.assertEqual(row["linked_skladbot_source"], "skladbot_create_request_payload.products")
        self.assertIn("в БД 6 блок", row["error"])
        self.assertIn("в SkladBot 5 блок", row["error"])
        self.assertEqual(row["payload"], {})
        self.assertEqual(create_events, [])

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

    def test_explicit_order_dry_run_queues_canonical_order_for_duplicate_only_retry_import(self):
        _original_import_id, order_id = self.seed_import_order()
        with self.SessionLocal() as db:
            retry_import = ImportJob(
                source="smartup",
                status="completed",
                rows_total=2,
                rows_imported=0,
                raw_payload={"smartup_deal_id": "259704266", "duplicate_rows": 2},
            )
            db.add(retry_import)
            db.flush()
            retry_import_id = str(retry_import.id)

            summary = create_skladbot_dry_run_for_orders(
                db,
                [order_id],
                import_id=retry_import_id,
                force_mode="enabled",
            )
            second_summary = create_skladbot_dry_run_for_orders(
                db,
                [order_id],
                import_id=str(uuid.uuid4()),
                force_mode="enabled",
            )
            db.commit()
            create_events = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
            ).scalars().all()
            create_event = create_events[0]

        self.assertEqual(summary["orders"], 1)
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(summary["ready"], 0)
        self.assertEqual(second_summary["queued"], 1)
        self.assertEqual(len(create_events), 1)
        self.assertEqual(create_event.aggregate_id, order_id)
        self.assertEqual(create_event.payload["order_id"], order_id)
        self.assertEqual(create_event.payload["import_id"], retry_import_id)
        self.assertRegex(create_event.payload["taksklad_marker"], r"^TakSklad ref: TSF-[A-F0-9]{24}$")
        request_payload = create_event.payload["request_payload"]
        self.assertEqual(request_payload["comment"], request_payload["fields"]["comment"]["value"])
        self.assertNotIn("TakSklad ref:", request_payload["comment"])
        self.assertNotIn("TSF-", request_payload["comment"])

    def test_legacy_ready_payload_is_sanitized_before_its_first_post(self):
        import_id, _order_id = self.seed_import_order()
        legacy_marker = "TakSklad ref: TSF-AAAAAAAAAAAAAAAAAAAAAAAA"

        with self.SessionLocal() as db:
            create_skladbot_dry_run_for_import(db, import_id)
            db.commit()
            row = list_skladbot_dry_runs(db, import_id)[0]
            legacy_comment = f"{row['payload']['comment']}\nSmartup ID: smartup:731\n{legacy_marker}"
            row["payload"]["comment"] = legacy_comment
            row["payload"]["fields"]["comment"]["value"] = legacy_comment
            queued = queue_skladbot_create_events(db, import_id, [row])
            db.commit()
            event = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
            ).scalar_one()

        request_payload = event.payload["request_payload"]
        self.assertEqual(queued, 1)
        self.assertEqual(event.payload["taksklad_marker"], legacy_marker)
        self.assertEqual(request_payload["comment"], "Перечисление\nТП1")
        self.assertEqual(request_payload["comment"], request_payload["fields"]["comment"]["value"])
        self.assertNotIn("TakSklad ref:", request_payload["comment"])
        self.assertNotIn("Smartup", request_payload["comment"])
        self.assertNotIn("731", request_payload["comment"])

    def test_markerless_sanitizer_uses_canonical_full_line_marker_semantics(self):
        business_sentence = (
            "Комментарий клиента содержит текст taksklad ref: "
            "TSF-EEEEEEEEEEEEEEEEEEEEEEEE внутри предложения"
        )
        smartup_business_line = "ТП Smartup Team"
        smartup_sentence = "Клиент просит не добавлять Smartup ID в комментарий"
        business_lookalike_line = "smartup id: 731"
        payload = {
            "comment": "\n".join([
                "Перечисление",
                "taksklad ref: TSF-AAAAAAAAAAAAAAAAAAAAAAAA",
                "TakSklad   ref: TSF-BBBBBBBBBBBBBBBBBBBBBBBB",
                "TaKsKlAd\tref:\tTSF-CCCCCCCCCCCCCCCCCCCCCCCC",
                "  TakSklad ref: TSF-DDDDDDDDDDDDDDDDDDDDDDDD  ",
                "Smartup ID: smartup:731",
                business_lookalike_line,
                business_sentence,
                smartup_business_line,
                smartup_sentence,
            ]),
            "fields": {"comment": {"value": "stale comment"}},
        }

        result = markerless_skladbot_request_payload(payload)

        self.assertEqual(result["comment"], "\n".join([
            "Перечисление",
            business_lookalike_line,
            business_sentence,
            smartup_business_line,
            smartup_sentence,
        ]))
        self.assertEqual(result["comment"], result["fields"]["comment"]["value"])
        self.assertEqual(taksklad_marker_from_comment(result["comment"]), "")
        self.assertIn(business_sentence, result["comment"])
        self.assertIn(smartup_business_line, result["comment"])
        self.assertIn(smartup_sentence, result["comment"])
        self.assertNotIn("Smartup ID: smartup:731", result["comment"])

    def test_enabled_mode_queues_create_event_without_posting_inline(self):
        import_id, _order_id = self.seed_import_order()

        with mock.patch.dict("os.environ", {
            "SKLADBOT_CREATE_REQUESTS_MODE": "enabled",
            "TAKSKLAD_EVENT_LEASES_ENABLED": "1",
        }, clear=False):
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
                    create_event = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                    ).scalar_one()
                    fulfillment = get_or_create_fulfillment(
                        db,
                        source_scope="smartup:synthetic",
                        deal_id="synthetic-deal",
                        target_status="B#W",
                        payload={"deal_id": "synthetic-deal", "order_ids": [order_id]},
                        canonical_import_id=uuid.UUID(import_id),
                    )
                    link_fulfillment_orders(db, fulfillment, [uuid.UUID(order_id)])
                    transition_fulfillment(db, fulfillment, "smartup_write_started")
                    transition_fulfillment(db, fulfillment, "smartup_confirmed")
                    link_fulfillment_order_event(
                        db,
                        fulfillment,
                        uuid.UUID(order_id),
                        create_event=create_event,
                    )
                    transition_fulfillment(db, fulfillment, "skladbot_create_queued")
                    db.commit()
                    process_result = process_pending_skladbot_request_creates(db, client=fake_client)
                    db.commit()
                    order = db.get(Order, uuid.UUID(order_id))
                    fulfillment = db.execute(select(SmartupFulfillment)).scalar_one()
                    fulfillment_remote_request_id = fulfillment.order_links[0].remote_request_id
                    dry_run_event = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE)
                    ).scalar_one()
                    create_event = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                    ).scalar_one()

        self.assertEqual(summary["mode"], "enabled")
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(summary["ready"], 0)
        self.assertEqual(process_result["created"], 1)
        self.assertEqual(len(fake_client.created_payloads), 1)
        self.assertIs(fake_client.created_payloads[0]["notify"], True)
        self.assertEqual(
            fake_client.created_payloads[0]["comment"],
            fake_client.created_payloads[0]["fields"]["comment"]["value"],
        )
        self.assertNotIn("TakSklad ref:", fake_client.created_payloads[0]["comment"])
        self.assertNotIn("TSF-", fake_client.created_payloads[0]["comment"])
        self.assertEqual(fake_client.request_id, 777)
        self.assertEqual(order.raw_payload["skladbot_request_number"], "WH-R-777")
        self.assertEqual(order.raw_payload["skladbot_request_id"], "777")
        self.assertEqual(order.raw_payload["skladbot_status"], "created")
        self.assertTrue(order.raw_payload["skladbot_created_by_taksklad"])
        self.assertTrue(dry_run_event.payload["would_post"])
        self.assertEqual(create_event.status, "completed")
        self.assertIsNone(create_event.lease_owner)
        self.assertIsNotNone(create_event.completed_at)
        self.assertEqual(create_event.payload["create_status"], "created")
        self.assertEqual(fulfillment.state, "skladbot_created")
        self.assertEqual(fulfillment_remote_request_id, "777")

    def test_new_queued_event_with_zero_attempts_posts_exactly_once(self):
        import_id, order_id = self.seed_import_order()

        class FakeSkladBotClient:
            def __init__(self):
                self.create_calls = 0

            @property
            def configured(self):
                return True

            def create_request(self, payload):
                self.create_calls += 1
                return {"data": {"id": 778}}

            def get_request_detail(self, request_id):
                return {"id": request_id, "delivery_number": "WH-R-778", "fields": [], "products": []}

        fake_client = FakeSkladBotClient()
        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with self.SessionLocal() as db:
                create_skladbot_dry_run_for_import(db, import_id)
                event = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                ).scalar_one()
                self.assertEqual(event.attempts, 0)
                result = process_pending_skladbot_request_creates(db, client=fake_client)
                db.commit()
                order = db.get(Order, uuid.UUID(order_id))
                db.refresh(event)

        self.assertEqual(result["created"], 1)
        self.assertEqual(fake_client.create_calls, 1)
        self.assertEqual(event.attempts, 1)
        self.assertEqual(order.raw_payload["skladbot_request_number"], "WH-R-778")

    def test_direct_processor_rejects_foreign_aggregate_before_any_remote_or_order_action(self):
        import_id, order_id = self.seed_import_order()

        class FakeSkladBotClient:
            def __init__(self):
                self.create_calls = 0
                self.list_calls = 0
                self.detail_calls = 0

            def create_request(self, payload):
                self.create_calls += 1
                raise AssertionError("invalid ownership must not POST")

            def list_requests(self):
                self.list_calls += 1
                raise AssertionError("invalid ownership must not list")

            def get_request_detail(self, request_id):
                self.detail_calls += 1
                raise AssertionError("invalid ownership must not GET detail")

        fake_client = FakeSkladBotClient()
        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with self.SessionLocal() as db:
                create_skladbot_dry_run_for_import(db, import_id)
                event = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                ).scalar_one()
                order = db.get(Order, uuid.UUID(order_id))
                order_payload_before = json.loads(json.dumps(order.raw_payload or {}))
                event.aggregate_id = str(uuid.uuid4())
                event.payload = {
                    **(event.payload or {}),
                    "post_state": "ambiguous",
                    "create_status": "ambiguous",
                    "post_response_request_id": 777,
                }
                result = process_skladbot_create_event(db, event, fake_client)
                db.commit()
                db.refresh(order)
                db.refresh(event)
                incident = db.execute(
                    select(Incident)
                    .where(Incident.pending_event_id == event.id)
                    .where(Incident.title == "Invalid SkladBot create event ownership")
                ).scalar_one()
                audit = db.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "skladbot_create_event_ownership_invalid")
                    .where(AuditLog.entity_id == str(event.id))
                ).scalar_one()

        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(fake_client.create_calls, 0)
        self.assertEqual(fake_client.list_calls, 0)
        self.assertEqual(fake_client.detail_calls, 0)
        self.assertEqual(order.raw_payload, order_payload_before)
        self.assertEqual(order.raw_payload["skladbot_request_id"], "")
        self.assertEqual(order.raw_payload["skladbot_request_number"], "")
        self.assertEqual(event.status, "blocked")
        self.assertEqual(incident.status, "manual_review")
        self.assertEqual(audit.payload["reason"], "ownership_mismatch")

    def test_multi_order_fulfillment_waits_for_every_skladbot_request(self):
        first_import_id, first_order_id = self.seed_import_order()
        _second_import_id, second_order_id = self.seed_import_order()

        class FakeSkladBotClient:
            def __init__(self):
                self.payloads = {}
                self.next_id = 800

            @property
            def configured(self):
                return True

            def create_request(self, payload):
                self.next_id += 1
                self.payloads[self.next_id] = payload
                return {"data": {"id": self.next_id, "delivery_number": f"WH-R-{self.next_id}"}}

            def get_request_detail(self, request_id):
                payload = self.payloads[request_id]
                return {
                    "id": request_id,
                    "delivery_number": f"WH-R-{request_id}",
                    "fields": [
                        {"field": "address", "value": payload["fields"]["address"]["value"]},
                        {"field": "company_name", "value": payload["fields"]["company_name"]["value"]},
                        {"field": "unloading_date", "value": payload["fields"]["unloading_date"]["value"]},
                        {"field": "comment", "value": payload["comment"]},
                    ],
                    "products": [
                        {"name": item["comment"], "barcode": item["barcode"], "amount": item["amount"]}
                        for item in payload["products"]
                    ],
                }

            def list_requests(self):
                return []

        fake_client = FakeSkladBotClient()
        order_ids = [uuid.UUID(first_order_id), uuid.UUID(second_order_id)]
        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with self.SessionLocal() as db:
                create_skladbot_dry_run_for_orders(
                    db,
                    [first_order_id, second_order_id],
                    import_id=first_import_id,
                )
                events = db.execute(
                    select(PendingEvent)
                    .where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                    .order_by(PendingEvent.aggregate_id)
                ).scalars().all()
                markers = {(event.payload or {}).get("taksklad_marker") for event in events}
                public_comments = [
                    ((event.payload or {}).get("request_payload") or {}).get("comment", "")
                    for event in events
                ]
                fulfillment = get_or_create_fulfillment(
                    db,
                    source_scope="smartup:synthetic",
                    deal_id="synthetic-multi-order-deal",
                    target_status="B#W",
                    payload={"deal_id": "synthetic-multi-order-deal", "order_ids": sorted(map(str, order_ids))},
                )
                link_fulfillment_orders(db, fulfillment, order_ids)
                transition_fulfillment(db, fulfillment, "smartup_write_started")
                transition_fulfillment(db, fulfillment, "smartup_confirmed")
                for event in events:
                    link_fulfillment_order_event(
                        db,
                        fulfillment,
                        uuid.UUID(event.aggregate_id),
                        create_event=event,
                    )
                transition_fulfillment(db, fulfillment, "skladbot_create_queued")
                db.commit()

                result = process_pending_skladbot_request_creates(db, client=fake_client)
                db.commit()
                db.refresh(fulfillment)
                links = db.execute(
                    select(SmartupFulfillmentOrder)
                    .where(SmartupFulfillmentOrder.fulfillment_id == fulfillment.id)
                ).scalars().all()

        self.assertEqual(result["created"], 2)
        self.assertEqual(len(markers), 2)
        self.assertTrue(all(marker.startswith("TakSklad ref: TSF-") for marker in markers))
        self.assertTrue(all("TakSklad ref:" not in comment and "TSF-" not in comment for comment in public_comments))
        self.assertEqual(fulfillment.state, "skladbot_created")
        self.assertEqual({link.state for link in links}, {"created"})
        self.assertEqual(len({link.remote_request_id for link in links}), 2)

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
        self.assertEqual(process_result["failed"], 0)
        self.assertEqual(process_result["ambiguous"], 1)
        self.assertEqual(fake_client.create_calls, 1)
        self.assertEqual(create_event.status, "blocked")
        self.assertEqual(order.raw_payload["skladbot_status"], "ambiguous")
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

    def test_markerless_timeout_blocks_without_list_lookup_or_blind_repost(self):
        import_id, order_id = self.seed_import_order()

        class FakeSkladBotClient:
            def __init__(self):
                self.create_calls = 0

            @property
            def configured(self):
                return True

            def create_request(self, payload):
                self.create_calls += 1
                self.assert_markerless = (
                    payload["comment"] == payload["fields"]["comment"]["value"]
                    and "TakSklad ref:" not in payload["comment"]
                    and "TSF-" not in payload["comment"]
                )
                raise SkladBotApiError(
                    "SkladBot POST timeout",
                    kind=SkladBotErrorKind.TIMEOUT,
                    ambiguous=True,
                )

            def list_requests(self):
                raise AssertionError("markerless ambiguous create must not use list lookup")

            def get_request_detail(self, request_id):
                raise AssertionError("markerless ambiguous create has no exact response ID")

        fake_client = FakeSkladBotClient()
        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with self.SessionLocal() as db:
                create_skladbot_dry_run_for_import(db, import_id)
                first_result = process_pending_skladbot_request_creates(db, client=fake_client)
                event = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                ).scalar_one()
                event.status = "failed"
                event.available_at = event.created_at
                db.commit()
                second_result = process_pending_skladbot_request_creates(db, client=fake_client)
                db.commit()
                order = db.get(Order, uuid.UUID(order_id))
                event = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                ).scalar_one()
                event_status = event.status
                event_payload = dict(event.payload or {})
                incident = db.execute(select(Incident).where(Incident.source == "skladbot_create")).scalar_one()

        self.assertEqual(fake_client.create_calls, 1)
        self.assertTrue(fake_client.assert_markerless)
        self.assertEqual(first_result["ambiguous"], 1)
        self.assertEqual(second_result["ambiguous"], 1)
        self.assertEqual(order.raw_payload["skladbot_status"], "ambiguous")
        self.assertEqual(event_status, "blocked")
        self.assertEqual(event_payload["create_status"], "ambiguous")
        self.assertEqual(event_payload["post_request_marker"], "")
        self.assertEqual(incident.status, "manual_review")

    def test_stale_started_event_is_ambiguous_and_never_posts(self):
        import_id, order_id = self.seed_import_order()

        class FakeSkladBotClient:
            def __init__(self):
                self.create_calls = 0

            @property
            def configured(self):
                return True

            def create_request(self, payload):
                self.create_calls += 1
                raise AssertionError("stale started event must never POST")

            def list_requests(self):
                raise AssertionError("markerless stale event must not use list lookup")

            def get_request_detail(self, request_id):
                raise AssertionError("markerless stale event has no exact response ID")

        fake_client = FakeSkladBotClient()
        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with self.SessionLocal() as db:
                create_skladbot_dry_run_for_import(db, import_id)
                event = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                ).scalar_one()
                event.payload = {
                    **(event.payload or {}),
                    "post_state": "started",
                    "create_status": "queued",
                    "post_request_marker": "",
                }
                event.status = "processing"
                event.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
                db.commit()
                reset_count = reset_stale_skladbot_create_events(db)
                result = process_pending_skladbot_request_creates(db, client=fake_client)
                db.commit()
                order = db.get(Order, uuid.UUID(order_id))
                db.refresh(event)

        self.assertEqual(result["ambiguous"], 1)
        self.assertEqual(reset_count, 1)
        self.assertEqual(fake_client.create_calls, 0)
        self.assertEqual(event.status, "blocked")
        self.assertEqual(order.raw_payload["skladbot_status"], "ambiguous")

    def test_response_id_recovery_uses_only_exact_canonical_detail_without_repost(self):
        import_id, order_id = self.seed_import_order()

        class FakeSkladBotClient:
            def __init__(self):
                self.create_calls = 0
                self.detail_calls = 0

            @property
            def configured(self):
                return True

            def create_request(self, payload):
                self.create_calls += 1
                return {"data": {"id": 880}}

            def get_request_detail(self, request_id):
                self.detail_calls += 1
                self.assert_request_id = request_id
                if self.detail_calls == 1:
                    raise TimeoutError("canonical show timeout")
                return {"id": 880, "delivery_number": "WH-R-880", "fields": [], "products": []}

            def list_requests(self):
                raise AssertionError("response-ID recovery must not use list lookup")

        fake_client = FakeSkladBotClient()
        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with self.SessionLocal() as db:
                create_skladbot_dry_run_for_import(db, import_id)
                first_result = process_pending_skladbot_request_creates(db, client=fake_client)
                event = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                ).scalar_one()
                event.status = "failed"
                event.available_at = event.created_at
                db.commit()
                second_result = process_pending_skladbot_request_creates(db, client=fake_client)
                db.commit()
                order = db.get(Order, uuid.UUID(order_id))

        self.assertEqual(first_result["ambiguous"], 1)
        self.assertEqual(second_result["recovered"], 1)
        self.assertEqual(fake_client.create_calls, 1)
        self.assertEqual(fake_client.assert_request_id, 880)
        self.assertEqual(order.raw_payload["skladbot_request_number"], "WH-R-880")

    def test_malformed_create_response_blocks_without_lookup_or_repost(self):
        import_id, order_id = self.seed_import_order()

        class FakeSkladBotClient:
            def __init__(self):
                self.create_calls = 0

            @property
            def configured(self):
                return True

            def create_request(self, payload):
                self.create_calls += 1
                return {"data": {"id": "not-an-id"}}

            def get_request_detail(self, request_id):
                raise AssertionError("malformed ID must not call canonical show")

            def list_requests(self):
                raise AssertionError("malformed ID must not use list lookup")

        fake_client = FakeSkladBotClient()
        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with self.SessionLocal() as db:
                create_skladbot_dry_run_for_import(db, import_id)
                first_result = process_pending_skladbot_request_creates(db, client=fake_client)
                event = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                ).scalar_one()
                event.status = "failed"
                event.available_at = event.created_at
                db.commit()
                second_result = process_pending_skladbot_request_creates(db, client=fake_client)
                db.commit()
                order = db.get(Order, uuid.UUID(order_id))

        self.assertEqual(first_result["ambiguous"], 1)
        self.assertEqual(second_result["ambiguous"], 1)
        self.assertEqual(fake_client.create_calls, 1)
        self.assertEqual(order.raw_payload["skladbot_status"], "ambiguous")

    def test_legacy_started_event_recovers_only_by_exact_taksklad_marker(self):
        import_id, order_id = self.seed_import_order()

        class FakeSkladBotClient:
            def __init__(self, marker):
                self.marker = marker
                self.create_calls = 0

            @property
            def configured(self):
                return True

            def create_request(self, payload):
                self.create_calls += 1
                raise AssertionError("legacy started event must never POST")

            def list_requests(self):
                return [
                    {"id": 879, "delivery_number": "WH-R-879"},
                    {"id": 880, "delivery_number": "WH-R-880"},
                ]

            def get_request_detail(self, request_id):
                marker = "TakSklad ref: TSF-000000000000000000000000"
                if request_id == 880:
                    marker = self.marker
                return {
                    "id": request_id,
                    "delivery_number": f"WH-R-{request_id}",
                    "fields": [{"field": "comment", "value": f"Перечисление\nТП1\n{marker}"}],
                    "products": [],
                }

        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with self.SessionLocal() as db:
                create_skladbot_dry_run_for_import(db, import_id)
                event = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                ).scalar_one()
                marker = event.payload["taksklad_marker"]
                legacy_payload = dict(event.payload["request_payload"])
                legacy_fields = dict(legacy_payload["fields"])
                legacy_comment_field = dict(legacy_fields["comment"])
                legacy_comment = f"{legacy_payload['comment']}\n{marker}"
                legacy_payload["comment"] = legacy_comment
                legacy_comment_field["value"] = legacy_comment
                legacy_fields["comment"] = legacy_comment_field
                legacy_payload["fields"] = legacy_fields
                event.payload = {
                    **(event.payload or {}),
                    "request_payload": legacy_payload,
                    "post_state": "started",
                    "create_status": "queued",
                }
                event.status = "failed"
                event.available_at = event.created_at
                db.commit()
                fake_client = FakeSkladBotClient(marker)
                result = process_pending_skladbot_request_creates(db, client=fake_client)
                db.commit()
                order = db.get(Order, uuid.UUID(order_id))

        self.assertEqual(result["recovered"], 1)
        self.assertEqual(fake_client.create_calls, 0)
        self.assertEqual(order.raw_payload["skladbot_request_number"], "WH-R-880")
        self.assertEqual(order.raw_payload["skladbot_status"], "created_recovered")

    def test_legacy_attempt_without_post_state_never_posts_and_uses_exact_marker(self):
        import_id, order_id = self.seed_import_order()

        class FakeSkladBotClient:
            def __init__(self, marker):
                self.marker = marker
                self.create_calls = 0

            @property
            def configured(self):
                return True

            def create_request(self, payload):
                self.create_calls += 1
                raise AssertionError("attempted legacy event without post_state must not POST")

            def list_requests(self):
                return [{"id": 881, "delivery_number": "WH-R-881"}]

            def get_request_detail(self, request_id):
                return {
                    "id": request_id,
                    "delivery_number": "WH-R-881",
                    "fields": [{"field": "comment", "value": f"Перечисление\nТП1\n{self.marker}"}],
                    "products": [],
                }

        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with self.SessionLocal() as db:
                create_skladbot_dry_run_for_import(db, import_id)
                event = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                ).scalar_one()
                marker = event.payload["taksklad_marker"]
                legacy_payload = dict(event.payload["request_payload"])
                legacy_fields = dict(legacy_payload["fields"])
                legacy_comment_field = dict(legacy_fields["comment"])
                legacy_comment = f"{legacy_payload['comment']}\n{marker}"
                legacy_payload["comment"] = legacy_comment
                legacy_comment_field["value"] = legacy_comment
                legacy_fields["comment"] = legacy_comment_field
                legacy_payload["fields"] = legacy_fields
                event_payload = {
                    **(event.payload or {}),
                    "request_payload": legacy_payload,
                    "create_status": "queued",
                }
                event_payload.pop("post_state", None)
                event.payload = event_payload
                event.attempts = 1
                event.status = "failed"
                event.available_at = event.created_at
                db.commit()
                fake_client = FakeSkladBotClient(marker)
                result = process_pending_skladbot_request_creates(db, client=fake_client)
                db.commit()
                order = db.get(Order, uuid.UUID(order_id))
                db.refresh(event)

        self.assertEqual(result["recovered"], 1)
        self.assertEqual(fake_client.create_calls, 0)
        self.assertEqual(event.attempts, 2)
        self.assertEqual(order.raw_payload["skladbot_request_number"], "WH-R-881")

    def test_attempted_markerless_event_without_post_state_blocks_for_manual_review(self):
        import_id, order_id = self.seed_import_order()

        class FakeSkladBotClient:
            def __init__(self):
                self.create_calls = 0

            @property
            def configured(self):
                return True

            def create_request(self, payload):
                self.create_calls += 1
                raise AssertionError("attempted markerless event without post_state must not POST")

            def list_requests(self):
                raise AssertionError("markerless attempted event must not use list lookup")

        fake_client = FakeSkladBotClient()
        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with self.SessionLocal() as db:
                create_skladbot_dry_run_for_import(db, import_id)
                event = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                ).scalar_one()
                event_payload = {**(event.payload or {}), "create_status": "queued"}
                event_payload.pop("post_state", None)
                event.payload = event_payload
                event.attempts = 1
                event.status = "failed"
                event.available_at = event.created_at
                db.commit()
                result = process_pending_skladbot_request_creates(db, client=fake_client)
                db.commit()
                order = db.get(Order, uuid.UUID(order_id))
                incident = db.execute(
                    select(Incident).where(Incident.source == "skladbot_create")
                ).scalar_one()

        self.assertEqual(result["ambiguous"], 1)
        self.assertEqual(fake_client.create_calls, 0)
        self.assertEqual(order.raw_payload["skladbot_status"], "ambiguous")
        self.assertEqual(incident.status, "manual_review")

    def test_rate_limit_lease_finalize_preserves_backoff_and_prevents_immediate_claim(self):
        import_id, _order_id = self.seed_import_order()

        class FakeSkladBotClient:
            def __init__(self):
                self.create_calls = 0

            @property
            def configured(self):
                return True

            def create_request(self, payload):
                self.create_calls += 1
                raise SkladBotApiError(
                    "SkladBot API HTTP 429",
                    kind=SkladBotErrorKind.RATE_LIMIT,
                    status_code=429,
                    ambiguous=False,
                )

        fake_client = FakeSkladBotClient()
        with mock.patch.dict(
            "os.environ",
            {
                "SKLADBOT_CREATE_REQUESTS_MODE": "enabled",
                "TAKSKLAD_EVENT_LEASES_ENABLED": "1",
            },
            clear=False,
        ):
            with self.SessionLocal() as db:
                create_skladbot_dry_run_for_import(db, import_id)
                first_result = process_pending_skladbot_request_creates(db, client=fake_client)
                event = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                ).scalar_one()
                stored_available_at = event.available_at
                retry_at = datetime.fromisoformat(event.payload["retry_at"])
                second_result = process_pending_skladbot_request_creates(db, client=fake_client)
                db.commit()
                db.refresh(event)
                event_status = event.status

        if stored_available_at.tzinfo is None:
            stored_available_at = stored_available_at.replace(tzinfo=timezone.utc)
        self.assertEqual(first_result["checked"], 1)
        self.assertEqual(second_result["checked"], 0)
        self.assertEqual(fake_client.create_calls, 1)
        self.assertEqual(event_status, "pending")
        self.assertAlmostEqual(stored_available_at.timestamp(), retry_at.timestamp(), delta=1)
        self.assertGreater((stored_available_at - datetime.now(timezone.utc)).total_seconds(), 240)

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
        self.assertEqual(process_result["blocked"], 1)
        self.assertEqual(order.raw_payload["skladbot_status"], "create_failed")
        self.assertNotIn("skladbot_request_number", {k: v for k, v in order.raw_payload.items() if v})

    def test_stock_shortage_create_failure_blocks_unscanned_order_without_deleting_data(self):
        import_id, order_id = self.seed_import_order(telegram_chat_id="123")

        class FakeSkladBotClient:
            @property
            def configured(self):
                return True

            def create_request(self, payload):
                raise SkladBotApiError(
                    "SkladBot API HTTP 422: Недостаточно товара на складе для создания заявки",
                    kind=SkladBotErrorKind.STOCK_SHORTAGE,
                    status_code=422,
                    ambiguous=False,
                )

            def list_requests(self):
                return []

        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with mock.patch("backend.app.skladbot_request_dry_run.SkladBotClient", return_value=FakeSkladBotClient()):
                with self.SessionLocal() as db:
                    summary = create_skladbot_dry_run_for_import(db, import_id)
                    process_result = process_pending_skladbot_request_creates(db, client=FakeSkladBotClient())
                    db.commit()
                    order = db.get(Order, uuid.UUID(order_id))
                    items = db.execute(select(OrderItem)).scalars().all()
                    create_event = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                    ).scalar_one()
                    telegram_event = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == "telegram_notification")
                    ).scalar_one()
                    incident = db.execute(select(Incident).where(Incident.source == "skladbot_create")).scalar_one()

        self.assertEqual(summary["queued"], 1)
        self.assertEqual(process_result["stock_shortage_blocked"], 1)
        self.assertEqual(process_result["stock_shortage_cancelled"], 0)
        self.assertEqual(process_result["failed"], 0)
        self.assertIsNotNone(order)
        self.assertEqual(len(items), 2)
        self.assertEqual(order.raw_payload["skladbot_status"], "blocked_stock")
        self.assertEqual(create_event.status, "blocked")
        self.assertEqual(create_event.payload["create_status"], "blocked_stock")
        self.assertNotIn("chat_id", telegram_event.payload)
        self.assertEqual(telegram_event.payload["kind"], "skladbot_stock_shortage_blocked_order")
        self.assertIn("Заказ заблокирован из-за недостатка товара", telegram_event.payload["text"])
        self.assertIn("не удалён", telegram_event.payload["text"])
        self.assertEqual(incident.status, "manual_review")
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
                raise SkladBotApiError(
                    "SkladBot API HTTP 422: Недостаточно товара на складе для создания заявки",
                    kind=SkladBotErrorKind.STOCK_SHORTAGE,
                    status_code=422,
                    ambiguous=False,
                )

            def list_requests(self):
                return []

        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with self.SessionLocal() as db:
                create_skladbot_dry_run_for_import(db, import_id)
                process_result = process_pending_skladbot_request_creates(db, client=FakeSkladBotClient())
                db.commit()
                order = db.get(Order, uuid.UUID(order_id))
                telegram_events = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == "telegram_notification")
                ).scalars().all()
                incident = db.execute(select(Incident).where(Incident.source == "skladbot_create")).scalar_one()
                create_event = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                ).scalar_one()

        self.assertEqual(process_result["failed"], 0)
        self.assertEqual(process_result["stock_shortage_blocked"], 1)
        self.assertEqual(process_result["stock_shortage_cancelled"], 0)
        self.assertIsNotNone(order)
        self.assertEqual(order.raw_payload["skladbot_status"], "blocked_stock")
        self.assertEqual(len(telegram_events), 1)
        self.assertEqual(create_event.status, "blocked")
        self.assertEqual(incident.status, "manual_review")
        self.assertEqual(incident.pending_event_id, create_event.id)
        self.assertEqual(str(incident.order_id), order_id)
        self.assertIn("Недостаточно товара", incident.message)

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
            with mock.patch("backend.app.skladbot_client.httpx.Client", FakeHttpClient):
                payload = {"customer_id": 6211, "request_type_id": 3389, "products": []}
                result = SkladBotClient().create_request(payload)

        self.assertEqual(result["data"]["delivery_number"], "WH-R-1")
        self.assertEqual(captured["url"], "https://api.skladbot.ru/v1/requests")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer secret-token")
        self.assertEqual(captured["json"], payload)

    def test_skladbot_client_post_classifies_timeout_as_ambiguous_without_retry(self):
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
            with mock.patch("backend.app.skladbot_client.httpx.Client", FakeHttpClient):
                with self.assertRaises(SkladBotApiError) as raised:
                    SkladBotClient().create_request({"customer_id": 6211})

        self.assertEqual(calls["count"], 1)
        self.assertEqual(raised.exception.kind, SkladBotErrorKind.TIMEOUT)
        self.assertTrue(raised.exception.ambiguous)

    def test_skladbot_client_post_classifies_5xx_as_ambiguous_not_stock_shortage(self):
        class FakeHttpClient:
            def __init__(self, timeout):
                self.timeout = timeout

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url, json=None, headers=None):
                return httpx.Response(
                    503,
                    json={"detail": "Недостаточно товара на складе"},
                    request=httpx.Request("POST", url),
                )

        with mock.patch.dict("os.environ", {"SKLADBOT_API_TOKEN": "token", "SKLADBOT_API_TOKENS": ""}, clear=False):
            with mock.patch("backend.app.skladbot_client.httpx.Client", FakeHttpClient):
                with self.assertRaises(SkladBotApiError) as raised:
                    SkladBotClient().create_request({"customer_id": 6211})

        self.assertEqual(raised.exception.kind, SkladBotErrorKind.SERVER)
        self.assertTrue(raised.exception.ambiguous)

    def test_skladbot_client_post_classifies_deterministic_4xx_stock_shortage(self):
        class FakeHttpClient:
            def __init__(self, timeout):
                self.timeout = timeout

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url, json=None, headers=None):
                return httpx.Response(
                    422,
                    json={"detail": "Недостаточно товара на складе"},
                    request=httpx.Request("POST", url),
                )

        with mock.patch.dict("os.environ", {"SKLADBOT_API_TOKEN": "token", "SKLADBOT_API_TOKENS": ""}, clear=False):
            with mock.patch("backend.app.skladbot_client.httpx.Client", FakeHttpClient):
                with self.assertRaises(SkladBotApiError) as raised:
                    SkladBotClient().create_request({"customer_id": 6211})

        self.assertEqual(raised.exception.kind, SkladBotErrorKind.STOCK_SHORTAGE)
        self.assertFalse(raised.exception.ambiguous)

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
            failed_audit = db.execute(
                select(AuditLog).where(AuditLog.action == "skladbot_request_dry_run_failed")
            ).scalars().all()
            import_job = db.get(ImportJob, uuid.UUID(payload["id"]))

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
        with mock.patch.dict("os.environ", {"TAKSKLAD_EVENT_LEASES_ENABLED": "1"}, clear=False):
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

        self.assertEqual(process_result["created"], 1)
        self.assertEqual(len(fake_client.created_payloads), 1)
        payload = fake_client.created_payloads[0]
        self.assertEqual(payload["request_type_id"], 3403)
        self.assertEqual(payload["customer_id"], 6211)
        self.assertIs(payload["notify"], True)
        self.assertEqual(payload["comment"], "Перечисление\nТП1")
        self.assertEqual(payload["fields"]["comment"]["value"], "Перечисление\nТП1")
        self.assertEqual(payload["fields"]["company_name"]["value"], '"TEST CLIENT" MCHJ')
        self.assertEqual(payload["fields"]["unloading_date"]["value"], "2026-06-05")
        self.assertEqual([product["amount"] for product in payload["products"]], [2, 3])
        self.assertEqual(order.raw_payload["skladbot_request_number"], "WH-R-OUT")
        self.assertEqual(order.raw_payload["skladbot_request_id"], "180000")
        self.assertEqual(order.raw_payload["skladbot_return_request_number"], "WH-R-190707")
        self.assertEqual(order.raw_payload["skladbot_return_request_id"], "190707")
        self.assertEqual(order.raw_payload["skladbot_return_request_status"], "created")
        self.assertEqual(event.status, "completed")
        self.assertIsNone(event.lease_owner)
        self.assertIsNotNone(event.completed_at)
        self.assertEqual(event.payload["create_status"], "created")

    def test_return_create_worker_recovers_response_id_by_exact_detail_without_duplicate_post(self):
        _import_id, order_id = self.seed_import_order()

        class FakeSkladBotClient:
            @property
            def configured(self):
                return True

            def create_request(self, payload):
                raise AssertionError("response-id recovery must not POST /requests")

            def list_requests(self, type_id=None):
                raise AssertionError("response-id recovery must not list requests")

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
            event.payload = {
                **(event.payload or {}),
                "post_state": "response_received",
                "post_response_request_id": 190708,
            }
            process_result = process_pending_skladbot_return_request_creates(db, client=fake_client)
            db.commit()
            order = db.get(Order, uuid.UUID(order_id))
            event = db.execute(
                select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_RETURN_REQUEST_CREATE_EVENT_TYPE)
            ).scalar_one()

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
