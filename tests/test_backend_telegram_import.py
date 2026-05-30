import shutil
import tempfile
import unittest
from pathlib import Path

import openpyxl

from backend.app.excel_importer import excel_file_to_import_payload
from backend.app.telegram_worker import (
    TELEGRAM_BUTTON_KIZ_BY_FILES,
    TELEGRAM_BUTTON_LOGISTICS_REPORT,
    TELEGRAM_BUTTON_SHIPMENT_DATE,
    TelegramWorker,
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


if __name__ == "__main__":
    unittest.main()
