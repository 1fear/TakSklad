import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.models import Base, ImportJob, LogisticsCalendarDay, Order, PendingEvent
from backend.app.smartup_auto_import import (
    SmartupAutoImportConfig,
    SmartupAutoImportError,
    SMARTUP_AUTO_IMPORT_EVENT_TYPE,
    build_smartup_auto_import_status,
    build_import_rows,
    delivery_dates_for_export_date,
    filter_smartup_orders,
    parse_disabled_weekdays,
    preview_delivery_groups,
    run_due_smartup_auto_imports,
    run_smartup_auto_import_once,
    run_scheduled_smartup_auto_import_slot,
    acquire_smartup_slot_advisory_lock,
    release_smartup_slot_advisory_lock,
    send_final_logistics_reports,
    smartup_advisory_lock_key,
    smartup_slot_idempotency_key,
)
from backend.app import smartup_auto_import_worker
from backend.app.kiz_reports_service import list_completed_kiz_source_files
from backend.app.smartup_auto_import_history_service import list_smartup_auto_import_history
from backend.app.skladbot_request_dry_run import SKLADBOT_REQUEST_CREATE_EVENT_TYPE


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
    def __init__(self, orders):
        self.orders = orders
        self.changed = []

    def export_orders(self, export_date):
        return {"order": self.orders}

    def change_status(self, deal_ids, status_code):
        self.changed.append((list(deal_ids), status_code))
        return {
            "successes": [{"code": deal_id} for deal_id in deal_ids],
            "errors": [],
            "submitted": len(deal_ids),
            "status": status_code,
        }


class FailingSmartupClient:
    def export_orders(self, export_date):
        raise SmartupAutoImportError("Smartup test failure")


class FailingStatusChangeSmartupClient(FakeSmartupClient):
    def change_status(self, deal_ids, status_code):
        self.changed.append((list(deal_ids), status_code))
        raise SmartupAutoImportError("Smartup status change unavailable")


class PartialStatusChangeSmartupClient(FakeSmartupClient):
    def change_status(self, deal_ids, status_code):
        self.changed.append((list(deal_ids), status_code))
        return {
            "successes": [{"deal_id": deal_ids[0]}],
            "errors": [{"deal_id": deal_ids[1], "message": "locked"}],
            "submitted": len(deal_ids),
            "status": status_code,
        }


class FakeTelegramSender:
    configured = True

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

            self.assertEqual(result["status"], "completed")
            self.assertEqual(fake.changed, [(["642"], "B#W")])
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0].order_date.isoformat(), "2026-06-26")
            self.assertEqual(orders[0].payment_type, "Терминал")
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

    def test_failed_scheduled_slot_can_be_retried_without_duplicate_order(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = self.config(tmp_dir, backend_import_enabled=True, change_status_enabled=True)
            sender = FakeTelegramSender()
            with mock.patch.dict("os.environ", {"SKLADBOT_CREATE_REQUESTS_MODE": "dry_run"}, clear=False):
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

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["imports"][0]["orders_created"], 0)
            self.assertEqual(result["imports"][0]["duplicate_rows"], 1)
            self.assertEqual(retry_client.changed, [(["642"], "B#W")])
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].status, "completed")
            self.assertEqual(events[0].attempts, 2)
            self.assertEqual(len(orders), 1)
            self.assertEqual(len(imports), 2)

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
