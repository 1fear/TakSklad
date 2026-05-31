import json
import shutil
import tempfile
import unittest
from pathlib import Path

import openpyxl
import httpx

from backend.app import telegram_worker as telegram_worker_module
from backend.app import excel_importer
from backend.app.excel_importer import excel_file_to_import_payload
from backend.app.telegram_worker import (
    TELEGRAM_BUTTON_KIZ_BY_FILES,
    TELEGRAM_BUTTON_LOGISTICS_REPORT,
    TELEGRAM_BUTTON_SHIPMENT_DATE,
    TELEGRAM_KIZ_FILE_PREFIX,
    TELEGRAM_LOGISTICS_DATE_PREFIX,
    TelegramWorker,
    display_date,
    telegram_bot_commands,
    telegram_main_keyboard,
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
                }

            worker.send_message = fake_send
            worker.download_telegram_document = fake_download
            worker.backend_post = fake_backend_post

            worker.import_telegram_document("123", {"file_name": "orders.xlsx", "file_id": "file-1"}, shipment_date="31.05.2026")

        self.assertEqual(calls["path"], "/api/v1/imports")
        self.assertEqual(calls["payload"]["source"], "telegram")
        self.assertEqual(calls["payload"]["filename"], "orders.xlsx")
        self.assertEqual(len(calls["payload"]["rows"]), 1)
        self.assertEqual(calls["payload"]["rows"][0]["Дата отгрузки"], "31.05.2026")
        self.assertEqual(messages[0][0], "123")
        self.assertIn("Начинаю импорт", messages[0][1])
        self.assertIn("Excel импортирован через Telegram", messages[-1][1])

    def test_telegram_worker_send_message_uses_bottom_reply_keyboard(self):
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
        self.assertIn("keyboard", payload["reply_markup"])
        self.assertEqual(payload["reply_markup"]["keyboard"][0][0]["text"], TELEGRAM_BUTTON_SHIPMENT_DATE)
        self.assertEqual(payload["reply_markup"]["keyboard"][0][1]["text"], TELEGRAM_BUTTON_LOGISTICS_REPORT)
        self.assertEqual(payload["reply_markup"]["keyboard"][1][0]["text"], TELEGRAM_BUTTON_KIZ_BY_FILES)
        self.assertTrue(payload["reply_markup"]["resize_keyboard"])
        self.assertTrue(payload["reply_markup"]["is_persistent"])

    def test_telegram_main_keyboard_contains_only_user_buttons(self):
        keyboard = telegram_main_keyboard()

        self.assertEqual(
            keyboard["keyboard"],
            [
                [{"text": TELEGRAM_BUTTON_SHIPMENT_DATE}, {"text": TELEGRAM_BUTTON_LOGISTICS_REPORT}],
                [{"text": TELEGRAM_BUTTON_KIZ_BY_FILES}],
            ],
        )
        self.assertTrue(keyboard["resize_keyboard"])
        self.assertTrue(keyboard["is_persistent"])

    def test_telegram_date_buttons_are_user_friendly(self):
        worker = TelegramWorker.__new__(TelegramWorker)

        logistics_keyboard = worker.logistics_date_keyboard(["2026-05-29", "2026-05-30"])
        self.assertEqual(logistics_keyboard["keyboard"][0][0]["text"], f"{TELEGRAM_LOGISTICS_DATE_PREFIX}29.05.2026")
        self.assertEqual(logistics_keyboard["keyboard"][1][0]["text"], f"{TELEGRAM_LOGISTICS_DATE_PREFIX}30.05.2026")

        self.assertEqual(display_date("2026-05-29"), "29.05.2026")
        self.assertEqual(display_date("29.05.2026"), "29.05.2026")
        self.assertEqual(display_date("не дата"), "не дата")

    def test_telegram_worker_send_document_keeps_bottom_reply_keyboard(self):
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
        self.assertEqual(json.loads(captured["data"]["reply_markup"]), telegram_main_keyboard())
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
        self.assertEqual(commands, ["date", "logistics", "kiz_files"])
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

        worker.handle_update({
            "update_id": 11,
            "message": {
                "chat": {"id": 123},
                "text": "29.05.2026",
            },
        })

        self.assertEqual(saved, [("123", "29.05.2026")])
        self.assertIn("Дата отгрузки", messages[0][1])

    def test_telegram_worker_enqueues_excel_document_from_message(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.allowed_chat_ids = set()
        calls = []

        def fake_get_chat_shipment_date(chat_id):
            return "29.05.2026"

        def fake_enqueue(chat_id, document, update_id=None, shipment_date=""):
            calls.append((chat_id, document, update_id, shipment_date))
            return True

        worker.enqueue_telegram_document = fake_enqueue
        worker.get_chat_shipment_date = fake_get_chat_shipment_date

        worker.handle_update({
            "update_id": 77,
            "message": {
                "chat": {"id": 123},
                "document": {"file_name": "orders.xlsx", "file_id": "file-1"},
            },
        })

        self.assertEqual(calls, [("123", {"file_name": "orders.xlsx", "file_id": "file-1"}, 77, "29.05.2026")])

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

        def fake_import(chat_id, document, shipment_date=""):
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

    def test_telegram_worker_shows_kiz_source_file_dates_as_display_dates(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        messages = []
        states = []

        def fake_backend_get(path, params=None):
            self.assertEqual(path, "/api/v1/reports/kiz/source-files")
            return [
                {
                    "source_file": "orders-a.xlsx",
                    "dates": ["2026-05-29"],
                    "planned_blocks": 3,
                    "scanned_blocks": 3,
                },
                {
                    "source_file": "orders-b.xlsx",
                    "dates": ["2026-05-30"],
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

        worker.show_kiz_source_files("123")

        self.assertIn("29.05.2026", messages[0][1])
        self.assertIn("30.05.2026", messages[0][1])
        self.assertEqual(messages[0][2]["keyboard"][0][0]["text"], f"{TELEGRAM_KIZ_FILE_PREFIX}1")
        self.assertEqual(states[0][1]["kiz_files"][0]["source_file"], "orders-a.xlsx")

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
                return updates
            raise AssertionError(method)

        def fake_show_logistics_dates(chat_id):
            raise RuntimeError("backend temporary unavailable")

        def fake_error(chat_id, text, reply_markup=None):
            errors.append((chat_id, text))

        def fake_get_chat_shipment_date(chat_id):
            return "31.05.2026"

        def fake_enqueue(chat_id, document, update_id=None, shipment_date=""):
            enqueued.append((chat_id, document, update_id, shipment_date))
            return True

        def fake_save_offset():
            saved_offsets.append(worker.offset)

        worker.telegram_request = fake_telegram_request
        worker.show_logistics_dates = fake_show_logistics_dates
        worker.safe_send_message = fake_error
        worker.get_chat_shipment_date = fake_get_chat_shipment_date
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
            "31.05.2026",
        )])

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


if __name__ == "__main__":
    unittest.main()
