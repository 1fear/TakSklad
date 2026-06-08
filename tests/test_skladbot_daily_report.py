import unittest
from datetime import date, datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import openpyxl
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import telegram_worker as telegram_worker_module
from backend.app.models import Base, PendingEvent
from backend.app.skladbot_daily_report import (
    build_skladbot_daily_report_message,
    build_skladbot_daily_report_xlsx,
    collect_skladbot_daily_report,
)
from backend.app.telegram_worker import (
    SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
    TelegramWorker,
)


class FakeSkladBotDailyReportClient:
    configured = True
    customer_id = 6211
    limit = 500

    def get(self, path, params=None):
        params = params or {}
        if path == "/requests/filter/fields":
            return {
                "data": {
                    "types": [
                        {"id": 3389, "name": "Отгрузка 3PL"},
                        {"id": 3403, "name": "Возврат 3PL"},
                        {"id": 3391, "name": "Приемка с услугами"},
                    ]
                }
            }
        if path == "/requests":
            request_type = params.get("type_id")
            if request_type == 3389:
                return {"data": [
                    {
                        "id": 101,
                        "delivery_number": "WH-R-101",
                        "type": "Отгрузка 3PL",
                        "created_at": "2026-06-08T10:00:00+05:00",
                    },
                    {
                        "id": 999,
                        "delivery_number": "WH-R-999",
                        "type": "Отгрузка 3PL",
                        "created_at": "2026-06-01T10:00:00+05:00",
                    },
                ]}
            if request_type == 3403:
                return {"data": [
                    {
                        "id": 202,
                        "delivery_number": "WH-R-202",
                        "type": "Возврат 3PL",
                        "updated_at": "2026-06-08T14:00:00+05:00",
                    },
                ]}
            if request_type == 3391:
                return {"data": [
                    {
                        "id": 303,
                        "delivery_number": "WH-R-303",
                        "type": "Приемка с услугами",
                        "created_at": "2026-06-08T09:00:00+05:00",
                    },
                ]}
            return {"data": []}
        raise AssertionError(path)

    def get_request_detail(self, request_id):
        details = {
            101: self.request_detail(
                101,
                "WH-R-101",
                "Отгрузка 3PL",
                "2026-06-08T10:00:00+05:00",
                "2026-06-08T11:00:00+05:00",
                "2026-06-09",
                "XASAN XUSAN SAVDO SERVIS XK",
                "Ташкент, Карасу",
                "Терминал",
                [{"name": "Chapman Brown OP 20", "vendorCode": "130400353", "barcode": "4006396053978", "amount": 4}],
            ),
            202: self.request_detail(
                202,
                "WH-R-202",
                "Возврат 3PL",
                "2026-06-07T10:00:00+05:00",
                "2026-06-08T14:00:00+05:00",
                "2026-06-08",
                "THE BIG RICH RAKAT",
                "Ташкент, возврат",
                "Возврат",
                [{"name": "Chapman RED OP 20", "vendorCode": "130400237", "barcode": "4006396053947", "amount": 2}],
            ),
            303: self.request_detail(
                303,
                "WH-R-303",
                "Приемка с услугами",
                "2026-06-08T09:00:00+05:00",
                "2026-06-08T09:30:00+05:00",
                "2026-06-08",
                "BASTION IMPORT",
                "Склад",
                "Приемка",
                [{"name": "Chapman Gold SSL", "vendorCode": "4006396054012", "barcode": "4006396054005", "amount": 500}],
            ),
        }
        return details[request_id]

    def post(self, path, payload=None):
        payload = payload or {}
        if path == "/warehouse/transactions":
            if payload.get("type") == "in":
                return {"data": [
                    {
                        "date": "2026-06-08 09:30:00",
                        "delivery_number": "WH-R-303",
                        "type": "in",
                        "product": {"name": "Chapman Gold SSL", "vendorCode": "4006396054012", "barcode": "4006396054005"},
                        "amount": 500,
                        "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                        "box": {"number": "BOX-1"},
                    }
                ]}
            return {"data": [
                {
                    "date": "2026-06-08 11:00:00",
                    "delivery_number": "WH-R-101",
                    "type": "out",
                    "product": {"name": "Chapman Brown OP 20", "vendorCode": "130400353", "barcode": "4006396053978"},
                    "amount": 4,
                    "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                }
            ]}
        if path == "/report/stock":
            return {
                "total": 542,
                "items": [
                    {
                        "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                        "product": {"name": "Chapman Brown OP 20", "vendorCode": "130400353", "barcode": "4006396053978"},
                        "stock": 42,
                    },
                    {
                        "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                        "product": {"name": "Chapman Gold SSL", "vendorCode": "4006396054012", "barcode": "4006396054005"},
                        "stock": 500,
                    },
                ],
            }
        raise AssertionError(path)

    @staticmethod
    def request_detail(request_id, number, request_type, created_at, updated_at, unloading_date, company, address, comment, products):
        return {
            "id": request_id,
            "delivery_number": number,
            "type": request_type,
            "isCompleted": True,
            "archived": False,
            "createdAt": created_at,
            "updatedAt": updated_at,
            "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
            "comment": comment,
            "fields": [
                {"field": "company_name", "value": company},
                {"field": "address", "value": address},
                {"field": "unloading_date", "value": unloading_date},
                {"field": "comment", "value": comment},
            ],
            "products": products,
        }


class SkladBotDailyReportTests(unittest.TestCase):
    def test_collects_requests_movements_stock_and_builds_xlsx(self):
        report = collect_skladbot_daily_report(
            report_date=date(2026, 6, 8),
            client=FakeSkladBotDailyReportClient(),
        )

        summary = report["summary"]
        self.assertEqual(summary["requests_total"], 3)
        self.assertEqual(summary["category_counts"]["Отгрузка"], 1)
        self.assertEqual(summary["category_counts"]["Возврат"], 1)
        self.assertEqual(summary["category_counts"]["Приемка"], 1)
        self.assertEqual(summary["request_blocks_by_category"]["Приемка"], 500)
        self.assertEqual(summary["movement_in_amount"], 500)
        self.assertEqual(summary["movement_out_amount"], 4)
        self.assertEqual(summary["stock_total"], 542)
        self.assertEqual(report["errors"], [])

        content, filename = build_skladbot_daily_report_xlsx(report)
        self.assertEqual(filename, "TakSklad_SkladBot_daily_08.06.2026.xlsx")
        workbook = openpyxl.load_workbook(BytesIO(content), data_only=True)
        self.assertEqual(workbook.sheetnames, ["Сводка", "Заявки", "Товары заявок", "Движения", "Остатки", "Ошибки"])
        requests_sheet = workbook["Заявки"]
        self.assertEqual(requests_sheet.max_row, 4)
        request_values = [cell.value for cell in requests_sheet[2]]
        self.assertIn("WH-R-101", request_values)
        self.assertIn("XASAN XUSAN SAVDO SERVIS XK", request_values)

        message = build_skladbot_daily_report_message(report)
        self.assertIn("SkladBot отчет за 08.06.2026", message)
        self.assertIn("Отгрузка: 1 заявок, 4 блоков", message)
        self.assertIn("Актуальный остаток: 542", message)

    def test_telegram_manual_command_sends_skladbot_daily_report(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.allowed_chat_ids = set()
        worker.admin_chat_ids = set()
        messages = []
        documents = []
        captured = {}

        def fake_collect(report_date=None):
            captured["report_date"] = report_date
            return {
                "report_date": report_date,
                "generated_at": datetime(2026, 6, 8, 22, 0),
                "customer_id": 6211,
                "requests": [],
                "movements": [],
                "stock": {"total": 0, "rows": []},
                "errors": [],
                "summary": {
                    "requests_total": 0,
                    "category_counts": {"Отгрузка": 0, "Возврат": 0, "Приемка": 0, "Прочее": 0},
                    "request_blocks_by_category": {"Отгрузка": 0, "Возврат": 0, "Приемка": 0, "Прочее": 0},
                    "movement_in_amount": 0,
                    "movement_out_amount": 0,
                    "stock_total": 0,
                },
            }

        original_collect = telegram_worker_module.skladbot_daily_report.collect_skladbot_daily_report
        original_build_xlsx = telegram_worker_module.skladbot_daily_report.build_skladbot_daily_report_xlsx
        try:
            telegram_worker_module.skladbot_daily_report.collect_skladbot_daily_report = fake_collect
            telegram_worker_module.skladbot_daily_report.build_skladbot_daily_report_xlsx = lambda report: (b"xlsx", "daily.xlsx")
            worker.safe_send_message = lambda chat_id, text, reply_markup=None: messages.append((chat_id, text))
            worker.safe_send_document = lambda chat_id, content, filename, caption="": documents.append((chat_id, content, filename, caption)) or {"message_id": 1}

            worker.handle_update({
                "update_id": 1,
                "message": {"chat": {"id": 123}, "text": "/skladbot_daily 08.06.2026"},
            })
        finally:
            telegram_worker_module.skladbot_daily_report.collect_skladbot_daily_report = original_collect
            telegram_worker_module.skladbot_daily_report.build_skladbot_daily_report_xlsx = original_build_xlsx

        self.assertEqual(captured["report_date"], date(2026, 6, 8))
        self.assertEqual(messages[0], ("123", "Собираю SkladBot отчет за 08.06.2026."))
        self.assertEqual(documents[0][2], "daily.xlsx")
        self.assertEqual(documents[0][3], "SkladBot отчет за 08.06.2026")

    def test_scheduled_report_is_sent_once_per_chat_and_date(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        original_session_local = telegram_worker_module.SessionLocal
        try:
            telegram_worker_module.SessionLocal = SessionLocal
            worker = TelegramWorker.__new__(TelegramWorker)
            worker.skladbot_daily_report_enabled = True
            worker.skladbot_daily_report_chat_ids = {"123"}
            worker.skladbot_daily_report_hour = 22
            worker.skladbot_daily_report_minute = 0
            worker.skladbot_daily_report_retry_minutes = 15
            sends = []
            worker.send_skladbot_daily_report = lambda chat_id, report_date=None, scheduled=False: sends.append((chat_id, report_date, scheduled)) or True

            now = datetime(2026, 6, 8, 22, 5, tzinfo=ZoneInfo("Asia/Tashkent"))
            self.assertEqual(worker.send_due_skladbot_daily_reports(now=now), 1)
            self.assertEqual(worker.send_due_skladbot_daily_reports(now=now), 0)

            with SessionLocal() as db:
                events = db.execute(select(PendingEvent)).scalars().all()

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].event_type, SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE)
            self.assertEqual(events[0].status, "completed")
            self.assertEqual(sends, [("123", date(2026, 6, 8), True)])
        finally:
            telegram_worker_module.SessionLocal = original_session_local


if __name__ == "__main__":
    unittest.main()
