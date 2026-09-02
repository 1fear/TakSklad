"""Сквозной прогон двух новых SKU Chapman KSSL по всей цепочке склада.

Поставка 03.09.2026, заказы с KSSL ожидаются с 04.09. Тест повторяет путь
заказа руками кода: имя из Smartup, импорт Excel через API, dry-run и POST
заявки SkladBot с картами 4134853 и 4135839, нехватка остатка до приёмки,
сканы КИЗ на позиции KSSL с защитой от чужого SKU, возврат, дейли-отчёт.
"""

import unittest
import uuid
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import get_db
from backend.app.main import app, require_admin_write_permission, require_service_token
from backend.app.models import Base, ImportJob, Order, OrderItem, PendingEvent, ScanCode
from backend.app.settings import load_settings
from backend.app.skladbot_client import SkladBotApiError, SkladBotErrorKind
from backend.app.skladbot_daily_report import assign_kiz_rows_to_products
from backend.app.skladbot_request_dry_run import (
    SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
    create_skladbot_dry_run_for_import,
    list_skladbot_dry_runs,
    process_pending_skladbot_request_creates,
)
from backend.app.skladbot_return_requests import build_skladbot_return_payload
from backend.app.smartup_auto_import import SmartupAutoImportConfig, build_import_rows
from backend.app.telegram_admin_processor import TELEGRAM_MANUAL_PRODUCTS as ADMIN_MANUAL_PRODUCTS
from backend.app.telegram_manual_support import TELEGRAM_MANUAL_PRODUCTS, telegram_manual_product_keyboard

BROWN_KSSL = "Chapman Brown KSSL 20"
GREEN_KSSL = "Chapman Green KSSL 20"
BROWN_KSSL_UNIT_PREFIX = "0104006396104199"
GREEN_KSSL_UNIT_PREFIX = "0104006396104229"
BROWN_SSL_UNIT_CODE = "0104006396054067217KDAUbG93OVvXgs6C"
BOX_TAIL = "21UZ1112022525522513824013040046110ZIG1218229310000"


def unit_code(prefix, serial):
    code = f"{prefix}21{serial}".ljust(35, "X")
    assert len(code) == 35, code
    return code


class KsslSmartupNameTests(unittest.TestCase):
    def test_smartup_line_with_supplier_suffix_becomes_kssl_row(self):
        order = {
            "deal_id": "9001",
            "deal_time": "03.09.2026 09:10:00",
            "delivery_date": "04.09.2026",
            "status": "B#N",
            "payment_type_code": "PYMT:2",
            "person_name": "TEST TRADE MCHJ",
            "delivery_address_full": "Ташкент, тестовая 1",
            "person_latitude": "41.311081",
            "person_longitude": "69.240562",
            "sales_manager_name": "ТП",
            "order_products": [
                {
                    "external_id": "line-1",
                    "product_code": "brown-kssl",
                    "product_name": f"{BROWN_KSSL} / VON EICKEN / Германия",
                    "order_quant": "30",
                    "product_price": "240000",
                    "sold_amount": "720000",
                },
                {
                    "external_id": "line-2",
                    "product_code": "green-kssl",
                    "product_name": GREEN_KSSL,
                    "order_quant": "20",
                    "product_price": "240000",
                    "sold_amount": "480000",
                },
            ],
        }
        config = SmartupAutoImportConfig(
            enabled=True,
            smartup_username="user",
            smartup_password="password",
            route_fingerprint_key="synthetic-unit-route-key",
            output_dir=Path("/tmp"),
        )

        rows = build_import_rows([order], date(2026, 9, 3), "Перечисление 03.09.2026 Часть 1.xlsx", config)

        self.assertEqual([row["Товары"] for row in rows], [BROWN_KSSL, GREEN_KSSL])
        self.assertEqual([row["Кол-во блок"] for row in rows], [3, 2])
        self.assertEqual([row["Кол-во ШТ"] for row in rows], [30, 20])


class KsslWarehouseFlowTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "dry_run"}, clear=False)
        self.env_patch.start()
        self.settings_patch = mock.patch(
            "backend.app.main.settings",
            load_settings({"TAKSKLAD_ENV": "local", "TAKSKLAD_INSECURE_LOCAL_ANONYMOUS": "true"}),
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
        app.dependency_overrides[require_admin_write_permission] = lambda: None
        self.client = TestClient(app)

    def tearDown(self):
        self.settings_patch.stop()
        self.env_patch.stop()
        app.dependency_overrides.clear()
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def import_kssl_order(self, order_key="kssl-order-1"):
        rows = [
            {
                "Дата отгрузки": "04.09.2026",
                "Тип оплаты": "Перечисление",
                "Клиент": "TEST TRADE MCHJ",
                "Адрес": "Ташкент, тестовая 1",
                "Торговый представитель": "ТП1",
                "Товары": BROWN_KSSL,
                "Кол-во ШТ": "30",
                "Кол-во блок": "3",
                "ID заказа": order_key,
                "ID импорта": f"{order_key}:1",
            },
            {
                "Дата отгрузки": "04.09.2026",
                "Тип оплаты": "Перечисление",
                "Клиент": "TEST TRADE MCHJ",
                "Адрес": "Ташкент, тестовая 1",
                "Торговый представитель": "ТП1",
                "Товары": GREEN_KSSL,
                "Кол-во ШТ": "20",
                "Кол-во блок": "2",
                "ID заказа": order_key,
                "ID импорта": f"{order_key}:2",
            },
        ]
        response = self.client.post(
            "/api/v1/imports",
            json={"source": "excel", "filename": f"{order_key}.xlsx", "rows": rows},
        )
        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        self.assertEqual((payload["orders_created"], payload["items_created"]), (1, 2))
        with self.SessionLocal() as db:
            import_job = db.execute(select(ImportJob).order_by(ImportJob.created_at.desc())).scalars().first()
            order = db.execute(select(Order).order_by(Order.created_at.desc())).scalars().first()
            items = {item.product: str(item.id) for item in order.items}
            return str(import_job.id), str(order.id), items

    def test_excel_import_dry_run_targets_kssl_skladbot_cards(self):
        import_id, order_id, _items = self.import_kssl_order()

        with self.SessionLocal() as db:
            summary = create_skladbot_dry_run_for_import(db, import_id)
            db.commit()
            rows = list_skladbot_dry_runs(db, import_id)

        self.assertEqual(summary["ready"], 1, summary)
        row = rows[0]
        self.assertEqual(row["order_id"], order_id)
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["payload"]["comment"].split("\n")[0], "Перечисление")
        self.assertEqual(
            [(p["product_data_id"], p["barcode"], p["amount"]) for p in row["payload"]["products"]],
            [(4134853, "4006396104199", 3), (4135839, "4006396104229", 2)],
        )

    def test_enabled_mode_posts_kssl_request_and_links_order(self):
        import_id, order_id, _items = self.import_kssl_order()

        class FakeSkladBotClient:
            def __init__(self):
                self.created_payloads = []

            @property
            def configured(self):
                return True

            def create_request(self, payload):
                self.created_payloads.append(payload)
                return {"data": {"id": 777, "delivery_number": "WH-R-777", "created_at": "2026-09-03T12:00:00.000000Z"}}

            def get_request_detail(self, request_id):
                return {
                    "id": request_id,
                    "delivery_number": "WH-R-777",
                    "fields": [
                        {"field": "address", "value": "Ташкент, тестовая 1"},
                        {"field": "company_name", "value": "TEST TRADE MCHJ"},
                        {"field": "unloading_date", "value": "2026-09-04"},
                        {"field": "comment", "value": "Перечисление"},
                    ],
                    "products": [
                        {"name": "Chapman Brown KSSL 20 UZ - KingSize SuperSlim", "barcode": "4006396104199", "amount": 3},
                        {"name": "Chapman Green KSSL 20 UZ - KingSize SuperSlim", "barcode": "4006396104229", "amount": 2},
                    ],
                }

            def list_requests(self, *args, **kwargs):
                return []

        fake = FakeSkladBotClient()
        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with mock.patch("backend.app.skladbot_request_dry_run.SkladBotClient", return_value=fake):
                with self.SessionLocal() as db:
                    create_skladbot_dry_run_for_import(db, import_id)
                    result = process_pending_skladbot_request_creates(db, client=fake)
                    db.commit()
                    order = db.get(Order, uuid.UUID(order_id))
                    event = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                    ).scalar_one()
                    raw_payload = dict(order.raw_payload or {})
                    event_status = event.status
                    create_status = (event.payload or {}).get("create_status")

        self.assertEqual(len(fake.created_payloads), 1, result)
        self.assertEqual(
            [(p["product_data_id"], p["amount"]) for p in fake.created_payloads[0]["products"]],
            [(4134853, 3), (4135839, 2)],
        )
        self.assertEqual(event_status, "completed", (event_status, create_status, result))
        self.assertEqual(raw_payload.get("skladbot_request_number"), "WH-R-777", raw_payload)

    def test_kssl_request_before_receiving_blocks_order_loudly(self):
        import_id, order_id, _items = self.import_kssl_order()

        class ShortageClient:
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

            def list_requests(self, *args, **kwargs):
                return []

        with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
            with mock.patch("backend.app.skladbot_request_dry_run.SkladBotClient", return_value=ShortageClient()):
                with self.SessionLocal() as db:
                    create_skladbot_dry_run_for_import(db, import_id)
                    result = process_pending_skladbot_request_creates(db, client=ShortageClient())
                    db.commit()
                    order = db.get(Order, uuid.UUID(order_id))
                    skladbot_status = (order.raw_payload or {}).get("skladbot_status")
                    items_count = len(order.items)
                    telegram_event = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == "telegram_notification")
                    ).scalar_one()
                    text = telegram_event.payload["text"]

        self.assertEqual(result["stock_shortage_blocked"], 1, result)
        self.assertEqual(skladbot_status, "blocked_stock")
        self.assertEqual(items_count, 2)
        self.assertIn("Заказ заблокирован из-за недостатка товара", text)
        self.assertIn(f"{BROWN_KSSL}: 3 блок.", text)
        self.assertIn(f"{GREEN_KSSL}: 2 блок.", text)

    def scan(self, item_id, code):
        return self.client.post(
            "/api/v1/scans",
            json={"order_item_id": item_id, "code": code, "workstation_id": "pc-1", "scanned_by": "operator"},
        )

    def test_kssl_scans_complete_order_and_reject_foreign_codes(self):
        _import_id, order_id, items = self.import_kssl_order()
        brown_item = items[BROWN_KSSL]
        green_item = items[GREEN_KSSL]

        # Чужой SKU на позиции Brown KSSL: старый Brown SSL и новый Green KSSL
        for foreign in (BROWN_SSL_UNIT_CODE, unit_code(GREEN_KSSL_UNIT_PREFIX, "7GREEN0001")):
            response = self.scan(brown_item, foreign)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["detail"]["code"], "scan_product_mismatch")

        # Код коробочной длины со штучным GTIN KSSL это обрезок или склейка
        truncated_box = f"{BROWN_KSSL_UNIT_PREFIX}{BOX_TAIL}"
        self.assertEqual(len(truncated_box), 67)
        response = self.scan(brown_item, truncated_box)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("length_for_gtin", response.text)

        # Короб KSSL с ещё не заведённым коробочным GTIN не проходит молча:
        # неизвестный GTIN на позиции с известным ключом это несовпадение товара
        unknown_box = f"0104006396104205{BOX_TAIL}"
        self.assertEqual(len(unknown_box), 67)
        response = self.scan(brown_item, unknown_box)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "scan_product_mismatch")

        # Три блока Brown KSSL закрывают позицию, четвёртый не принимается
        for index in range(1, 4):
            response = self.scan(brown_item, unit_code(BROWN_KSSL_UNIT_PREFIX, f"7BROWN{index:04d}"))
            self.assertEqual(response.status_code, 201, response.text)
            payload = response.json()
            self.assertEqual(payload["scanned_blocks"], index)
            self.assertEqual(payload["item_status"], "completed" if index == 3 else "not_completed")
        response = self.scan(brown_item, unit_code(BROWN_KSSL_UNIT_PREFIX, "7BROWN0004"))
        self.assertEqual(response.status_code, 409, response.text)

        # Тот же блок Brown KSSL второй раз, уже на позицию Green, не проходит
        response = self.scan(green_item, unit_code(BROWN_KSSL_UNIT_PREFIX, "7BROWN0001"))
        self.assertEqual(response.status_code, 409, response.text)

        for index in range(1, 3):
            response = self.scan(green_item, unit_code(GREEN_KSSL_UNIT_PREFIX, f"7GREEN{index:04d}"))
            self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["item_status"], "completed")

        completed = self.client.post(f"/api/v1/orders/{order_id}/complete")
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertEqual(completed.json()["status"], "completed")

        with self.SessionLocal() as db:
            scans = db.execute(select(ScanCode)).scalars().all()
            self.assertEqual(len(scans), 5)
            self.assertEqual({scan.raw_payload["scan_type"] for scan in scans}, {"unit"})
            self.assertEqual(
                sorted({scan.raw_payload["product_key"] for scan in scans}),
                ["brown:kssl", "green:kssl"],
            )
            for item in db.execute(select(OrderItem)).scalars().all():
                self.assertEqual(item.scanned_blocks, item.quantity_blocks, item.product)

    def test_daily_report_assigns_kssl_codes_to_kssl_rows_only(self):
        products = [
            {"name": "Chapman Brown KSSL 20 UZ - KingSize SuperSlim", "barcode": "4006396104199"},
            {"name": "Chapman Brown SSL 20 UZ - SuperSlim", "barcode": "4006396054067"},
            {"name": "Chapman Green KSSL 20 UZ - KingSize SuperSlim", "barcode": "4006396104229"},
        ]
        kiz_rows = [
            {"code": unit_code(BROWN_KSSL_UNIT_PREFIX, "7BROWN0001")},
            {"code": BROWN_SSL_UNIT_CODE},
            {"code": unit_code(GREEN_KSSL_UNIT_PREFIX, "7GREEN0001")},
        ]

        matched, unmatched = assign_kiz_rows_to_products(products, kiz_rows)

        self.assertEqual(unmatched, [])
        self.assertEqual({position: [row["code"][:16] for row in rows] for position, rows in matched.items()}, {
            0: [BROWN_KSSL_UNIT_PREFIX],
            1: ["0104006396054067"],
            2: [GREEN_KSSL_UNIT_PREFIX],
        })

    def test_return_payload_uses_kssl_cards(self):
        _import_id, order_id, _items = self.import_kssl_order()
        with self.SessionLocal() as db:
            order = db.get(Order, uuid.UUID(order_id))
            payload, errors = build_skladbot_return_payload(
                order,
                [{"product": BROWN_KSSL, "quantity_blocks": 1}, {"product": GREEN_KSSL, "quantity_blocks": 2}],
            )

        self.assertEqual(errors, [])
        self.assertEqual(
            [(p["product_data_id"], p["barcode"], p["amount"]) for p in payload["products"]],
            [(4134853, "4006396104199", 1), (4135839, "4006396104229", 2)],
        )


class KsslTelegramManualOrderTests(unittest.TestCase):
    def test_manual_order_keyboard_offers_both_kssl_and_both_dicts_agree(self):
        self.assertEqual(TELEGRAM_MANUAL_PRODUCTS, ADMIN_MANUAL_PRODUCTS)
        self.assertEqual(TELEGRAM_MANUAL_PRODUCTS["brown_kssl"], BROWN_KSSL)
        self.assertEqual(TELEGRAM_MANUAL_PRODUCTS["green_kssl"], GREEN_KSSL)
        keyboard = telegram_manual_product_keyboard()
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        labels = {button["text"]: button["callback_data"] for button in buttons}
        self.assertEqual(labels[BROWN_KSSL], "manual:product:brown_kssl")
        self.assertEqual(labels[GREEN_KSSL], "manual:product:green_kssl")
        self.assertEqual(list(labels)[-1], "Отмена")


if __name__ == "__main__":
    unittest.main()
