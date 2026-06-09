import shutil
import tempfile
import unittest
from pathlib import Path

import openpyxl
import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import telegram_worker as telegram_worker_module
from backend.app import excel_importer
from backend.app.excel_importer import excel_file_to_import_payload
from backend.app.models import Base, PendingEvent
from backend.app.telegram_worker import (
    TELEGRAM_BUTTON_KIZ_BY_FILES,
    TELEGRAM_BUTTON_LOGISTICS_REPORT,
    TELEGRAM_BUTTON_SHIPMENT_DATE,
    TELEGRAM_BUTTON_STATUS,
    TELEGRAM_KIZ_FILE_PREFIX,
    TelegramWorker,
    display_date,
    summarize_active_orders_by_date,
    telegram_bot_commands,
)


def create_orders_workbook(path):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Заявки"
    sheet.append(["Документ от 30.05.2026"])
    sheet.append([
        "Клиент",
        "Тип оплаты",
        "Товары",
        "Кол-во ШТ",
        "Кол-во блок",
        "Адрес",
        "Торговый представитель",
    ])
    sheet.append(["Client One", "Терминал", "Product One", 20, 2, "Address One", "Rep One"])
    sheet.append(["Итого", "", "", 20, 2, "", ""])
    workbook.save(path)


def create_conflicting_date_workbook(path):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Конструктор отчетов"
    sheet.append([
        "Торговый представитель",
        "Клиент",
        "Координаты клиента",
        "ТМЦ",
        "Тип оплаты",
        "Дата доставки",
        "Количество заказа",
        "Сумма с переоценкой",
    ])
    sheet.append([
        "ТП1",
        "Client One",
        "41.320075,69.298547",
        "Chapman Brown OP 20",
        "Перечисление",
        "09.06.2026",
        20,
        480000,
    ])
    workbook.save(path)


class BackendTelegramImportTests(unittest.TestCase):
    def test_excel_file_to_import_payload_converts_rows_for_backend_import(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "orders_30_05_2026.xlsx"
            create_orders_workbook(path)

            payload = excel_file_to_import_payload(path, file_name=path.name, source="telegram")

        self.assertEqual(payload["source"], "telegram")
        self.assertEqual(payload["filename"], "orders_30_05_2026.xlsx")
        self.assertEqual(len(payload["rows"]), 1)
        self.assertEqual(payload["meta"]["source_rows_count"], 1)
        row = payload["rows"][0]
        self.assertEqual(row["Дата отгрузки"], "30.05.2026")
        self.assertEqual(row["Клиент"], "Client One")
        self.assertEqual(row["Тип оплаты"], "Терминал")
        self.assertEqual(row["Товары"], "Product One")
        self.assertEqual(row["Кол-во ШТ"], 20)
        self.assertEqual(row["Кол-во блок"], 2)
        self.assertEqual(row["Источник файла"], "orders_30_05_2026.xlsx")

    def test_excel_file_to_import_payload_reads_delivery_date_from_upper_header(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "Шаблон_отправки_заказов_на_склад_04_06_2026.xlsx"
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = "Конструктор отчетов"
            sheet.append(["", "", "", "", "", "", "", "ИТОГО", "", "ДАТА ДОСТАВКИ"])
            sheet.append([
                "Торговый представитель",
                "Клиент",
                "Координаты клиента",
                "",
                "",
                "ТМЦ",
                "Тип оплаты",
                "Количество заказа",
                "Сумма с переоценкой",
                "",
            ])
            sheet.append([
                "ТП1",
                "Client One",
                "41.320075",
                "69.298547",
                "41.320075,69.298547",
                "Chapman Brown OP 20",
                "Перечисление",
                20,
                480000,
                "2026-06-05",
            ])
            workbook.save(path)

            payload = excel_file_to_import_payload(path, file_name=path.name, source="telegram")

        self.assertEqual(len(payload["rows"]), 1)
        self.assertEqual(payload["rows"][0]["Дата отгрузки"], "05.06.2026")
        self.assertEqual(payload["meta"]["shipment_date"], "05.06.2026")
        self.assertEqual(payload["meta"]["shipment_dates"], ["05.06.2026"])

    def test_excel_file_to_import_payload_rejects_telegram_date_conflict_with_excel_date(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "Шаблон_отправки_заказов_на_склад_09_06_2026.xlsx"
            create_conflicting_date_workbook(path)

            with self.assertRaisesRegex(ValueError, "не совпадает с датой в Excel"):
                excel_file_to_import_payload(
                    path,
                    file_name=path.name,
                    source="telegram",
                    shipment_date="08.06.2026",
                )

    def test_telegram_worker_imports_document_through_backend_api(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            source_path = Path(tmp_dir) / "orders.xlsx"
            create_orders_workbook(source_path)
            calls = {}
            messages = []
            worker = TelegramWorker.__new__(TelegramWorker)
            worker.token = "telegram-token"
            worker.timeout = 20
            worker.file_timeout = 120
            worker.max_file_size = 20 * 1024 * 1024

            def fake_send(chat_id, text):
                messages.append((chat_id, text))

            def fake_download(document, destination_path):
                shutil.copyfile(source_path, destination_path)

            def fake_backend_post(path, payload):
                calls["path"] = path
                calls["payload"] = payload
                return {
                    "status": "completed",
                    "items_created": len(payload["rows"]),
                    "orders_created": 1,
                    "duplicate_rows": 0,
                    "invalid_rows": 0,
                    "errors": [],
                    "backend_address_updates": 0,
                    "google_sheets_status": "completed",
                    "google_sheets_imported": len(payload["rows"]),
                    "google_sheets_duplicates": 0,
                    "google_sheets_updated": 0,
                    "google_sheets_error": "",
                }

            worker.send_message = fake_send
            worker.download_telegram_document = fake_download
            worker.backend_post = fake_backend_post

            worker.import_telegram_document("123", {"file_name": "orders.xlsx", "file_id": "file-1"}, shipment_date="30.05.2026")

        self.assertEqual(calls["path"], "/api/v1/imports")
        self.assertEqual(calls["payload"]["source"], "telegram")
        self.assertEqual(calls["payload"]["filename"], "orders.xlsx")
        self.assertEqual(len(calls["payload"]["rows"]), 1)
        self.assertEqual(calls["payload"]["rows"][0]["Дата отгрузки"], "30.05.2026")
        self.assertEqual(messages[0][0], "123")
        self.assertIn("Начинаю импорт", messages[0][1])
        self.assertIn("Excel импортирован через Telegram", messages[-1][1])
        self.assertIn("Адреса в backend обновлены: 0", messages[-1][1])
        self.assertIn("Google Sheets: записано 1, повторы 0, адреса обновлены 0", messages[-1][1])

    def test_telegram_worker_send_message_does_not_force_keyboard_by_default(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.token = "telegram-token"
        worker.timeout = 20
        calls = []

        def fake_telegram_request(method, payload):
            calls.append((method, payload))
            return {"ok": True}

        worker.telegram_request = fake_telegram_request

        worker.send_message("123", "hello")

        self.assertEqual(calls[0][0], "sendMessage")
        payload = calls[0][1]
        self.assertEqual(payload["chat_id"], "123")
        self.assertEqual(payload["text"], "hello")
        self.assertNotIn("reply_markup", payload)

    def test_telegram_worker_start_message_uses_command_menu_without_reply_keyboard(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.allowed_chat_ids = set()
        calls = []

        def fake_send(chat_id, text, reply_markup=None):
            calls.append((chat_id, text, reply_markup))

        worker.send_message = fake_send

        worker.handle_update({
            "update_id": 10,
            "message": {
                "chat": {"id": 123},
                "text": "/start",
            },
        })

        self.assertEqual(calls[0][0], "123")
        self.assertIn("Используйте нижнее меню Telegram", calls[0][1])
        self.assertIsNone(calls[0][2])

    def test_telegram_command_menu_contains_only_user_buttons(self):
        commands = telegram_bot_commands()

        self.assertEqual(
            commands,
            [
                {"command": "date", "description": TELEGRAM_BUTTON_SHIPMENT_DATE},
                {"command": "logistics", "description": TELEGRAM_BUTTON_LOGISTICS_REPORT},
                {"command": "kiz_files", "description": TELEGRAM_BUTTON_KIZ_BY_FILES},
                {"command": "status", "description": TELEGRAM_BUTTON_STATUS},
            ],
        )

    def test_telegram_date_buttons_are_user_friendly(self):
        worker = TelegramWorker.__new__(TelegramWorker)

        logistics_keyboard = worker.logistics_date_keyboard(["2026-05-29", "2026-05-30"])
        self.assertEqual(logistics_keyboard["inline_keyboard"][0][0]["text"], "29.05.2026")
        self.assertEqual(logistics_keyboard["inline_keyboard"][0][0]["callback_data"], "logistics:2026-05-29")
        self.assertEqual(logistics_keyboard["inline_keyboard"][1][0]["text"], "30.05.2026")
        self.assertEqual(logistics_keyboard["inline_keyboard"][1][0]["callback_data"], "logistics:2026-05-30")

        self.assertEqual(display_date("2026-05-29"), "29.05.2026")
        self.assertEqual(display_date("29.05.2026"), "29.05.2026")
        self.assertEqual(display_date("не дата"), "не дата")

    def test_telegram_worker_send_document_does_not_force_bottom_reply_keyboard(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.token = "telegram-token"
        worker.file_timeout = 120
        captured = {}
        original_client = telegram_worker_module.httpx.Client

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True, "result": {"message_id": 1}}

        class FakeClient:
            def __init__(self, timeout=None, **_kwargs):
                self.timeout = timeout

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def post(self, url, data=None, files=None):
                captured["url"] = url
                captured["data"] = data
                captured["files"] = files
                return FakeResponse()

        try:
            telegram_worker_module.httpx.Client = FakeClient

            result = worker.send_document("123", b"excel", "orders.xlsx", caption="done")
        finally:
            telegram_worker_module.httpx.Client = original_client

        self.assertEqual(result, {"message_id": 1})
        self.assertEqual(captured["data"]["chat_id"], "123")
        self.assertEqual(captured["data"]["caption"], "done")
        self.assertNotIn("reply_markup", captured["data"])
        self.assertEqual(captured["files"]["document"][0], "orders.xlsx")

    def test_telegram_worker_configures_telegram_command_menu_once(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        calls = []

        def fake_telegram_request(method, payload):
            calls.append((method, payload))
            return True

        worker.telegram_request = fake_telegram_request

        worker.ensure_bot_menu()
        worker.ensure_bot_menu()

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ("setMyCommands", {"commands": telegram_bot_commands()}))
        self.assertEqual(calls[1], ("setChatMenuButton", {"menu_button": {"type": "commands"}}))
        self.assertTrue(worker.bot_menu_ready)
        commands = [item["command"] for item in telegram_bot_commands()]
        self.assertEqual(commands, ["date", "logistics", "kiz_files", "status"])
        self.assertNotIn("health", commands)
        self.assertNotIn("imports", commands)
        self.assertNotIn("logs", commands)

    def test_telegram_worker_handles_bottom_logistics_button(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.allowed_chat_ids = set()
        calls = []

        def fake_show_logistics_dates(chat_id):
            calls.append(chat_id)

        worker.show_logistics_dates = fake_show_logistics_dates

        worker.handle_update({
            "update_id": 10,
            "message": {
                "chat": {"id": 123},
                "text": TELEGRAM_BUTTON_LOGISTICS_REPORT,
            },
        })

        self.assertEqual(calls, ["123"])

    def test_telegram_worker_handles_status_button(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.allowed_chat_ids = set()
        messages = []
        calls = []

        def fake_backend_get(path, params=None):
            calls.append(path)
            if path == "/api/v1/reports/day":
                return {
                    "report_date": "2026-05-31",
                    "totals": {
                        "orders": 8,
                        "completed_orders": 3,
                        "active_orders": 5,
                        "planned_blocks": 20,
                        "scanned_blocks": 7,
                        "scanned_today": 4,
                        "remaining_blocks": 13,
                        "scan_codes": 7,
                        "total_price": 4_800_000,
                    },
                }
            if path == "/api/v1/orders/active":
                return [
                    {
                        "order_date": "2026-06-02",
                        "skladbot_request_number": "WH-R-1",
                        "items": [
                            {"quantity_blocks": 2, "scanned_blocks": 1, "line_total": 480000},
                        ],
                    },
                    {
                        "order_date": "2026-06-03",
                        "skladbot_request_number": "",
                        "skladbot_request_id": "",
                        "items": [
                            {"quantity_blocks": 3, "scanned_blocks": 0, "line_total": 720000},
                        ],
                    },
                ]
            raise AssertionError(path)

        def fake_send(chat_id, text, reply_markup=None):
            messages.append((chat_id, text, reply_markup))

        worker.backend_get = fake_backend_get
        worker.safe_send_message = fake_send

        worker.handle_update({
            "update_id": 12,
            "message": {
                "chat": {"id": 123},
                "text": TELEGRAM_BUTTON_STATUS,
            },
        })

        self.assertEqual(messages[0][0], "123")
        self.assertEqual(calls, ["/api/v1/reports/day", "/api/v1/orders/active"])
        self.assertIn("Статус TakSklad за 31.05.2026", messages[0][1])
        self.assertIn("КИЗов сегодня: 4", messages[0][1])
        self.assertIn("02.06.2026: 1 заказов, 1/2 блоков", messages[0][1])
        self.assertIn("03.06.2026: 1 заказов, 0/3 блоков", messages[0][1])
        self.assertIn("Без номера SkladBot: 1", messages[0][1])

    def test_status_summary_groups_active_orders_by_shipment_date(self):
        summary = summarize_active_orders_by_date([
            {
                "order_date": "2026-06-02",
                "skladbot_request_number": "WH-R-1",
                "items": [
                    {"quantity_blocks": 2, "scanned_blocks": 1, "line_total": 480000},
                    {"quantity_blocks": 1, "scanned_blocks": 1, "line_total": 240000},
                ],
            },
            {
                "order_date": "2026-06-02",
                "skladbot_request_number": "",
                "skladbot_request_id": "",
                "items": [
                    {"quantity_blocks": 3, "scanned_blocks": 0, "line_total": 720000},
                ],
            },
        ])

        self.assertEqual(summary["2026-06-02"]["orders"], 2)
        self.assertEqual(summary["2026-06-02"]["items"], 3)
        self.assertEqual(summary["2026-06-02"]["planned_blocks"], 6)
        self.assertEqual(summary["2026-06-02"]["scanned_blocks"], 2)
        self.assertEqual(summary["2026-06-02"]["remaining_blocks"], 4)
        self.assertEqual(summary["2026-06-02"]["missing_skladbot"], 1)
        self.assertEqual(summary["2026-06-02"]["total_price"], 1_440_000)

    def test_telegram_worker_handles_inline_logistics_callback(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.allowed_chat_ids = set()
        answered = []
        calls = []

        def fake_answer(callback_query_id, text=""):
            answered.append((callback_query_id, text))

        def fake_send_report(chat_id, shipment_date):
            calls.append((chat_id, shipment_date))
            return True

        worker.answer_callback_query = fake_answer
        worker.send_logistics_report = fake_send_report

        worker.handle_update({
            "update_id": 20,
            "callback_query": {
                "id": "cb-1",
                "data": "logistics:2026-05-29",
                "message": {"chat": {"id": 123}},
            },
        })

        self.assertEqual(answered, [("cb-1", "")])
        self.assertEqual(calls, [("123", "2026-05-29")])

    def test_telegram_worker_handles_inline_kiz_file_callback(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.allowed_chat_ids = set()
        answered = []
        calls = []

        def fake_answer(callback_query_id, text=""):
            answered.append((callback_query_id, text))

        def fake_send_by_index(chat_id, text):
            calls.append((chat_id, text))
            return True

        worker.answer_callback_query = fake_answer
        worker.send_kiz_source_file_by_index = fake_send_by_index

        worker.handle_update({
            "update_id": 21,
            "callback_query": {
                "id": "cb-2",
                "data": "kiz_file:2",
                "message": {"chat": {"id": 123}},
            },
        })

        self.assertEqual(answered, [("cb-2", "")])
        self.assertEqual(calls, [("123", "2")])

    def test_telegram_worker_handles_inline_kiz_date_callback(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.allowed_chat_ids = set()
        answered = []
        calls = []

        def fake_answer(callback_query_id, text=""):
            answered.append((callback_query_id, text))

        def fake_send_date(chat_id, shipment_date):
            calls.append((chat_id, shipment_date))
            return True

        worker.answer_callback_query = fake_answer
        worker.send_kiz_date_report = fake_send_date

        worker.handle_update({
            "update_id": 22,
            "callback_query": {
                "id": "cb-3",
                "data": "kiz_date:2026-05-30",
                "message": {"chat": {"id": 123}},
            },
        })

        self.assertEqual(answered, [("cb-3", "")])
        self.assertEqual(calls, [("123", "2026-05-30")])

    def test_telegram_worker_reports_logistics_backend_error_to_user(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        messages = []

        def fake_backend_get(path, params=None):
            return ["2026-05-29"]

        def fake_backend_get_bytes(path, params=None):
            request = httpx.Request("GET", "http://backend-api/api/v1/logistics/report")
            response = httpx.Response(
                409,
                json={"detail": "Missing coordinates for logistics report: Client One"},
                request=request,
            )
            raise httpx.HTTPStatusError("backend rejected report", request=request, response=response)

        def fake_send_message(chat_id, text, reply_markup=None):
            messages.append((chat_id, text))

        worker.backend_get = fake_backend_get
        worker.backend_get_bytes = fake_backend_get_bytes
        worker.safe_send_message = fake_send_message

        worker.show_logistics_dates("123")

        self.assertEqual(messages[0][0], "123")
        self.assertIn("Не удалось выгрузить отчёт логистики за 29.05.2026", messages[0][1])
        self.assertIn("Missing coordinates for logistics report: Client One", messages[0][1])

    def test_telegram_worker_saves_shipment_date_from_message(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.allowed_chat_ids = set()
        saved = []
        messages = []

        def fake_set_chat_shipment_date(chat_id, shipment_date):
            saved.append((chat_id, shipment_date))

        def fake_send(chat_id, text):
            messages.append((chat_id, text))

        worker.set_chat_shipment_date = fake_set_chat_shipment_date
        worker.send_message = fake_send
        worker.confirm_waiting_telegram_import_shipment_date = lambda chat_id, shipment_date: False

        worker.handle_update({
            "update_id": 11,
            "message": {
                "chat": {"id": 123},
                "text": "29.05.2026",
            },
        })

        self.assertEqual(saved, [("123", "29.05.2026")])
        self.assertIn("Дата сохранена: 29.05.2026", messages[0][1])
        self.assertIn("бот всё равно спросит дату", messages[0][1])

    def test_telegram_worker_enqueues_excel_document_from_message(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.allowed_chat_ids = set()
        calls = []

        def fake_enqueue(chat_id, document, update_id=None, shipment_date=""):
            calls.append((chat_id, document, update_id, shipment_date))
            return True

        worker.enqueue_telegram_document = fake_enqueue

        worker.handle_update({
            "update_id": 77,
            "message": {
                "chat": {"id": 123},
                "document": {"file_name": "orders.xlsx", "file_id": "file-1"},
            },
        })

        self.assertEqual(calls, [("123", {"file_name": "orders.xlsx", "file_id": "file-1"}, 77, "")])

    def test_telegram_worker_enqueues_document_waiting_for_manual_shipment_date(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        original_session_local = telegram_worker_module.SessionLocal
        try:
            messages = []
            worker = TelegramWorker.__new__(TelegramWorker)
            worker.safe_send_message = lambda chat_id, text, reply_markup=None: messages.append((chat_id, text, reply_markup))
            telegram_worker_module.SessionLocal = SessionLocal

            result = worker.enqueue_telegram_document(
                "123",
                {"file_name": "orders.xlsx", "file_id": "file-1"},
                update_id=77,
            )

            with SessionLocal() as db:
                event = db.execute(select(PendingEvent)).scalar_one()
                payload = event.payload

            self.assertTrue(result)
            self.assertEqual(event.status, telegram_worker_module.TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS)
            self.assertEqual(payload["shipment_date"], "")
            self.assertEqual(payload["shipment_date_source"], "")
            self.assertIn("Укажите дату отгрузки", messages[0][1])
            self.assertIn("ДД.ММ.ГГГГ", messages[0][1])
        finally:
            telegram_worker_module.SessionLocal = original_session_local
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_telegram_worker_manual_date_moves_waiting_import_to_pending(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        original_session_local = telegram_worker_module.SessionLocal
        try:
            with SessionLocal() as db:
                event = PendingEvent(
                    event_type=telegram_worker_module.TELEGRAM_EXCEL_IMPORT_EVENT_TYPE,
                    status=telegram_worker_module.TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS,
                    payload={
                        "chat_id": "123",
                        "file_name": "orders.xlsx",
                        "document": {"file_name": "orders.xlsx", "file_id": "file-1"},
                        "shipment_date": "",
                    },
                )
                db.add(event)
                db.commit()

            messages = []
            processed = []
            worker = TelegramWorker.__new__(TelegramWorker)
            worker.allowed_chat_ids = set()
            worker.safe_send_message = lambda chat_id, text, reply_markup=None: messages.append((chat_id, text, reply_markup))
            worker.process_queued_telegram_imports = lambda: processed.append(True)
            telegram_worker_module.SessionLocal = SessionLocal

            worker.handle_update({
                "update_id": 78,
                "message": {
                    "chat": {"id": 123},
                    "text": "09.06.2026",
                },
            })

            with SessionLocal() as db:
                event = db.execute(select(PendingEvent)).scalar_one()
                payload = event.payload

            self.assertEqual(event.status, "pending")
            self.assertEqual(event.last_error, "")
            self.assertEqual(payload["shipment_date"], "09.06.2026")
            self.assertEqual(payload["shipment_date_source"], "telegram_manual_input")
            self.assertEqual(processed, [True])
            self.assertIn("Дата принята", messages[0][1])
        finally:
            telegram_worker_module.SessionLocal = original_session_local
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_telegram_worker_repeats_prompt_for_invalid_manual_import_date(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        original_session_local = telegram_worker_module.SessionLocal
        try:
            with SessionLocal() as db:
                event = PendingEvent(
                    event_type=telegram_worker_module.TELEGRAM_EXCEL_IMPORT_EVENT_TYPE,
                    status=telegram_worker_module.TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS,
                    payload={
                        "chat_id": "123",
                        "file_name": "orders.xlsx",
                        "document": {"file_name": "orders.xlsx", "file_id": "file-1"},
                        "shipment_date": "",
                    },
                )
                db.add(event)
                db.commit()

            messages = []
            processed = []
            worker = TelegramWorker.__new__(TelegramWorker)
            worker.allowed_chat_ids = set()
            worker.safe_send_message = lambda chat_id, text, reply_markup=None: messages.append((chat_id, text, reply_markup))
            worker.process_queued_telegram_imports = lambda: processed.append(True)
            telegram_worker_module.SessionLocal = SessionLocal

            worker.handle_update({
                "update_id": 79,
                "message": {
                    "chat": {"id": 123},
                    "text": "завтра",
                },
            })

            with SessionLocal() as db:
                event = db.execute(select(PendingEvent)).scalar_one()

            self.assertEqual(event.status, telegram_worker_module.TELEGRAM_EXCEL_IMPORT_WAITING_SHIPMENT_DATE_STATUS)
            self.assertEqual(processed, [])
            self.assertIn("Ожидаю дату отгрузки", messages[0][1])
            self.assertIn("ДД.ММ.ГГГГ", messages[0][1])
        finally:
            telegram_worker_module.SessionLocal = original_session_local
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_telegram_worker_processes_multiple_queued_imports(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        events = [
            {"id": "event-1", "payload": {"chat_id": "123", "document": {"file_name": "a.xlsx"}}},
            {"id": "event-2", "payload": {"chat_id": "123", "document": {"file_name": "b.xlsx"}}},
        ]
        imported = []
        finished = []

        def fake_take_next():
            return events.pop(0) if events else None

        def fake_import(chat_id, document, shipment_date="", event_id=None):
            imported.append((chat_id, document["file_name"], shipment_date))
            return True, ""

        def fake_finish(event_id, success, error=""):
            finished.append((event_id, success, error))

        worker.take_next_telegram_import_event = fake_take_next
        worker.import_telegram_document = fake_import
        worker.finish_telegram_import_event = fake_finish

        processed = worker.process_queued_telegram_imports()

        self.assertEqual(processed, 2)
        self.assertEqual(imported, [("123", "a.xlsx", ""), ("123", "b.xlsx", "")])
        self.assertEqual(finished, [("event-1", True, ""), ("event-2", True, "")])

    def test_telegram_worker_manual_shipment_date_overrides_excel_date(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            source_path = Path(tmp_dir) / "orders.xlsx"
            create_conflicting_date_workbook(source_path)
            worker = TelegramWorker.__new__(TelegramWorker)
            worker.token = "telegram-token"
            worker.timeout = 20
            worker.file_timeout = 120
            worker.max_file_size = 20 * 1024 * 1024
            messages = []
            calls = {}

            def fake_download(document, destination_path):
                shutil.copyfile(source_path, destination_path)

            def fake_backend_post(path, payload):
                calls["path"] = path
                calls["payload"] = payload
                return {
                    "status": "completed",
                    "items_created": len(payload["rows"]),
                    "orders_created": 1,
                    "duplicate_rows": 0,
                    "invalid_rows": 0,
                    "errors": [],
                    "backend_address_updates": 0,
                    "google_sheets_status": "disabled",
                }

            def fake_send(chat_id, text, reply_markup=None):
                messages.append((chat_id, text, reply_markup))

            worker.download_telegram_document = fake_download
            worker.backend_post = fake_backend_post
            worker.safe_send_message = fake_send

            result = worker.import_telegram_document(
                "123",
                {"file_name": "orders.xlsx", "file_id": "file-1"},
                shipment_date="08.06.2026",
                event_id="11111111-1111-1111-1111-111111111111",
            )

        self.assertEqual(result, (True, ""))
        self.assertEqual(calls["path"], "/api/v1/imports")
        self.assertEqual(calls["payload"]["rows"][0]["Дата отгрузки"], "08.06.2026")
        self.assertIn("Дата отгрузки: 08.06.2026", messages[-1][1])

    def test_telegram_worker_does_not_finish_event_while_waiting_for_date_choice(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        events = [{
            "id": "11111111-1111-1111-1111-111111111111",
            "payload": {
                "chat_id": "123",
                "document": {"file_name": "orders.xlsx"},
                "shipment_date": "08.06.2026",
            },
        }]
        finished = []

        def fake_take_next():
            return events.pop(0) if events else None

        def fake_import(chat_id, document, shipment_date="", event_id=None):
            self.assertEqual(event_id, "11111111-1111-1111-1111-111111111111")
            return None, "date_choice_required"

        worker.take_next_telegram_import_event = fake_take_next
        worker.import_telegram_document = fake_import
        worker.finish_telegram_import_event = lambda *args: finished.append(args)

        processed = worker.process_queued_telegram_imports()

        self.assertEqual(processed, 1)
        self.assertEqual(finished, [])

    def test_telegram_worker_handles_date_choice_callbacks(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.allowed_chat_ids = set()
        answered = []
        confirmed = []
        cancelled = []

        worker.answer_callback_query = lambda callback_id, text="": answered.append((callback_id, text))
        worker.confirm_telegram_import_excel_date = lambda chat_id, event_id: confirmed.append((chat_id, event_id))
        worker.cancel_telegram_import_date_choice = lambda chat_id, event_id: cancelled.append((chat_id, event_id))

        worker.handle_update({
            "update_id": 78,
            "callback_query": {
                "id": "cb-1",
                "data": "excel_date:use_excel:11111111-1111-1111-1111-111111111111",
                "message": {"chat": {"id": 123}},
            },
        })
        worker.handle_update({
            "update_id": 79,
            "callback_query": {
                "id": "cb-2",
                "data": "excel_date:cancel:22222222-2222-2222-2222-222222222222",
                "message": {"chat": {"id": 123}},
            },
        })

        self.assertEqual(answered, [("cb-1", ""), ("cb-2", "")])
        self.assertEqual(confirmed, [("123", "11111111-1111-1111-1111-111111111111")])
        self.assertEqual(cancelled, [("123", "22222222-2222-2222-2222-222222222222")])

    def test_telegram_worker_confirm_excel_date_requeues_waiting_event(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        original_session_local = telegram_worker_module.SessionLocal
        try:
            with SessionLocal() as db:
                event = PendingEvent(
                    event_type=telegram_worker_module.TELEGRAM_EXCEL_IMPORT_EVENT_TYPE,
                    status=telegram_worker_module.TELEGRAM_EXCEL_IMPORT_WAITING_DATE_CHOICE_STATUS,
                    payload={
                        "chat_id": "123",
                        "file_name": "orders.xlsx",
                        "document": {"file_name": "orders.xlsx"},
                        "shipment_date": "08.06.2026",
                        "date_conflict": {"telegram_date": "08.06.2026", "excel_date": "09.06.2026"},
                    },
                )
                db.add(event)
                db.commit()
                event_id = str(event.id)

            messages = []
            processed = []
            worker = TelegramWorker.__new__(TelegramWorker)
            worker.safe_send_message = lambda chat_id, text, reply_markup=None: messages.append((chat_id, text, reply_markup))
            worker.process_queued_telegram_imports = lambda: processed.append(True)
            telegram_worker_module.SessionLocal = SessionLocal

            result = worker.confirm_telegram_import_excel_date("123", event_id)

            with SessionLocal() as db:
                event = db.execute(select(PendingEvent)).scalar_one()
                payload = event.payload

            self.assertTrue(result)
            self.assertEqual(event.status, "pending")
            self.assertEqual(event.last_error, "")
            self.assertEqual(payload["shipment_date"], "")
            self.assertEqual(payload["date_choice_resolution"]["action"], "use_excel")
            self.assertEqual(processed, [True])
            self.assertIn("Импорт будет выполнен по дате из Excel", messages[0][1])
        finally:
            telegram_worker_module.SessionLocal = original_session_local
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_telegram_worker_cancel_date_choice_does_not_requeue_event(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        original_session_local = telegram_worker_module.SessionLocal
        try:
            with SessionLocal() as db:
                event = PendingEvent(
                    event_type=telegram_worker_module.TELEGRAM_EXCEL_IMPORT_EVENT_TYPE,
                    status=telegram_worker_module.TELEGRAM_EXCEL_IMPORT_WAITING_DATE_CHOICE_STATUS,
                    payload={
                        "chat_id": "123",
                        "file_name": "orders.xlsx",
                        "document": {"file_name": "orders.xlsx"},
                        "shipment_date": "08.06.2026",
                        "date_conflict": {"telegram_date": "08.06.2026", "excel_date": "09.06.2026"},
                    },
                )
                db.add(event)
                db.commit()
                event_id = str(event.id)

            messages = []
            worker = TelegramWorker.__new__(TelegramWorker)
            worker.safe_send_message = lambda chat_id, text, reply_markup=None: messages.append((chat_id, text, reply_markup))
            telegram_worker_module.SessionLocal = SessionLocal

            result = worker.cancel_telegram_import_date_choice("123", event_id)

            with SessionLocal() as db:
                event = db.execute(select(PendingEvent)).scalar_one()
                payload = event.payload

            self.assertTrue(result)
            self.assertEqual(event.status, "cancelled")
            self.assertEqual(event.last_error, "cancelled_by_user")
            self.assertEqual(payload["shipment_date"], "08.06.2026")
            self.assertEqual(payload["date_choice_resolution"]["action"], "cancel")
            self.assertIn("Импорт отменён", messages[0][1])
        finally:
            telegram_worker_module.SessionLocal = original_session_local
            Base.metadata.drop_all(engine)
            engine.dispose()

    def test_telegram_worker_shows_kiz_export_menu_modes(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        messages = []

        def fake_send(chat_id, text, reply_markup=None):
            messages.append((chat_id, text, reply_markup))

        worker.safe_send_message = fake_send

        worker.show_kiz_export_menu("123")

        self.assertIn("Как выгрузить КИЗы", messages[0][1])
        keyboard = messages[0][2]["inline_keyboard"]
        self.assertEqual(keyboard[0][0]["callback_data"], "kiz_mode:dates")
        self.assertEqual(keyboard[1][0]["callback_data"], "kiz_mode:files")

    def test_telegram_worker_shows_kiz_dates_as_display_dates(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        messages = []
        states = []

        def fake_backend_get(path, params=None):
            self.assertEqual(path, "/api/v1/reports/kiz/dates")
            return [
                {
                    "date": "2026-05-29",
                    "planned_blocks": 3,
                    "scanned_blocks": 3,
                },
                {
                    "date": "2026-05-30",
                    "planned_blocks": 2,
                    "scanned_blocks": 2,
                },
            ]

        def fake_get_state(chat_id):
            return {}

        def fake_save_state(chat_id, payload):
            states.append((chat_id, payload))

        def fake_send(chat_id, text, reply_markup=None):
            messages.append((chat_id, text, reply_markup))

        worker.backend_get = fake_backend_get
        worker.get_chat_state = fake_get_state
        worker.save_chat_state = fake_save_state
        worker.safe_send_message = fake_send

        worker.show_kiz_dates("123")

        self.assertIn("29.05.2026", messages[0][1])
        self.assertIn("30.05.2026", messages[0][1])
        self.assertEqual(messages[0][2]["inline_keyboard"][0][0]["callback_data"], "kiz_date:2026-05-29")
        self.assertEqual(states[0][1]["kiz_dates"][0]["date"], "2026-05-29")

    def test_telegram_worker_shows_kiz_source_files_with_progress(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        messages = []
        states = []

        def fake_backend_get(path, params=None):
            self.assertEqual(path, "/api/v1/reports/kiz/source-files")
            return [
                {
                    "source_key": "import:done",
                    "source_file": "done.xlsx",
                    "planned_blocks": 3,
                    "scanned_blocks": 3,
                    "completed": True,
                    "dates": ["2026-06-04"],
                },
                {
                    "source_key": "import:open",
                    "source_file": "open.xlsx",
                    "planned_blocks": 5,
                    "scanned_blocks": 2,
                    "completed": False,
                    "dates": ["2026-06-08"],
                },
            ]

        def fake_get_state(chat_id):
            return {}

        def fake_save_state(chat_id, payload):
            states.append((chat_id, payload))

        def fake_send(chat_id, text, reply_markup=None):
            messages.append((chat_id, text, reply_markup))

        worker.backend_get = fake_backend_get
        worker.get_chat_state = fake_get_state
        worker.save_chat_state = fake_save_state
        worker.safe_send_message = fake_send

        worker.show_kiz_source_files("123")

        self.assertIn("done.xlsx", messages[0][1])
        self.assertIn("3/3 блоков", messages[0][1])
        self.assertIn("open.xlsx", messages[0][1])
        self.assertIn("2/5 блоков", messages[0][1])
        self.assertEqual(messages[0][2]["inline_keyboard"][0][0]["callback_data"], "kiz_file:1")
        self.assertEqual(len(messages[0][2]["inline_keyboard"]), 1)
        self.assertEqual(states[0][1]["kiz_files"][0]["source_key"], "import:done")
        self.assertFalse(states[0][1]["kiz_files"][1]["completed"])

    def test_telegram_worker_downloads_kiz_source_file_by_import_key(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        params_seen = []
        sent = []

        def fake_backend_get_bytes(path, params=None):
            params_seen.append((path, params))
            return b"xlsx", {"X-TakSklad-Filename": "TakSklad_KIZ.xlsx"}

        def fake_send_document(chat_id, content, filename, caption=""):
            sent.append((chat_id, content, filename, caption))

        worker.backend_get_bytes = fake_backend_get_bytes
        worker.safe_send_document = fake_send_document

        result = worker.send_kiz_source_file_report("123", "same-name.xlsx", "import:first")

        self.assertTrue(result)
        self.assertEqual(params_seen[0], (
            "/api/v1/reports/kiz/source-file",
            {"source_file": "same-name.xlsx", "source_key": "import:first"},
        ))
        self.assertEqual(sent[0][2], "TakSklad_KIZ.xlsx")

    def test_telegram_worker_keeps_kiz_source_key_when_file_selected_by_index(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        calls = []

        def fake_get_state(chat_id):
            return {
                "kiz_files": [
                    {"index": 1, "source_file": "same-name.xlsx", "source_key": "import:first"},
                ],
            }

        def fake_send_report(chat_id, source_file, source_key=""):
            calls.append((chat_id, source_file, source_key))
            return True

        worker.get_chat_state = fake_get_state
        worker.send_kiz_source_file_report = fake_send_report

        result = worker.send_kiz_source_file_by_index("123", "1")

        self.assertTrue(result)
        self.assertEqual(calls, [("123", "same-name.xlsx", "import:first")])

    def test_telegram_worker_keeps_polling_after_single_update_error(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.token = "telegram-token"
        worker.allowed_chat_ids = set()
        worker.timeout = 20
        worker.poll_timeout = 1
        worker.offset = 0
        worker.bot_menu_ready = True
        saved_offsets = []
        errors = []
        enqueued = []

        updates = [
            {
                "update_id": 101,
                "message": {
                    "chat": {"id": 123},
                    "text": TELEGRAM_BUTTON_LOGISTICS_REPORT,
                },
            },
            {
                "update_id": 102,
                "message": {
                    "chat": {"id": 123},
                    "document": {"file_name": "orders.xlsx", "file_id": "file-1"},
                },
            },
        ]

        def fake_telegram_request(method, payload=None, timeout=None):
            if method == "getUpdates":
                self.assertEqual(payload["allowed_updates"], ["message", "callback_query"])
                return updates
            raise AssertionError(method)

        def fake_show_logistics_dates(chat_id):
            raise RuntimeError("backend temporary unavailable")

        def fake_error(chat_id, text, reply_markup=None):
            errors.append((chat_id, text))

        def fake_enqueue(chat_id, document, update_id=None, shipment_date=""):
            enqueued.append((chat_id, document, update_id, shipment_date))
            return True

        def fake_save_offset():
            saved_offsets.append(worker.offset)

        worker.telegram_request = fake_telegram_request
        worker.show_logistics_dates = fake_show_logistics_dates
        worker.safe_send_message = fake_error
        worker.enqueue_telegram_document = fake_enqueue
        worker.save_offset = fake_save_offset
        worker.process_queued_telegram_imports = lambda: 0

        worker.poll_once()

        self.assertEqual(worker.offset, 102)
        self.assertEqual(saved_offsets, [102])
        self.assertEqual(errors[0][0], "123")
        self.assertIn("Не удалось выполнить действие Telegram", errors[0][1])
        self.assertEqual(enqueued, [(
            "123",
            {"file_name": "orders.xlsx", "file_id": "file-1"},
            102,
            "",
        )])

    def test_telegram_worker_runs_scheduled_jobs_after_getupdates_conflict(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.token = "telegram-token"
        worker.timeout = 20
        worker.poll_timeout = 1
        worker.offset = 0
        calls = []

        def fake_telegram_request(method, payload=None, timeout=None):
            if method == "getUpdates":
                raise RuntimeError("Telegram API request failed: getUpdates: HTTP 409 Conflict")
            raise AssertionError(method)

        worker.ensure_bot_menu = lambda: None
        worker.telegram_request = fake_telegram_request
        worker.process_queued_telegram_imports = lambda: calls.append("imports") or 0
        worker.send_due_skladbot_daily_reports = lambda: calls.append("daily") or 0

        worker.poll_once()

        self.assertEqual(calls, ["imports", "daily"])
        self.assertEqual(worker.offset, 0)

    def test_telegram_worker_handles_hidden_logs_command(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.allowed_chat_ids = set()
        worker.admin_chat_ids = set()
        sent = []

        def fake_backend_get_bytes(path, params=None):
            return b"diagnostics", {"X-TakSklad-Filename": "TakSklad_backend_diagnostics.txt"}

        def fake_send_document(chat_id, content, filename, caption=""):
            sent.append((chat_id, content, filename, caption))

        worker.backend_get_bytes = fake_backend_get_bytes
        worker.safe_send_document = fake_send_document

        worker.handle_update({
            "update_id": 88,
            "message": {
                "chat": {"id": 123},
                "text": "/logs",
            },
        })

        self.assertEqual(sent[0][0], "123")
        self.assertEqual(sent[0][1], b"diagnostics")
        self.assertEqual(sent[0][2], "TakSklad_backend_diagnostics.txt")
        self.assertIn("backend", sent[0][3])

    def test_telegram_worker_restricts_hidden_admin_commands_when_admin_chat_ids_set(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.allowed_chat_ids = {"123", "999"}
        worker.admin_chat_ids = {"999"}
        messages = []
        backend_calls = []

        def fake_send_message(chat_id, text, reply_markup=None):
            messages.append((chat_id, text))

        def fake_backend_get(path, params=None):
            backend_calls.append(path)
            return {"status": "ok", "version": "test"}

        worker.send_message = fake_send_message
        worker.backend_get = fake_backend_get

        worker.handle_update({
            "update_id": 89,
            "message": {"chat": {"id": 123}, "text": "/health"},
        })

        self.assertEqual(backend_calls, [])
        self.assertEqual(messages[0][0], "123")
        self.assertIn("только администратору", messages[0][1])

        worker.handle_update({
            "update_id": 90,
            "message": {"chat": {"id": 999}, "text": "/health"},
        })

        self.assertEqual(backend_calls, ["/health"])
        self.assertIn("Backend: ok / test", messages[-1][1])

    def test_excel_file_to_import_payload_reads_smartup_coordinates_and_prices(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "smartup.xlsx"
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = "Конструктор отчетов"
            for _ in range(4):
                sheet.append([])
            sheet.append([
                "Торговый представитель",
                "Клиент",
                "Координаты клиента",
                "ТМЦ",
                "Тип оплаты",
                "Статус",
                "Количество заказа",
                "Сумма с переоценкой",
            ])
            sheet.append(["Rep One", "Client One", "41.31, 69.27", "Chapman Brown OP 20", "Терминал", "", 200, 4_800_000])
            workbook.save(path)

            payload = excel_file_to_import_payload(path, file_name=path.name, source="telegram", shipment_date="29.05.2026")

        self.assertEqual(len(payload["rows"]), 1)
        row = payload["rows"][0]
        self.assertEqual(row["Дата отгрузки"], "29.05.2026")
        self.assertEqual(row["Координаты"], "41.31, 69.27")
        self.assertEqual(row["Кол-во ШТ"], 200)
        self.assertEqual(row["Кол-во блок"], 20)
        self.assertEqual(row["Сумма позиции"], 4_800_000)
        self.assertEqual(row["Цена за блок"], 240000)

    def test_excel_file_to_import_payload_removes_country_prefix_from_address(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "address.xlsx"
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = "Заявки"
            sheet.append([
                "Клиент",
                "Тип оплаты",
                "Товары",
                "Кол-во ШТ",
                "Адрес",
                "Торговый представитель",
            ])
            sheet.append([
                "Client One",
                "Терминал",
                "Chapman Brown OP 20",
                20,
                "Uzbekistan, Tashkent, Chilanzar 10",
                "Rep One",
            ])
            sheet.append([
                "Client Two",
                "Перечисление",
                "Chapman Red SSL 20",
                10,
                "O'zbekiston, Toshkent, Yunusobod 4",
                "Rep Two",
            ])
            workbook.save(path)

            payload = excel_file_to_import_payload(path, file_name=path.name, source="telegram", shipment_date="29.05.2026")

        self.assertEqual(payload["rows"][0]["Адрес"], "Tashkent, Chilanzar 10")
        self.assertEqual(payload["rows"][1]["Адрес"], "Toshkent, Yunusobod 4")

    def test_excel_file_to_import_payload_geocodes_address_when_coordinates_missing(self):
        original_geocoder = excel_importer.geocode_address_yandex
        calls = []
        try:
            def fake_geocoder(address, cache=None):
                calls.append(address)
                return "41.311081, 69.240562", ""

            excel_importer.geocode_address_yandex = fake_geocoder

            with tempfile.TemporaryDirectory() as tmp_dir:
                path = Path(tmp_dir) / "address_without_coordinates.xlsx"
                workbook = openpyxl.Workbook()
                sheet = workbook.active
                sheet.title = "Заявки"
                sheet.append([
                    "Клиент",
                    "Тип оплаты",
                    "Товары",
                    "Кол-во ШТ",
                    "Адрес",
                    "Торговый представитель",
                ])
                sheet.append([
                    "Client One",
                    "Терминал",
                    "Chapman Brown OP 20",
                    20,
                    "Uzbekistan, Tashkent, Chilanzar 10",
                    "Rep One",
                ])
                workbook.save(path)

                payload = excel_importer.excel_file_to_import_payload(
                    path,
                    file_name=path.name,
                    source="telegram",
                    shipment_date="29.05.2026",
                )
        finally:
            excel_importer.geocode_address_yandex = original_geocoder

        self.assertEqual(calls, ["Tashkent, Chilanzar 10"])
        self.assertEqual(payload["rows"][0]["Координаты"], "41.311081, 69.240562")
        self.assertEqual(payload["meta"]["geocoded_count"], 1)
        self.assertEqual(payload["meta"]["geocode_failed_count"], 0)

    def test_excel_file_to_import_payload_marks_missing_address_as_pickup(self):
        original_geocoder = excel_importer.geocode_address_yandex
        calls = []
        try:
            def fake_geocoder(address, cache=None):
                calls.append(address)
                return "41.311081, 69.240562", ""

            excel_importer.geocode_address_yandex = fake_geocoder

            with tempfile.TemporaryDirectory() as tmp_dir:
                path = Path(tmp_dir) / "pickup_without_address.xlsx"
                workbook = openpyxl.Workbook()
                sheet = workbook.active
                sheet.title = "Заявки"
                sheet.append([
                    "Клиент",
                    "Тип оплаты",
                    "Товары",
                    "Кол-во ШТ",
                    "Адрес",
                    "Торговый представитель",
                ])
                sheet.append([
                    "Pickup Client",
                    "Терминал",
                    "Chapman Brown OP 20",
                    20,
                    "",
                    "Rep One",
                ])
                workbook.save(path)

                payload = excel_importer.excel_file_to_import_payload(
                    path,
                    file_name=path.name,
                    source="telegram",
                    shipment_date="29.05.2026",
                )
        finally:
            excel_importer.geocode_address_yandex = original_geocoder

        self.assertEqual(calls, [])
        self.assertEqual(payload["rows"][0]["Адрес"], "Самовывоз со склада")
        self.assertEqual(payload["rows"][0]["Координаты"], "")
        self.assertEqual(payload["meta"]["geocoded_count"], 0)
        self.assertEqual(payload["meta"]["geocode_failed_count"], 0)

    def test_excel_file_to_import_payload_reverse_geocodes_address_when_only_coordinates_present(self):
        original_reverse_geocoder = excel_importer.reverse_geocode_yandex
        calls = []
        try:
            def fake_reverse_geocoder(coordinates, cache=None):
                calls.append(coordinates)
                return "Ташкент, Чиланзарский район, 10", ""

            excel_importer.reverse_geocode_yandex = fake_reverse_geocoder

            with tempfile.TemporaryDirectory() as tmp_dir:
                path = Path(tmp_dir) / "coordinates_without_address.xlsx"
                workbook = openpyxl.Workbook()
                sheet = workbook.active
                sheet.title = "Заявки"
                sheet.append([
                    "Клиент",
                    "Тип оплаты",
                    "Товары",
                    "Кол-во ШТ",
                    "Адрес доставки",
                    "Координаты",
                    "Торговый представитель",
                ])
                sheet.append([
                    "Client One",
                    "Терминал",
                    "Chapman Brown OP 20",
                    20,
                    "",
                    "41.311081,69.240562,15",
                    "Rep One",
                ])
                workbook.save(path)

                payload = excel_importer.excel_file_to_import_payload(
                    path,
                    file_name=path.name,
                    source="telegram",
                    shipment_date="29.05.2026",
                )
        finally:
            excel_importer.reverse_geocode_yandex = original_reverse_geocoder

        self.assertEqual(calls, ["41.311081, 69.240562"])
        self.assertEqual(payload["rows"][0]["Адрес"], "Ташкент, Чиланзарский район, 10")
        self.assertEqual(payload["rows"][0]["Координаты"], "41.311081, 69.240562")
        self.assertEqual(payload["meta"]["geocoded_count"], 1)
        self.assertEqual(payload["meta"]["geocode_failed_count"], 0)

    def test_excel_file_to_import_payload_reverse_geocodes_when_address_is_not_found_placeholder(self):
        original_reverse_geocoder = excel_importer.reverse_geocode_yandex
        calls = []
        try:
            def fake_reverse_geocoder(coordinates, cache=None):
                calls.append(coordinates)
                return "Ташкент, Яккасарайский район", ""

            excel_importer.reverse_geocode_yandex = fake_reverse_geocoder

            with tempfile.TemporaryDirectory() as tmp_dir:
                path = Path(tmp_dir) / "coordinates_with_placeholder_address.xlsx"
                workbook = openpyxl.Workbook()
                sheet = workbook.active
                sheet.title = "Заявки"
                sheet.append([
                    "Клиент",
                    "Тип оплаты",
                    "Товары",
                    "Кол-во ШТ",
                    "Адрес",
                    "Координаты",
                    "Торговый представитель",
                ])
                sheet.append([
                    "Client One",
                    "Терминал",
                    "Chapman Brown OP 20",
                    20,
                    "Адрес не найден",
                    "41.311081,69.240562",
                    "Rep One",
                ])
                workbook.save(path)

                payload = excel_importer.excel_file_to_import_payload(
                    path,
                    file_name=path.name,
                    source="telegram",
                    shipment_date="29.05.2026",
                )
        finally:
            excel_importer.reverse_geocode_yandex = original_reverse_geocoder

        self.assertEqual(calls, ["41.311081, 69.240562"])
        self.assertEqual(payload["rows"][0]["Адрес"], "Ташкент, Яккасарайский район")
        self.assertEqual(payload["rows"][0]["Координаты"], "41.311081, 69.240562")
        self.assertEqual(payload["meta"]["geocoded_count"], 1)
        self.assertEqual(payload["meta"]["geocode_failed_count"], 0)

    def test_excel_file_to_import_payload_reads_full_coordinates_from_repeated_smartup_columns(self):
        original_reverse_geocoder = excel_importer.reverse_geocode_yandex
        calls = []
        try:
            def fake_reverse_geocoder(coordinates, cache=None):
                calls.append(coordinates)
                return "Ташкент, Юнусабадский район", ""

            excel_importer.reverse_geocode_yandex = fake_reverse_geocoder

            with tempfile.TemporaryDirectory() as tmp_dir:
                path = Path(tmp_dir) / "smartup_repeated_coordinates.xlsx"
                workbook = openpyxl.Workbook()
                sheet = workbook.active
                sheet.title = "Конструктор отчетов"
                sheet.append([
                    "Торговый представитель",
                    "Клиент",
                    "Координаты клиента",
                    "Координаты клиента",
                    "Координаты клиента",
                    "ТМЦ",
                    "Тип оплаты",
                    "Статус",
                    "Количество заказа",
                    "Сумма с переоценкой",
                    "Дата отгрузки",
                ])
                sheet.append([
                    "Rep One",
                    "Client One",
                    "41.325658539017745",
                    "69.23166364431383",
                    "41.325658539017745,69.23166364431383",
                    "Chapman Brown OP 20",
                    "Перечисление",
                    "В обработке",
                    150,
                    3_600_000,
                    "2026-06-03",
                ])
                workbook.save(path)

                payload = excel_importer.excel_file_to_import_payload(
                    path,
                    file_name=path.name,
                    source="telegram",
                    shipment_date="03.06.2026",
                )
        finally:
            excel_importer.reverse_geocode_yandex = original_reverse_geocoder

        self.assertEqual(calls, ["41.325658539017745, 69.23166364431383"])
        self.assertEqual(payload["rows"][0]["Адрес"], "Ташкент, Юнусабадский район")
        self.assertEqual(payload["rows"][0]["Координаты"], "41.325658539017745, 69.23166364431383")
        self.assertEqual(payload["meta"]["geocoded_count"], 1)
        self.assertEqual(payload["meta"]["geocode_failed_count"], 0)

    def test_excel_file_to_import_payload_combines_split_smartup_coordinates_without_full_column_header(self):
        original_reverse_geocoder = excel_importer.reverse_geocode_yandex
        calls = []
        try:
            def fake_reverse_geocoder(coordinates, cache=None):
                calls.append(coordinates)
                return "Ташкент, Мирзо-Улугбекский район", ""

            excel_importer.reverse_geocode_yandex = fake_reverse_geocoder

            with tempfile.TemporaryDirectory() as tmp_dir:
                path = Path(tmp_dir) / "smartup_split_coordinates.xlsx"
                workbook = openpyxl.Workbook()
                sheet = workbook.active
                sheet.title = "Конструктор отчетов"
                sheet.append([
                    "Торговый представитель",
                    "Клиент",
                    "Координаты клиента",
                    "",
                    "",
                    "ТМЦ",
                    "Тип оплаты",
                    "Статус",
                    "Количество заказа",
                    "Сумма с переоценкой",
                    "Дата отгрузки",
                ])
                sheet.append([
                    "Rep One",
                    "Client One",
                    "41.363127",
                    "69.287982",
                    "",
                    "Chapman Brown OP 20",
                    "Перечисление",
                    "В обработке",
                    40,
                    960_000,
                    "2026-06-03",
                ])
                workbook.save(path)

                payload = excel_importer.excel_file_to_import_payload(
                    path,
                    file_name=path.name,
                    source="telegram",
                    shipment_date="03.06.2026",
                )
        finally:
            excel_importer.reverse_geocode_yandex = original_reverse_geocoder

        self.assertEqual(calls, ["41.363127, 69.287982"])
        self.assertEqual(payload["rows"][0]["Адрес"], "Ташкент, Мирзо-Улугбекский район")
        self.assertEqual(payload["rows"][0]["Координаты"], "41.363127, 69.287982")
        self.assertEqual(payload["meta"]["geocoded_count"], 1)
        self.assertEqual(payload["meta"]["geocode_failed_count"], 0)


if __name__ == "__main__":
    unittest.main()
