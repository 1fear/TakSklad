import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest import mock
from zipfile import ZipFile
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import openpyxl
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import AuditLog, Base, ImportJob, LogisticsCalendarDay, Order, PendingEvent, SmartupFulfillment
from backend.app.smartup_auto_import import (
    SmartupClient,
    SmartupAutoImportConfig,
    SmartupAutoImportError,
    SMARTUP_AUTO_IMPORT_EVENT_TYPE,
    SMARTUP_LOGISTICS_REPORT_EVENT_TYPE,
    build_smartup_auto_import_status,
    build_import_rows,
    delivery_dates_for_export_date,
    filter_smartup_orders,
    parse_disabled_weekdays,
    preview_delivery_groups,
    prepare_orphaned_smartup_sagas,
    run_due_smartup_auto_imports,
    run_smartup_auto_import_once,
    run_scheduled_smartup_auto_import_slot,
    acquire_smartup_slot_advisory_lock,
    release_smartup_slot_advisory_lock,
    scheduled_smartup_target_delivery_date,
    scheduled_smartup_target_delivery_dates,
    send_final_logistics_reports,
    smartup_advisory_lock_key,
    smartup_slot_idempotency_key,
    sweep_incomplete_smartup_fulfillments,
)
from backend.app import smartup_auto_import_worker
from backend.app.kiz_reports_service import list_completed_kiz_source_files
from backend.app.smartup_auto_import_history_service import list_smartup_auto_import_history
from backend.app.skladbot_request_dry_run import SKLADBOT_REQUEST_CREATE_EVENT_TYPE, list_skladbot_dry_runs
from backend.app.smartup_saga import SMARTUP_DEAL_SAGA_EVENT_TYPE


def sample_order(**overrides):
    order = {
        "deal_id": "642",
        "deal_time": "25.06.2026 09:10:00",
        "delivery_date": "26.06.2026",
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
                "product_code": "red-op",
                "product_name": "Chapman RED OP 20 / VON EICKEN / Германия",
                "order_quant": "200",
                "product_price": "240000",
                "sold_amount": "4800000",
            }
        ],
    }
    order.update(overrides)
    return order


class FakeSmartupClient:
    smartup_saga_fake = True

    def __init__(self, orders):
        self.orders = orders
        self.changed = []
        self.exports = []
        self.status_reads = []
        self.statuses = {
            str(order.get("deal_id")): str(order.get("status") or "B#N")
            for order in orders
        }

    def export_orders(self, export_date, *, target_delivery_date=None):
        self.exports.append((export_date, target_delivery_date))
        return {"order": self.orders}

    def change_status(self, deal_ids, status_code):
        self.changed.append((list(deal_ids), status_code))
        for deal_id in deal_ids:
            self.statuses[str(deal_id)] = status_code
        return {
            "successes": [{"code": deal_id} for deal_id in deal_ids],
            "errors": [],
            "submitted": len(deal_ids),
            "status": status_code,
        }

    def get_deal_statuses(self, deal_ids):
        self.status_reads.append(list(deal_ids))
        return {str(deal_id): self.statuses.get(str(deal_id), "") for deal_id in deal_ids}


class FailingSmartupClient:
    def export_orders(self, export_date, *, target_delivery_date=None):
        raise SmartupAutoImportError("Smartup test failure")


class FailingStatusChangeSmartupClient(FakeSmartupClient):
    def change_status(self, deal_ids, status_code):
        self.changed.append((list(deal_ids), status_code))
        raise SmartupAutoImportError("Smartup status change unavailable")


class PartialStatusChangeSmartupClient(FakeSmartupClient):
    def change_status(self, deal_ids, status_code):
        self.changed.append((list(deal_ids), status_code))
        self.statuses[str(deal_ids[0])] = status_code
        return {
            "successes": [{"deal_id": deal_ids[0]}],
            "errors": [{"deal_id": deal_ids[1], "message": "locked"}],
            "submitted": len(deal_ids),
            "status": status_code,
        }


class FakeTelegramSender:
    configured = True
    smartup_saga_fake = True

    def __init__(self):
        self.messages = []
        self.documents = []

    def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))
        return {"ok": True}

    def send_document(self, chat_id, content, filename, caption=""):
        self.documents.append((chat_id, content, filename, caption))
        return {"ok": True}


class FakeScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class FakePostgresConnection:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, statement, params):
        self.executed.append((str(statement), params))
        return FakeScalarResult(self.acquired)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class FakePostgresBind:
    class Dialect:
        name = "postgresql"

    dialect = Dialect()

    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return self.connection


class FakeDbSession:
    def __init__(self, bind):
        self.bind = bind

    def get_bind(self):
        return self.bind


class SmartupAutoImportTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def config(self, output_dir, **overrides):
        values = {
            "enabled": True,
            "smartup_username": "user",
            "smartup_password": "password",
            "output_dir": Path(output_dir),
        }
        values.update(overrides)
        return SmartupAutoImportConfig(**values)

    def assert_xlsx_has_no_orphaned_pane_selections(self, content):
        namespace = {"xlsx": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        with ZipFile(BytesIO(content)) as archive:
            worksheet_names = [
                name for name in archive.namelist()
                if name.startswith("xl/worksheets/") and name.endswith(".xml")
            ]
            self.assertTrue(worksheet_names)
            for worksheet_name in worksheet_names:
                root = ET.fromstring(archive.read(worksheet_name))
                for sheet_view in root.findall(".//xlsx:sheetView", namespace):
                    pane = sheet_view.find("xlsx:pane", namespace)
                    if pane is not None:
                        continue
                    for selection in sheet_view.findall("xlsx:selection", namespace):
                        selection_pane = selection.attrib.get("pane")
                        self.assertIn(
                            selection_pane,
                            (None, "topLeft"),
                            f"{worksheet_name} contains orphaned selection pane={selection_pane}",
                        )

    def test_preview_delivery_groups_uses_same_source_batch_key_as_import(self):
        captured_payloads = []

        def fake_preview(_db, payload):
            captured_payloads.append(payload)
            return mock.Mock(
                status="ok",
                rows_total=1,
                rows_importable=1,
                orders_new=1,
                items_new=1,
                duplicate_rows=0,
                invalid_rows=0,
                errors=[],
            )

        grouped_rows = {
            "2026-07-02": [
                {
                    "Дата отгрузки": "02.07.2026",
                    "Тип оплаты": "Терминал",
                    "Клиент": "Preview Client",
                    "Адрес": "Preview Address",
                    "Товары": "Chapman RED OP 20",
                    "Кол-во ШТ": 10,
                    "Кол-во блок": 1,
                    "ID заказа": "smartup:preview-deal",
                    "ID импорта": "smartup:preview-deal:red:1",
                }
            ]
        }

        with mock.patch("backend.app.smartup_auto_import.preview_import", side_effect=fake_preview):
            previews = preview_delivery_groups(
                mock.Mock(),
                grouped_rows,
                "Терминал 01.07.2026 Часть 2.xlsx",
                "sha",
                source_batch_key="smartup:2026-07-01:part:2:sha256:sha",
            )

        self.assertEqual(previews[0]["status"], "ok")
        self.assertEqual(
            captured_payloads[0].rows[0]["source_batch_key"],
            "smartup:2026-07-01:part:2:sha256:sha",
        )

    def test_mapper_uses_delivery_date_for_taksklad_order_date(self):
        rows = build_import_rows(
            [sample_order()],
            datetime(2026, 6, 25).date(),
            "Терминал 25.06.2026 Часть 1.xlsx",
            self.config("/tmp"),
        )

        self.assertEqual(rows[0]["Дата заказа"], "25.06.2026")
        self.assertEqual(rows[0]["Дата отгрузки"], "26.06.2026")
        self.assertEqual(rows[0]["Товары"], "Chapman RED OP 20")
        self.assertEqual(rows[0]["Кол-во ШТ"], 200)
        self.assertEqual(rows[0]["Кол-во блок"], 20)
        self.assertEqual(rows[0]["ID заказа"], "smartup:642")
        self.assertEqual(rows[0]["ID импорта"], "smartup:642:line-1:1")

    def test_mapper_moves_weekend_delivery_date_to_next_working_day(self):
        rows = build_import_rows(
            [sample_order(delivery_date="27.06.2026")],
            datetime(2026, 6, 26).date(),
            "Терминал 26.06.2026 Часть 1.xlsx",
            self.config("/tmp"),
        )

        self.assertEqual(rows[0]["Дата отгрузки"], "29.06.2026")
        self.assertEqual(rows[0]["Smartup delivery_date original"], "27.06.2026")
        self.assertEqual(rows[0]["Smartup delivery_date adjusted"], "yes")
        self.assertEqual(rows[0]["Smartup delivery_date adjustment_reason"], "non_working_logistics_day")
        self.assertEqual(rows[0]["Smartup delivery_date skipped_dates"], "2026-06-27,2026-06-28")

    def test_mapper_uses_manual_holiday_calendar_for_next_working_day(self):
        with self.SessionLocal() as db:
            db.add(LogisticsCalendarDay(
                service_date=date(2026, 6, 29),
                is_non_working=True,
                reason="Праздник",
                source="web",
                raw_payload={},
            ))
            db.commit()

            rows = build_import_rows(
                [sample_order(delivery_date="27.06.2026")],
                datetime(2026, 6, 26).date(),
                "Терминал 26.06.2026 Часть 1.xlsx",
                self.config("/tmp"),
                db=db,
            )

        self.assertEqual(rows[0]["Дата отгрузки"], "30.06.2026")
        self.assertEqual(rows[0]["Smartup delivery_date original"], "27.06.2026")
        self.assertEqual(rows[0]["Smartup delivery_date adjusted"], "yes")
        self.assertEqual(
            rows[0]["Smartup delivery_date skipped_dates"],
            "2026-06-27,2026-06-28,2026-06-29",
        )

    def test_mapper_allows_manual_working_override_for_weekend(self):
        with self.SessionLocal() as db:
            db.add(LogisticsCalendarDay(
                service_date=date(2026, 6, 27),
                is_non_working=False,
                reason="Рабочая суббота",
                source="web",
                raw_payload={},
            ))
            db.commit()

            rows = build_import_rows(
                [sample_order(delivery_date="27.06.2026")],
                datetime(2026, 6, 26).date(),
                "Терминал 26.06.2026 Часть 1.xlsx",
                self.config("/tmp"),
                db=db,
            )

        self.assertEqual(rows[0]["Дата отгрузки"], "27.06.2026")
        self.assertEqual(rows[0]["Smartup delivery_date original"], "27.06.2026")
        self.assertEqual(rows[0]["Smartup delivery_date adjusted"], "")
        self.assertEqual(rows[0]["Smartup delivery_date skipped_dates"], "")

    def test_filter_requires_today_new_terminal(self):
        config = self.config("/tmp")
        orders = [
            sample_order(deal_id="ok"),
            sample_order(deal_id="wrong-date", deal_time="24.06.2026 09:10:00"),
            sample_order(deal_id="wrong-status", status="B#W"),
            sample_order(deal_id="wrong-payment", payment_type_code="PYMT:3"),
        ]

        filtered = filter_smartup_orders(orders, datetime(2026, 6, 25).date(), config)

        self.assertEqual([order["deal_id"] for order in filtered], ["ok"])

    def test_filter_can_limit_to_target_delivery_date(self):
        config = self.config("/tmp")
        orders = [
            sample_order(deal_id="ship-tomorrow", deal_time="30.06.2026 09:10:00", delivery_date="01.07.2026"),
            sample_order(deal_id="ship-later", deal_time="30.06.2026 10:10:00", delivery_date="02.07.2026"),
        ]

        filtered = filter_smartup_orders(
            orders,
            date(2026, 6, 30),
            config,
            target_delivery_date=date(2026, 7, 1),
        )

        self.assertEqual([order["deal_id"] for order in filtered], ["ship-tomorrow"])

    def test_target_delivery_date_includes_orders_from_previous_deal_date(self):
        config = self.config("/tmp")
        orders = [
            sample_order(deal_id="ship-target-old", deal_time="30.06.2026 09:10:00", delivery_date="02.07.2026"),
            sample_order(deal_id="ship-target-today", deal_time="01.07.2026 10:10:00", delivery_date="02.07.2026"),
            sample_order(deal_id="ship-other-date", deal_time="01.07.2026 11:10:00", delivery_date="03.07.2026"),
            sample_order(deal_id="wrong-payment", deal_time="30.06.2026 12:10:00", delivery_date="02.07.2026", payment_type_code="PYMT:3"),
        ]

        filtered = filter_smartup_orders(
            orders,
            date(2026, 7, 1),
            config,
            target_delivery_date=date(2026, 7, 2),
        )

        self.assertEqual([order["deal_id"] for order in filtered], ["ship-target-old", "ship-target-today"])

    def test_smartup_export_payload_uses_delivery_date_when_targeted(self):
        captured = {}

        class FakeResponse:
            text = "{}"

            def raise_for_status(self):
                return None

            def json(self):
                return {"order": []}

        class FakeHttpClient:
            def __init__(self, timeout):
                self.timeout = timeout

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url, headers, json, auth):
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                captured["auth"] = auth
                return FakeResponse()

        config = self.config(
            "/tmp",
            smartup_base_url="https://smartup.example",
            smartup_username="user",
            smartup_password="password",
        )

        with mock.patch("backend.app.smartup_auto_import.httpx.Client", FakeHttpClient):
            SmartupClient(config).export_orders(date(2026, 7, 1), target_delivery_date=date(2026, 7, 2))

        self.assertEqual(captured["json"]["begin_deal_date"], "")
        self.assertEqual(captured["json"]["end_deal_date"], "")
        self.assertEqual(captured["json"]["delivery_date"], "02.07.2026")
        self.assertEqual(captured["json"]["statuses"], ["B#N"])

    def test_smartup_status_readback_queries_exact_deals_without_write(self):
        captured = []

        class FakeResponse:
            text = "{}"

            def __init__(self, deal_id):
                self.deal_id = deal_id

            def raise_for_status(self):
                return None

            def json(self):
                statuses = {"642": "B#W", "643": "B#N"}
                return {"order": [{"deal_id": self.deal_id, "status": statuses[self.deal_id]}]}

        class FakeHttpClient:
            def __init__(self, timeout):
                self.timeout = timeout

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def post(self, url, headers, json, auth):
                captured.append({"url": url, "headers": headers, "json": json, "auth": auth})
                return FakeResponse(json["deal_id"])

        config = self.config(
            "/tmp",
            smartup_base_url="https://smartup.example",
            smartup_username="user",
            smartup_password="password",
        )

        with mock.patch("backend.app.smartup_auto_import.httpx.Client", FakeHttpClient):
            statuses = SmartupClient(config).get_deal_statuses(["642", "643", "642", " "])

        self.assertEqual(statuses, {"642": "B#W", "643": "B#N"})
        self.assertEqual([request["json"]["deal_id"] for request in captured], ["642", "643"])
        for request in captured:
            self.assertEqual(request["url"], "https://smartup.example/b/trade/txs/tdeal/order$export")
            self.assertEqual(request["json"]["begin_deal_date"], "")
            self.assertEqual(request["json"]["end_deal_date"], "")
            self.assertEqual(request["json"]["delivery_date"], "")
            self.assertEqual(request["json"]["statuses"], ["B#N", "B#W"])

    def test_build_import_rows_reverse_geocodes_smartup_coordinates_without_address(self):
        config = self.config("/tmp")
        order = sample_order(delivery_address_full="", delivery_address_short="")

        with mock.patch(
            "backend.app.smartup_auto_import.reverse_geocode_yandex",
            return_value=("Ташкент, геокодированный адрес 1", ""),
        ) as reverse_geocode:
            rows = build_import_rows(
                [order],
                datetime(2026, 6, 25).date(),
                "Терминал 25.06.2026 Часть 1.xlsx",
                config,
            )

        self.assertEqual(rows[0]["Адрес"], "Ташкент, геокодированный адрес 1")
        self.assertEqual(rows[0]["Координаты"], "41.311081,69.240562")
        reverse_geocode.assert_called_once_with("41.311081,69.240562", cache=mock.ANY)

    def test_build_import_rows_keeps_gps_fallback_when_reverse_geocode_fails(self):
        config = self.config("/tmp")
        order = sample_order(delivery_address_full="", delivery_address_short="")

        with mock.patch("backend.app.smartup_auto_import.reverse_geocode_yandex", return_value=("", "timeout")):
            rows = build_import_rows(
                [order],
                datetime(2026, 6, 25).date(),
                "Терминал 25.06.2026 Часть 1.xlsx",
                config,
            )

        self.assertEqual(rows[0]["Адрес"], "GPS: 41.311081,69.240562")
        self.assertEqual(rows[0]["Координаты"], "41.311081,69.240562")

    def test_build_import_rows_keeps_gps_fallback_when_reverse_geocode_raises(self):
        config = self.config("/tmp")
        order = sample_order(delivery_address_full="", delivery_address_short="")

        with mock.patch("backend.app.smartup_auto_import.reverse_geocode_yandex", side_effect=RuntimeError("timeout")):
            rows = build_import_rows(
                [order],
                datetime(2026, 6, 25).date(),
                "Терминал 25.06.2026 Часть 1.xlsx",
                config,
            )

        self.assertEqual(rows[0]["Адрес"], "GPS: 41.311081,69.240562")
        self.assertEqual(rows[0]["Координаты"], "41.311081,69.240562")

    def test_shadow_preview_writes_export_but_does_not_change_status_or_import(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake = FakeSmartupClient([sample_order()])
            config = self.config(tmp_dir, backend_import_enabled=False, change_status_enabled=False)
            with self.SessionLocal() as db:
                result = run_smartup_auto_import_once(
                    db,
                    config,
                    now=datetime(2026, 6, 25, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                    slot_label="12:00",
                    smartup_client=fake,
                )
                imports_count = len(db.execute(select(ImportJob)).scalars().all())

            self.assertEqual(result["status"], "shadow_preview")
            self.assertEqual(fake.changed, [])
            self.assertEqual(imports_count, 0)
            self.assertTrue((Path(tmp_dir) / "2026-06-25" / "Терминал 25.06.2026 Часть 1.xlsx").exists())

    def test_preview_failure_keeps_export_audit_artifact(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake = FakeSmartupClient([sample_order()])
            config = self.config(tmp_dir, backend_import_enabled=False, change_status_enabled=False)
            audit_path = Path(tmp_dir) / "2026-06-25" / "Терминал 25.06.2026 Часть 1.audit.json"
            with self.SessionLocal() as db:
                with mock.patch(
                    "backend.app.smartup_auto_import.preview_import",
                    side_effect=SmartupAutoImportError("preview boom"),
                ):
                    with self.assertRaisesRegex(SmartupAutoImportError, "preview boom"):
                        run_smartup_auto_import_once(
                            db,
                            config,
                            now=datetime(2026, 6, 25, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                            slot_label="12:00",
                            smartup_client=fake,
                        )

            self.assertTrue((Path(tmp_dir) / "2026-06-25" / "Терминал 25.06.2026 Часть 1.xlsx").exists())
            self.assertTrue(audit_path.exists())
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(audit["status"], "failed_preview")
            self.assertEqual(audit["filename"], "Терминал 25.06.2026 Часть 1.xlsx")
            self.assertIn("preview boom", audit["error"])

    def test_full_flow_changes_smartup_status_and_imports_by_delivery_date(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake = FakeSmartupClient([sample_order()])
            config = self.config(tmp_dir, backend_import_enabled=True, change_status_enabled=True)
            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "dry_run"}, clear=False):
                with self.SessionLocal() as db:
                    result = run_smartup_auto_import_once(
                        db,
                        config,
                        now=datetime(2026, 6, 25, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                        slot_label="12:00",
                        smartup_client=fake,
                    )
                    orders = db.execute(select(Order)).scalars().all()
                    imports = db.execute(select(ImportJob)).scalars().all()
                    dry_runs = list_skladbot_dry_runs(db, str(imports[0].id))
                    order_source_id = orders[0].raw_payload["source_order_id"]
                    item_source_ids = [item.raw_payload["source_order_id"] for item in orders[0].items]

            self.assertEqual(result["status"], "completed")
            self.assertEqual(fake.changed, [(["642"], "B#W")])
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0].order_date.isoformat(), "2026-06-26")
            self.assertEqual(orders[0].payment_type, "Терминал")
            self.assertEqual(order_source_id, "smartup:642")
            self.assertEqual(item_source_ids, ["smartup:642"])
            self.assertRegex(
                dry_runs[0]["payload"]["comment"],
                r"^Терминал\nТП\nSmartup ID: smartup:642\nTakSklad ref: TSF-[A-F0-9]{24}$",
            )
            self.assertEqual(len(imports), 1)
            self.assertEqual((imports[0].raw_payload["smartup_auto"]["delivery_dates"]), ["2026-06-26"])

    def test_full_flow_with_target_delivery_date_excludes_other_delivery_dates(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake = FakeSmartupClient([
                sample_order(deal_id="701", deal_time="30.06.2026 09:10:00", delivery_date="01.07.2026"),
                sample_order(deal_id="702", deal_time="30.06.2026 10:10:00", delivery_date="02.07.2026"),
            ])
            config = self.config(tmp_dir, backend_import_enabled=True, change_status_enabled=True)
            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "dry_run"}, clear=False):
                with self.SessionLocal() as db:
                    result = run_smartup_auto_import_once(
                        db,
                        config,
                        now=datetime(2026, 6, 30, 16, 1, tzinfo=ZoneInfo("Asia/Tashkent")),
                        slot_label="16:01",
                        target_delivery_date=date(2026, 7, 1),
                        smartup_client=fake,
                    )
                    orders = db.execute(select(Order)).scalars().all()
                    imports = db.execute(select(ImportJob)).scalars().all()

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["target_delivery_date"], "2026-07-01")
            self.assertEqual(result["selected_orders"], 1)
            self.assertEqual(result["delivery_dates"], ["2026-07-01"])
            self.assertEqual(result["deal_ids"], ["701"])
            self.assertEqual(fake.changed, [(["701"], "B#W")])
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0].order_date.isoformat(), "2026-07-01")
            self.assertEqual(imports[0].raw_payload["smartup_auto"]["delivery_dates"], ["2026-07-01"])

    def test_smartup_kiz_source_files_group_one_export_file_across_deal_imports(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake = FakeSmartupClient([
                sample_order(deal_id="701", deal_time="30.06.2026 09:10:00", delivery_date="01.07.2026"),
                sample_order(deal_id="702", deal_time="30.06.2026 10:10:00", delivery_date="01.07.2026"),
            ])
            config = self.config(tmp_dir, backend_import_enabled=True, change_status_enabled=True)
            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "dry_run"}, clear=False):
                with mock.patch(
                    "backend.app.smartup_auto_import.queue_skladbot_after_smartup_status",
                    side_effect=lambda db, imports, successful_deal_ids=None, **kwargs: imports,
                ):
                    with self.SessionLocal() as db:
                        result = run_smartup_auto_import_once(
                            db,
                            config,
                            now=datetime(2026, 6, 30, 16, 1, tzinfo=ZoneInfo("Asia/Tashkent")),
                            slot_label="16:01",
                            smartup_client=fake,
                        )
                        imports = db.execute(select(ImportJob)).scalars().all()
                        source_files = list_completed_kiz_source_files(db)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(imports), 2)
            self.assertEqual(len(source_files), 1)
            source_file = source_files[0]
            self.assertEqual(source_file["source_file"], "Терминал 30.06.2026 Часть 1.xlsx")
            self.assertTrue(source_file["source_key"].startswith("batch:smartup:2026-06-30:part:1:sha256:"))
            self.assertEqual(source_file["items"], 2)
            self.assertEqual(source_file["planned_blocks"], 40)
            self.assertEqual(source_file["scanned_blocks"], 0)
            self.assertEqual(source_file["remaining_blocks"], 40)
            self.assertFalse(source_file["completed"])
            self.assertEqual(source_file["dates"], ["2026-07-01"])
            self.assertEqual(
                {item.raw_payload["smartup_auto"]["source_batch_key"] for item in imports},
                {result["source_batch_key"]},
            )

    def test_full_flow_sends_smartup_file_to_client_but_not_logistics_before_final_slot(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake = FakeSmartupClient([sample_order()])
            sender = FakeTelegramSender()
            config = self.config(
                tmp_dir,
                backend_import_enabled=True,
                change_status_enabled=True,
                client_chat_id="-5271267499",
                logistics_chat_id="-1003515369435",
            )
            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "dry_run"}, clear=False):
                with self.SessionLocal() as db:
                    result = run_smartup_auto_import_once(
                        db,
                        config,
                        now=datetime(2026, 6, 25, 16, 1, tzinfo=ZoneInfo("Asia/Tashkent")),
                        slot_label="16:01",
                        smartup_client=fake,
                        telegram_sender=sender,
                    )
                    imports = db.execute(select(ImportJob)).scalars().all()

        self.assertEqual(result["client_export"]["status"], "sent")
        self.assertEqual(result["logistics_reports"], [])
        self.assertEqual(len(sender.documents), 1)
        chat_id, content, filename, caption = sender.documents[0]
        self.assertEqual(chat_id, "-5271267499")
        self.assertTrue(content)
        self.assertEqual(filename, "Терминал 25.06.2026 Часть 1.xlsx")
        self.assertIn("Smartup выгрузка за 25.06.2026", caption)
        self.assertEqual(imports[0].raw_payload["telegram_chat_id"], "-5271267499")

    def test_full_flow_queues_skladbot_create_after_smartup_status_change(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake = FakeSmartupClient([sample_order()])
            config = self.config(tmp_dir, backend_import_enabled=True, change_status_enabled=True)
            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
                with self.SessionLocal() as db:
                    result = run_smartup_auto_import_once(
                        db,
                        config,
                        now=datetime(2026, 6, 25, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                        slot_label="12:00",
                        smartup_client=fake,
                    )
                    create_events = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                    ).scalars().all()
                    imports = db.execute(select(ImportJob)).scalars().all()

            self.assertEqual(result["status"], "completed")
            self.assertEqual(fake.changed, [(["642"], "B#W")])
            self.assertEqual(len(create_events), 1)
            self.assertEqual(create_events[0].status, "pending")
            self.assertNotIn("not_before", create_events[0].payload)
            self.assertEqual(result["imports"][0]["skladbot_after_status"]["queued"], 1)
            self.assertEqual(imports[0].raw_payload["skladbot_dry_run"]["mode"], "enabled")

    def test_full_flow_imports_before_smartup_status_change_failure(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake = FailingStatusChangeSmartupClient([sample_order()])
            config = self.config(tmp_dir, backend_import_enabled=True, change_status_enabled=True)
            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
                with self.SessionLocal() as db:
                    with self.assertRaisesRegex(SmartupAutoImportError, "status change unavailable"):
                        run_smartup_auto_import_once(
                            db,
                            config,
                            now=datetime(2026, 6, 25, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                            slot_label="12:00",
                            smartup_client=fake,
                    )
                    orders = db.execute(select(Order)).scalars().all()
                    imports = db.execute(select(ImportJob)).scalars().all()
                    create_events = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                    ).scalars().all()

            self.assertEqual(fake.changed, [(["642"], "B#W")])
            self.assertEqual(len(orders), 1)
            self.assertEqual(len(imports), 1)
            self.assertEqual(imports[0].status, "completed")
            self.assertEqual(create_events, [])

    def test_partial_smartup_status_change_queues_only_confirmed_deal_imports(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake = PartialStatusChangeSmartupClient([
                sample_order(deal_id="701", deal_time="30.06.2026 09:10:00", delivery_date="01.07.2026"),
                sample_order(deal_id="702", deal_time="30.06.2026 10:10:00", delivery_date="01.07.2026"),
            ])
            config = self.config(tmp_dir, backend_import_enabled=True, change_status_enabled=True)
            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
                with self.SessionLocal() as db:
                    with self.assertRaisesRegex(SmartupAutoImportError, "Smartup status change failed"):
                        run_smartup_auto_import_once(
                            db,
                            config,
                            now=datetime(2026, 6, 30, 16, 1, tzinfo=ZoneInfo("Asia/Tashkent")),
                            slot_label="16:01",
                            smartup_client=fake,
                        )
                    imports = db.execute(select(ImportJob)).scalars().all()
                    create_events = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                    ).scalars().all()

            self.assertEqual(fake.changed, [(["701", "702"], "B#W")])
            self.assertEqual(len(imports), 2)
            imports_by_deal = {
                import_job.raw_payload["smartup_auto"]["deal_ids"][0]: import_job
                for import_job in imports
            }
            self.assertEqual(set(imports_by_deal), {"701", "702"})
            self.assertEqual(imports_by_deal["701"].raw_payload["skladbot_dry_run"]["mode"], "enabled")
            self.assertEqual(imports_by_deal["701"].raw_payload["skladbot_dry_run"]["queued"], 1)
            self.assertEqual(imports_by_deal["702"].raw_payload["skladbot_dry_run"]["mode"], "dry_run")
            self.assertEqual(imports_by_deal["702"].raw_payload["skladbot_dry_run"]["queued"], 0)
            self.assertEqual(len(create_events), 1)

    def test_delivery_dates_for_export_date_reads_import_metadata(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake = FakeSmartupClient([sample_order()])
            config = self.config(tmp_dir, backend_import_enabled=True, change_status_enabled=True)
            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "dry_run"}, clear=False):
                with self.SessionLocal() as db:
                    run_smartup_auto_import_once(
                        db,
                        config,
                        now=datetime(2026, 6, 25, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                        slot_label="12:00",
                        smartup_client=fake,
                    )
                    dates = delivery_dates_for_export_date(db, datetime(2026, 6, 25).date())

            self.assertEqual(dates, ["2026-06-26"])

    def test_status_summary_does_not_leak_secret_or_chat_values(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = self.config(
                tmp_dir,
                backend_import_enabled=True,
                change_status_enabled=True,
                smartup_username="raw-user",
                smartup_password="secret-pass",
                client_chat_id="-5271267499",
                logistics_chat_id="-1003515369435",
                alert_chat_id="-1003515369435",
                telegram_bot_token="bot123:secret-token",
            )
            with self.SessionLocal() as db:
                db.add(PendingEvent(
                    event_type=SMARTUP_AUTO_IMPORT_EVENT_TYPE,
                    idempotency_key="smartup:auto_import:v1:2026-06-30:16:01:delivery:2026-07-01",
                    status="completed",
                    attempts=2,
                    payload={
                        "export_date": "2026-06-30",
                        "target_delivery_date": "2026-07-01",
                        "slot": "16:01",
                        "result": {
                            "status": "completed",
                            "export_date": "2026-06-30",
                            "target_delivery_date": "2026-07-01",
                            "slot": "16:01",
                            "raw_orders": 45,
                            "selected_orders": 42,
                            "rows": 98,
                            "delivery_dates": ["2026-07-01"],
                            "imports": [{"import_id": "import-1"}],
                            "status_change": {"status": "B#W"},
                            "skladbot_processing": {"status": "skipped"},
                            "logistics_reports": [{"status": "sent"}],
                            "client_export": {"chat_id": "-5271267499", "status": "sent"},
                        },
                    },
                    last_error="password=secret-pass token=bot123:secret-token",
                ))
                db.add(PendingEvent(
                    event_type=SKLADBOT_REQUEST_CREATE_EVENT_TYPE,
                    idempotency_key="skladbot:create:order-1",
                    status="pending",
                    attempts=0,
                    payload={
                        "import_id": "import-1",
                        "request_payload": {"address": "Ташкент"},
                    },
                ))
                db.commit()

                status = build_smartup_auto_import_status(db, config, limit=3)

            rendered = json.dumps(status, ensure_ascii=False, sort_keys=True)
            self.assertEqual(status["status"], "ok")
            self.assertTrue(status["configuration"]["smartup_auth_configured"])
            self.assertTrue(status["configuration"]["client_chat_configured"])
            self.assertEqual(status["queues"]["pending_skladbot_request_creates"], 1)
            self.assertEqual(status["last_events"][0]["selected_orders"], 42)
            self.assertEqual(status["last_events"][0]["status_change"], "B#W")
            self.assertIn("password=***", status["last_events"][0]["last_error"])
            self.assertNotIn("raw-user", rendered)
            self.assertNotIn("secret-pass", rendered)
            self.assertNotIn("bot123:secret-token", rendered)
            self.assertNotIn("-5271267499", rendered)

    def test_backend_import_requires_smartup_status_change_gate(self):
        config = self.config("/tmp", backend_import_enabled=True, change_status_enabled=False)

        with self.SessionLocal() as db:
            with self.assertRaisesRegex(Exception, "CHANGE_STATUS"):
                run_smartup_auto_import_once(
                    db,
                    config,
                    now=datetime(2026, 6, 25, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                    slot_label="12:00",
                    smartup_client=FakeSmartupClient([sample_order()]),
                )

    def test_scheduled_slot_claim_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = self.config(tmp_dir, backend_import_enabled=False, change_status_enabled=False)
            with self.SessionLocal() as db:
                first = run_scheduled_smartup_auto_import_slot(
                    db,
                    config,
                    now=datetime(2026, 6, 25, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                    slot_label="12:00",
                    smartup_client=FakeSmartupClient([sample_order()]),
                )
                second = run_scheduled_smartup_auto_import_slot(
                    db,
                    config,
                    now=datetime(2026, 6, 25, 12, 1, tzinfo=ZoneInfo("Asia/Tashkent")),
                    slot_label="12:00",
                    smartup_client=FakeSmartupClient([sample_order(deal_id="643")]),
                )
                events = db.execute(select(PendingEvent)).scalars().all()
                history = list_smartup_auto_import_history(db, limit=10)

            self.assertEqual(first["status"], "shadow_preview")
            self.assertEqual(second["status"], "skipped")
            self.assertEqual(second["reason"], "slot_already_claimed")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].status, "completed")
            self.assertEqual(history["summary"]["total"], 1)
            self.assertEqual(history["runs"][0]["selected_orders"], 1)
            self.assertEqual(history["runs"][0]["filename"], "Терминал 25.06.2026 Часть 1.xlsx")

    def test_due_slot_targets_next_delivery_date(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake = FakeSmartupClient([
                sample_order(deal_id="old-deal", deal_time="30.06.2026 13:32:22", delivery_date="02.07.2026"),
                sample_order(deal_id="today-deal", deal_time="01.07.2026 09:10:00", delivery_date="02.07.2026"),
                sample_order(deal_id="future-delivery", deal_time="01.07.2026 10:10:00", delivery_date="03.07.2026"),
            ])
            config = self.config(tmp_dir, backend_import_enabled=False, change_status_enabled=False)
            with self.SessionLocal() as db:
                result = run_due_smartup_auto_imports(
                    db,
                    config,
                    now=datetime(2026, 7, 1, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                    smartup_client=fake,
                )
                events = db.execute(select(PendingEvent)).scalars().all()

        self.assertEqual(result[0]["status"], "shadow_preview")
        self.assertEqual(result[0]["export_date"], "2026-07-01")
        self.assertEqual(result[0]["target_delivery_date"], "2026-07-02")
        self.assertEqual(result[0]["selected_orders"], 2)
        self.assertEqual(result[0]["deal_ids"], ["old-deal", "today-deal"])
        self.assertEqual(fake.exports, [(date(2026, 7, 1), date(2026, 7, 2))])
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].idempotency_key,
            "smartup:auto_import:v1:2026-07-01:12:00:delivery:2026-07-02",
        )

    def test_due_slot_targets_all_dates_until_next_working_delivery_date(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake = FakeSmartupClient([
                sample_order(deal_id="saturday", deal_time="03.07.2026 08:49:15", delivery_date="04.07.2026"),
                sample_order(deal_id="sunday", deal_time="03.07.2026 10:17:51", delivery_date="05.07.2026"),
                sample_order(deal_id="monday", deal_time="03.07.2026 11:33:17", delivery_date="06.07.2026"),
                sample_order(deal_id="tuesday", deal_time="03.07.2026 12:56:24", delivery_date="07.07.2026"),
            ])
            config = self.config(tmp_dir, backend_import_enabled=False, change_status_enabled=False)
            with self.SessionLocal() as db:
                result = run_due_smartup_auto_imports(
                    db,
                    config,
                    now=datetime(2026, 7, 3, 17, 50, tzinfo=ZoneInfo("Asia/Tashkent")),
                    smartup_client=fake,
                )
                events = db.execute(
                    select(PendingEvent).where(PendingEvent.event_type == SMARTUP_AUTO_IMPORT_EVENT_TYPE)
                ).scalars().all()

        self.assertEqual([item["status"] for item in result], ["shadow_preview", "shadow_preview", "shadow_preview"])
        self.assertEqual(
            [item["target_delivery_date"] for item in result],
            ["2026-07-04", "2026-07-05", "2026-07-06"],
        )
        self.assertEqual([item["deal_ids"] for item in result], [["saturday"], ["sunday"], ["monday"]])
        self.assertEqual(
            fake.exports,
            [
                (date(2026, 7, 3), date(2026, 7, 4)),
                (date(2026, 7, 3), date(2026, 7, 5)),
                (date(2026, 7, 3), date(2026, 7, 6)),
            ],
        )
        self.assertEqual(
            sorted(event.idempotency_key for event in events),
            [
                "smartup:auto_import:v1:2026-07-03:17:50:delivery:2026-07-04",
                "smartup:auto_import:v1:2026-07-03:17:50:delivery:2026-07-05",
                "smartup:auto_import:v1:2026-07-03:17:50:delivery:2026-07-06",
            ],
        )

    def test_due_final_slot_sends_logistics_once_after_delivery_date_range(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake = FakeSmartupClient([
                sample_order(deal_id="saturday", deal_time="03.07.2026 08:49:15", delivery_date="04.07.2026"),
                sample_order(deal_id="sunday", deal_time="03.07.2026 10:17:51", delivery_date="05.07.2026"),
                sample_order(deal_id="monday", deal_time="03.07.2026 11:33:17", delivery_date="06.07.2026"),
            ])
            sender = FakeTelegramSender()
            config = self.config(
                tmp_dir,
                backend_import_enabled=True,
                change_status_enabled=True,
                client_chat_id="-5271267499",
                logistics_chat_id="-1003515369435",
            )
            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "dry_run"}, clear=False):
                with self.SessionLocal() as db:
                    result = run_due_smartup_auto_imports(
                        db,
                        config,
                        now=datetime(2026, 7, 3, 17, 50, tzinfo=ZoneInfo("Asia/Tashkent")),
                        smartup_client=fake,
                        telegram_sender=sender,
                    )
                    second_result = run_due_smartup_auto_imports(
                        db,
                        config,
                        now=datetime(2026, 7, 3, 17, 50, 30, tzinfo=ZoneInfo("Asia/Tashkent")),
                        smartup_client=fake,
                        telegram_sender=sender,
                    )
                    imports = db.execute(select(ImportJob)).scalars().all()
                    logistics_events = db.execute(
                        select(PendingEvent)
                        .where(PendingEvent.event_type == SMARTUP_LOGISTICS_REPORT_EVENT_TYPE)
                    ).scalars().all()

        self.assertEqual([item["status"] for item in result[:3]], ["completed", "completed", "completed"])
        self.assertEqual([item["delivery_dates"] for item in result[:3]], [
            ["2026-07-06"],
            ["2026-07-06"],
            ["2026-07-06"],
        ])
        self.assertEqual([item["logistics_reports"] for item in result[:3]], [[], [], []])
        self.assertEqual(result[3]["status"], "final_logistics_reports")
        self.assertEqual(result[3]["delivery_dates"], ["2026-07-06"])
        self.assertEqual(result[3]["logistics_reports"][0]["status"], "sent")
        self.assertEqual(result[3]["logistics_reports"][0]["delivery_date"], "2026-07-06")
        self.assertEqual(len(imports), 3)
        self.assertEqual(second_result[3]["status"], "final_logistics_reports")
        self.assertEqual(second_result[3]["delivery_dates"], ["2026-07-06"])
        self.assertEqual(second_result[3]["logistics_reports"][0]["status"], "skipped")
        self.assertEqual(second_result[3]["logistics_reports"][0]["reason"], "already_sent")
        self.assertEqual(len(logistics_events), 1)
        self.assertEqual(logistics_events[0].status, "completed")
        self.assertEqual(fake.changed, [
            (["saturday"], "B#W"),
            (["sunday"], "B#W"),
            (["monday"], "B#W"),
        ])
        self.assertEqual(len(sender.documents), 4)
        logistics_documents = [
            document for document in sender.documents if document[0] == "-1003515369435"
        ]
        self.assertEqual(len(logistics_documents), 1)
        _chat_id, content, filename, caption = logistics_documents[0]
        self.assertEqual(filename, "TakSklad_логистика_06.07.2026.xlsx")
        self.assertIn("Отчёт логистики за 06.07.2026", caption)
        self.assert_xlsx_has_no_orphaned_pane_selections(content)
        workbook = openpyxl.load_workbook(BytesIO(content), data_only=True)
        self.assertIn("Orders", workbook.sheetnames)
        workbook.close()

    def test_scheduled_target_delivery_date_is_next_calendar_day(self):
        self.assertEqual(scheduled_smartup_target_delivery_date(date(2026, 7, 1)), date(2026, 7, 2))

    def test_scheduled_target_delivery_dates_honor_working_weekend_override(self):
        config = self.config("/tmp", disabled_weekdays=(5, 6))
        with self.SessionLocal() as db:
            db.add(LogisticsCalendarDay(
                service_date=date(2026, 7, 4),
                is_non_working=False,
                reason="Рабочая суббота",
                source="web",
                raw_payload={},
            ))
            db.commit()

            result = scheduled_smartup_target_delivery_dates(db, date(2026, 7, 3), config)

        self.assertEqual(result, [date(2026, 7, 4)])

    def test_final_logistics_report_uses_existing_audit_as_idempotency_guard(self):
        sender = FakeTelegramSender()
        config = self.config(
            "/tmp",
            logistics_chat_id="-1003515369435",
        )
        with self.SessionLocal() as db:
            db.add(AuditLog(
                action="smartup_auto_import_logistics_report",
                entity_type="delivery_date",
                entity_id="2026-07-06",
                payload={
                    "status": "sent",
                    "delivery_date": "2026-07-06",
                    "filename": "TakSklad_логистика_06.07.2026.xlsx",
                },
            ))
            db.commit()

            result = send_final_logistics_reports(
                db,
                config,
                export_date=date(2026, 7, 3),
                telegram_sender=sender,
                extra_delivery_dates=["2026-07-06"],
            )
            logistics_events = db.execute(
                select(PendingEvent)
                .where(PendingEvent.event_type == SMARTUP_LOGISTICS_REPORT_EVENT_TYPE)
            ).scalars().all()

        self.assertEqual(result[0]["status"], "skipped")
        self.assertEqual(result[0]["reason"], "already_sent")
        self.assertEqual(len(sender.documents), 0)
        self.assertEqual(len(logistics_events), 1)
        self.assertEqual(logistics_events[0].status, "completed")

    def test_failed_scheduled_slot_can_be_retried_without_duplicate_order(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = self.config(tmp_dir, backend_import_enabled=True, change_status_enabled=True)
            sender = FakeTelegramSender()
            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
                with self.SessionLocal() as db:
                    with self.assertRaisesRegex(SmartupAutoImportError, "status change unavailable"):
                        run_scheduled_smartup_auto_import_slot(
                            db,
                            config,
                            now=datetime(2026, 6, 25, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                            slot_label="12:00",
                            smartup_client=FailingStatusChangeSmartupClient([sample_order()]),
                            telegram_sender=sender,
                        )
                    failed_event = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SMARTUP_AUTO_IMPORT_EVENT_TYPE)
                    ).scalar_one()
                    self.assertEqual(failed_event.status, "failed")
                    self.assertEqual(failed_event.attempts, 1)

                    retry_client = FakeSmartupClient([sample_order()])
                    result = run_scheduled_smartup_auto_import_slot(
                        db,
                        config,
                        now=datetime(2026, 6, 25, 12, 1, tzinfo=ZoneInfo("Asia/Tashkent")),
                        slot_label="12:00",
                        smartup_client=retry_client,
                        telegram_sender=sender,
                    )
                    events = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SMARTUP_AUTO_IMPORT_EVENT_TYPE)
                    ).scalars().all()
                    orders = db.execute(select(Order)).scalars().all()
                    imports = db.execute(select(ImportJob)).scalars().all()
                    create_events = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                    ).scalars().all()

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["imports"][0]["orders_created"], 0)
            self.assertEqual(result["imports"][0]["duplicate_rows"], 1)
            self.assertEqual(result["imports"][0]["resolved_order_ids"], [str(orders[0].id)])
            self.assertEqual(retry_client.changed, [(["642"], "B#W")])
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].status, "completed")
            self.assertEqual(events[0].attempts, 2)
            self.assertEqual(len(orders), 1)
            self.assertEqual(len(imports), 2)
            self.assertEqual(len(create_events), 1)
            self.assertEqual(create_events[0].aggregate_id, str(orders[0].id))

    def test_enforced_saga_persists_intent_before_change_and_queues_one_skladbot_key(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = self.config(
                tmp_dir,
                backend_import_enabled=True,
                change_status_enabled=True,
                saga_mode="enforced",
            )
            observed_states = []
            parent = self

            class ObservingClient(FakeSmartupClient):
                def change_status(self, deal_ids, status_code):
                    with parent.SessionLocal() as observer:
                        events = observer.execute(
                            select(PendingEvent).where(PendingEvent.event_type == SMARTUP_DEAL_SAGA_EVENT_TYPE)
                        ).scalars().all()
                        observed_states.extend((event.payload or {}).get("saga_state") for event in events)
                    return super().change_status(deal_ids, status_code)

            fake = ObservingClient([sample_order()])
            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
                with self.SessionLocal() as db:
                    result = run_smartup_auto_import_once(
                        db,
                        config,
                        now=datetime(2026, 6, 25, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                        slot_label="12:00",
                        smartup_client=fake,
                        telegram_sender=FakeTelegramSender(),
                    )
                    sagas = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SMARTUP_DEAL_SAGA_EVENT_TYPE)
                    ).scalars().all()
                    creates = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                    ).scalars().all()

            self.assertEqual(result["status"], "completed")
            self.assertEqual(observed_states, ["remote_write_started"])
            self.assertEqual(fake.changed, [(["642"], "B#W")])
            self.assertEqual(len(sagas), 1)
            self.assertEqual((sagas[0].payload or {}).get("saga_state"), "skladbot_queued")
            self.assertEqual((sagas[0].payload or {}).get("skladbot_event_count"), 1)
            self.assertEqual(len(creates), 1)
            self.assertEqual((sagas[0].payload or {}).get("skladbot_event_key"), creates[0].idempotency_key)
            self.assertEqual(len(result["smartup_saga"]["workflow_key_hashes"]), 1)

    def test_saga_mode_rejects_inline_skladbot_processing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake = FakeSmartupClient([sample_order()])
            processing_config = self.config(
                tmp_dir,
                backend_import_enabled=True,
                change_status_enabled=True,
                saga_mode="enforced",
                process_skladbot_now=True,
            )
            with self.SessionLocal() as db:
                with self.assertRaisesRegex(SmartupAutoImportError, "немедленную обработку SkladBot"):
                    run_smartup_auto_import_once(
                        db,
                        processing_config,
                        now=datetime(2026, 6, 25, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                        slot_label="12:00",
                        smartup_client=fake,
                        telegram_sender=FakeTelegramSender(),
                    )

            self.assertEqual(fake.exports, [])
            self.assertEqual(fake.changed, [])

    def test_orphan_recovery_ignores_legacy_import_without_enforced_provenance(self):
        config = self.config(
            "/tmp",
            backend_import_enabled=True,
            change_status_enabled=True,
            saga_mode="enforced",
        )
        metadata = {
            "export_date": "2026-06-25",
            "slot": "12:00",
            "target_delivery_date": "",
            "delivery_dates": ["2026-06-26"],
            "deal_ids": ["642"],
        }
        with self.SessionLocal() as db:
            import_job = ImportJob(
                source="smartup_auto",
                status="completed",
                rows_total=1,
                rows_imported=1,
                raw_payload={"smartup_auto": metadata, "items_created": 1},
            )
            db.add(import_job)
            db.commit()
            import_id = str(import_job.id)

            ignored = prepare_orphaned_smartup_sagas(
                db,
                config,
                export_date=date(2026, 6, 25),
                slot_label="12:00",
                target_delivery_date=None,
            )
            self.assertEqual(ignored, [])

            import_job.raw_payload = {
                **(import_job.raw_payload or {}),
                "smartup_auto": {**metadata, "saga_mode": "enforced"},
            }
            db.commit()
            recovered = prepare_orphaned_smartup_sagas(
                db,
                config,
                export_date=date(2026, 6, 25),
                slot_label="12:00",
                target_delivery_date=None,
            )
            recovered_import_id = (recovered[0].payload or {}).get("import_id")

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered_import_id, import_id)

    def test_enforced_saga_reconciles_remote_success_without_duplicate_write(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = self.config(
                tmp_dir,
                backend_import_enabled=True,
                change_status_enabled=True,
                saga_mode="enforced",
            )
            fake = FakeSmartupClient([sample_order()])
            failed_once = {"value": False}

            def inject_fault(boundary, _deal_id):
                if boundary == "smartup_to_local" and not failed_once["value"]:
                    failed_once["value"] = True
                    raise RuntimeError("synthetic Smartup-to-local fault")

            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
                with self.SessionLocal() as db, mock.patch(
                    "backend.app.smartup_saga.smartup_saga_fault",
                    side_effect=inject_fault,
                ):
                    with self.assertRaisesRegex(RuntimeError, "Smartup-to-local"):
                        run_scheduled_smartup_auto_import_slot(
                            db,
                            config,
                            now=datetime(2026, 6, 25, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                            slot_label="12:00",
                            smartup_client=fake,
                            telegram_sender=FakeTelegramSender(),
                        )

                fake.orders = []
                with self.SessionLocal() as db:
                    result = run_scheduled_smartup_auto_import_slot(
                        db,
                        config,
                        now=datetime(2026, 6, 25, 12, 1, tzinfo=ZoneInfo("Asia/Tashkent")),
                        slot_label="12:00",
                        smartup_client=fake,
                        telegram_sender=FakeTelegramSender(),
                    )
                    sagas = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SMARTUP_DEAL_SAGA_EVENT_TYPE)
                    ).scalars().all()
                    creates = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                    ).scalars().all()
                    slots = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SMARTUP_AUTO_IMPORT_EVENT_TYPE)
                    ).scalars().all()

            self.assertEqual(result["status"], "completed_recovery")
            self.assertEqual(fake.changed, [(["642"], "B#W")])
            self.assertEqual(fake.status_reads, [["642"]])
            self.assertEqual(len(fake.exports), 1)
            self.assertEqual(len(sagas), 1)
            self.assertEqual(len(creates), 1)
            self.assertEqual(len(slots), 1)
            self.assertEqual(slots[0].attempts, 2)

    def test_fulfillment_sweeper_recovers_without_original_schedule_slot(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = self.config(
                tmp_dir,
                backend_import_enabled=True,
                change_status_enabled=True,
                saga_mode="enforced",
            )
            fake = FakeSmartupClient([sample_order()])

            def inject_fault(boundary, _deal_id):
                if boundary == "smartup_to_local":
                    raise RuntimeError("synthetic lost Smartup response")

            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
                with self.SessionLocal() as db, mock.patch(
                    "backend.app.smartup_saga.smartup_saga_fault",
                    side_effect=inject_fault,
                ):
                    with self.assertRaisesRegex(RuntimeError, "lost Smartup response"):
                        run_smartup_auto_import_once(
                            db,
                            config,
                            now=datetime(2026, 6, 25, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                            slot_label="12:00",
                            smartup_client=fake,
                            telegram_sender=FakeTelegramSender(),
                        )

                with self.SessionLocal() as db:
                    sweep = sweep_incomplete_smartup_fulfillments(db, config, client=fake)
                    fulfillment = db.execute(select(SmartupFulfillment)).scalar_one()
                    creates = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                    ).scalars().all()

            self.assertEqual(sweep["status"], "completed")
            self.assertEqual(fake.changed, [(["642"], "B#W")])
            self.assertEqual(fake.status_reads, [["642"]])
            self.assertEqual(fulfillment.state, "skladbot_create_queued")
            self.assertEqual(len(creates), 1)

    def test_enforced_saga_keeps_slot_retryable_until_skladbot_key_exists(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = self.config(
                tmp_dir,
                backend_import_enabled=True,
                change_status_enabled=True,
                saga_mode="enforced",
            )
            fake = FakeSmartupClient([sample_order()])
            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "dry_run"}, clear=False):
                with self.SessionLocal() as db:
                    with self.assertRaisesRegex(SmartupAutoImportError, "remain retryable"):
                        run_scheduled_smartup_auto_import_slot(
                            db,
                            config,
                            now=datetime(2026, 6, 25, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                            slot_label="12:00",
                            smartup_client=fake,
                            telegram_sender=FakeTelegramSender(),
                        )
                    slot = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SMARTUP_AUTO_IMPORT_EVENT_TYPE)
                    ).scalar_one()
                    saga = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SMARTUP_DEAL_SAGA_EVENT_TYPE)
                    ).scalar_one()
                    self.assertEqual(slot.status, "failed")
                    self.assertEqual((saga.payload or {}).get("saga_state"), "skladbot_pending")

            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
                with self.SessionLocal() as db:
                    result = run_scheduled_smartup_auto_import_slot(
                        db,
                        config,
                        now=datetime(2026, 6, 25, 12, 1, tzinfo=ZoneInfo("Asia/Tashkent")),
                        slot_label="12:00",
                        smartup_client=fake,
                        telegram_sender=FakeTelegramSender(),
                    )
                    creates = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                    ).scalars().all()

            self.assertEqual(result["status"], "completed_recovery")
            self.assertEqual(fake.changed, [(["642"], "B#W")])
            self.assertEqual(len(creates), 1)

    def test_enforced_saga_recovers_local_to_skladbot_fault(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = self.config(
                tmp_dir,
                backend_import_enabled=True,
                change_status_enabled=True,
                saga_mode="enforced",
            )
            fake = FakeSmartupClient([sample_order()])

            def inject_fault(boundary, _deal_id):
                if boundary == "local_state_to_skladbot":
                    raise RuntimeError("synthetic local-to-SkladBot fault")

            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
                with self.SessionLocal() as db, mock.patch(
                    "backend.app.smartup_auto_import.smartup_saga_fault",
                    side_effect=inject_fault,
                ):
                    with self.assertRaisesRegex(RuntimeError, "local-to-SkladBot"):
                        run_scheduled_smartup_auto_import_slot(
                            db,
                            config,
                            now=datetime(2026, 6, 25, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                            slot_label="12:00",
                            smartup_client=fake,
                            telegram_sender=FakeTelegramSender(),
                        )

                with self.SessionLocal() as db:
                    saga = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SMARTUP_DEAL_SAGA_EVENT_TYPE)
                    ).scalar_one()
                    self.assertEqual((saga.payload or {}).get("saga_state"), "remote_confirmed")
                    result = run_scheduled_smartup_auto_import_slot(
                        db,
                        config,
                        now=datetime(2026, 6, 25, 12, 1, tzinfo=ZoneInfo("Asia/Tashkent")),
                        slot_label="12:00",
                        smartup_client=fake,
                        telegram_sender=FakeTelegramSender(),
                    )
                    creates = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                    ).scalars().all()

            self.assertEqual(result["status"], "completed_recovery")
            self.assertEqual(fake.changed, [(["642"], "B#W")])
            self.assertEqual(fake.status_reads, [])
            self.assertEqual(len(creates), 1)

    def test_enforced_saga_recovers_import_to_intent_without_second_import(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = self.config(
                tmp_dir,
                backend_import_enabled=True,
                change_status_enabled=True,
                saga_mode="enforced",
            )
            fake = FakeSmartupClient([sample_order()])

            def inject_fault(boundary, _deal_id):
                if boundary == "import_to_intent":
                    raise RuntimeError("synthetic import-to-intent fault")

            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "enabled"}, clear=False):
                with self.SessionLocal() as db, mock.patch(
                    "backend.app.smartup_saga.smartup_saga_fault",
                    side_effect=inject_fault,
                ):
                    with self.assertRaisesRegex(RuntimeError, "import-to-intent"):
                        run_scheduled_smartup_auto_import_slot(
                            db,
                            config,
                            now=datetime(2026, 6, 25, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                            slot_label="12:00",
                            smartup_client=fake,
                            telegram_sender=FakeTelegramSender(),
                        )
                    self.assertEqual(len(db.execute(select(ImportJob)).scalars().all()), 1)
                    self.assertEqual(db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SMARTUP_DEAL_SAGA_EVENT_TYPE)
                    ).scalars().all(), [])

                with self.SessionLocal() as db:
                    result = run_scheduled_smartup_auto_import_slot(
                        db,
                        config,
                        now=datetime(2026, 6, 25, 12, 1, tzinfo=ZoneInfo("Asia/Tashkent")),
                        slot_label="12:00",
                        smartup_client=fake,
                        telegram_sender=FakeTelegramSender(),
                    )
                    imports = db.execute(select(ImportJob)).scalars().all()
                    sagas = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SMARTUP_DEAL_SAGA_EVENT_TYPE)
                    ).scalars().all()
                    creates = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SKLADBOT_REQUEST_CREATE_EVENT_TYPE)
                    ).scalars().all()

            self.assertEqual(result["status"], "completed_recovery")
            self.assertEqual(len(fake.exports), 1)
            self.assertEqual(fake.changed, [(["642"], "B#W")])
            self.assertEqual(len(imports), 1)
            self.assertEqual(len(sagas), 1)
            self.assertEqual(len(creates), 1)
            self.assertEqual((sagas[0].payload or {}).get("import_id"), str(imports[0].id))

    def test_stale_processing_scheduled_slot_can_be_retried(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = self.config(tmp_dir, backend_import_enabled=True, change_status_enabled=True)
            stale_time = datetime(2026, 6, 25, 6, 0, tzinfo=timezone.utc)
            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "dry_run"}, clear=False):
                with self.SessionLocal() as db:
                    db.add(PendingEvent(
                        event_type=SMARTUP_AUTO_IMPORT_EVENT_TYPE,
                        idempotency_key=smartup_slot_idempotency_key(date(2026, 6, 25), "12:00"),
                        status="processing",
                        attempts=1,
                        payload={"version": 1, "claimed_at": stale_time.isoformat()},
                        created_at=stale_time - timedelta(minutes=1),
                        updated_at=stale_time,
                    ))
                    db.commit()

                    result = run_scheduled_smartup_auto_import_slot(
                        db,
                        config,
                        now=datetime(2026, 6, 25, 12, 45, tzinfo=ZoneInfo("Asia/Tashkent")),
                        slot_label="12:00",
                        smartup_client=FakeSmartupClient([sample_order()]),
                        telegram_sender=FakeTelegramSender(),
                    )
                    event = db.execute(
                        select(PendingEvent).where(PendingEvent.event_type == SMARTUP_AUTO_IMPORT_EVENT_TYPE)
                    ).scalar_one()
                    orders = db.execute(select(Order)).scalars().all()

            self.assertEqual(result["status"], "completed")
            self.assertEqual(event.status, "completed")
            self.assertEqual(event.attempts, 2)
            self.assertEqual(event.payload["retry_reason"], "stale_processing")
            self.assertEqual(len(orders), 1)

    def test_due_slots_skip_disabled_weekdays(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake = FakeSmartupClient([sample_order()])
            config = self.config(tmp_dir, disabled_weekdays=(5, 6))
            with self.SessionLocal() as db:
                result = run_due_smartup_auto_imports(
                    db,
                    config,
                    now=datetime(2026, 6, 27, 12, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                    smartup_client=fake,
                )
                events = db.execute(select(PendingEvent)).scalars().all()

            self.assertEqual(result[0]["status"], "idle")
            self.assertEqual(result[0]["reason"], "weekday_disabled")
            self.assertEqual(result[0]["weekday"], 5)
            self.assertEqual(events, [])
            self.assertEqual(fake.changed, [])

    def test_final_logistics_report_skips_non_working_delivery_date(self):
        sender = FakeTelegramSender()
        config = self.config("/tmp", logistics_chat_id="-1003515369435", disabled_weekdays=(5, 6))
        with self.SessionLocal() as db:
            result = send_final_logistics_reports(
                db,
                config,
                export_date=date(2026, 6, 26),
                extra_delivery_dates=["2026-06-27"],
                telegram_sender=sender,
            )

        self.assertEqual(result, [{
            "status": "skipped",
            "delivery_date": "2026-06-27",
            "reason": "non_working_logistics_day",
        }])
        self.assertEqual(sender.documents, [])

    def test_disabled_weekdays_parser_accepts_numbers_and_labels(self):
        self.assertEqual(parse_disabled_weekdays("sat,sunday"), (5, 6))
        self.assertEqual(parse_disabled_weekdays("5,6"), (5, 6))

    def test_scheduled_slot_failure_sends_telegram_alert(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            sender = FakeTelegramSender()
            config = self.config(tmp_dir, alert_chat_id="-1003515369435")
            with self.SessionLocal() as db:
                with self.assertRaisesRegex(SmartupAutoImportError, "test failure"):
                    run_scheduled_smartup_auto_import_slot(
                        db,
                        config,
                        now=datetime(2026, 6, 25, 15, 0, tzinfo=ZoneInfo("Asia/Tashkent")),
                        slot_label="15:00",
                        smartup_client=FailingSmartupClient(),
                        telegram_sender=sender,
                    )
                events = db.execute(select(PendingEvent)).scalars().all()

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].status, "failed")
            self.assertEqual(sender.messages[0][0], "-1003515369435")
            self.assertIn("15:00", sender.messages[0][1])
            self.assertIn("Smartup test failure", sender.messages[0][1])

    def test_smartup_advisory_lock_key_is_stable_signed_bigint(self):
        first = smartup_advisory_lock_key(datetime(2026, 6, 25).date(), "17:50")
        second = smartup_advisory_lock_key(datetime(2026, 6, 25).date(), "17:50")

        self.assertEqual(first, second)
        self.assertGreaterEqual(first, -(2**63))
        self.assertLess(first, 2**63)

    def test_smartup_slot_key_includes_target_delivery_date_only_when_set(self):
        old_key = smartup_slot_idempotency_key(date(2026, 6, 30), "16:01")
        filtered_key = smartup_slot_idempotency_key(
            date(2026, 6, 30),
            "16:01",
            target_delivery_date=date(2026, 7, 1),
        )

        self.assertEqual(old_key, "smartup:auto_import:v1:2026-06-30:16:01")
        self.assertEqual(filtered_key, "smartup:auto_import:v1:2026-06-30:16:01:delivery:2026-07-01")

    def test_run_once_cli_passes_target_delivery_date(self):
        args = smartup_auto_import_worker.parse_args([
            "run-once",
            "--date",
            "2026-06-30",
            "--slot",
            "16:01",
            "--delivery-date",
            "2026-07-01",
        ])
        config = self.config("/tmp")

        with mock.patch.object(smartup_auto_import_worker, "SessionLocal") as session_local:
            db = mock.Mock()
            session_local.return_value.__enter__.return_value = db
            with mock.patch.object(
                smartup_auto_import_worker,
                "run_scheduled_smartup_auto_import_slot",
                return_value={"status": "completed"},
            ) as run_slot:
                exit_code = smartup_auto_import_worker.run_once(args, config)

        self.assertEqual(exit_code, 0)
        run_slot.assert_called_once()
        self.assertEqual(run_slot.call_args.kwargs["now"], datetime(2026, 6, 30, 16, 1, tzinfo=config.timezone))
        self.assertEqual(run_slot.call_args.kwargs["target_delivery_date"], date(2026, 7, 1))

    def test_postgres_advisory_lock_uses_dedicated_connection(self):
        connection = FakePostgresConnection(acquired=True)
        db = FakeDbSession(FakePostgresBind(connection))

        acquired, lock_connection = acquire_smartup_slot_advisory_lock(
            db,
            datetime(2026, 6, 25).date(),
            "12:00",
        )
        release_smartup_slot_advisory_lock(lock_connection, datetime(2026, 6, 25).date(), "12:00")

        self.assertTrue(acquired)
        self.assertIs(lock_connection, connection)
        self.assertIn("pg_try_advisory_lock", connection.executed[0][0])
        self.assertIn("pg_advisory_unlock", connection.executed[1][0])
        self.assertEqual(connection.commits, 2)
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
