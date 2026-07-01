import os
import unittest
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from zoneinfo import ZoneInfo

import openpyxl
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import telegram_worker as telegram_worker_module
from backend.app.models import Base, PendingEvent
from backend.app.skladbot_daily_report import (
    MOVEMENT_HEADERS,
    REQUEST_HEADERS,
    REQUEST_PRODUCT_HEADERS,
    STOCK_HEADERS,
    build_skladbot_daily_report_message,
    build_skladbot_daily_report_xlsx,
    categorize_request_type,
    collect_skladbot_daily_report,
    product_breakdown_for_summary,
    summarize_daily_report,
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
                        "created_at": "2026-06-08T10:00:00+05:00",
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
                "Терминал\nТП-1 Умид\nРабочий номер: +998 91 111 11 11\nЛичный номер: +998 90 222 22 22",
                [{"name": "Chapman Brown OP 20", "vendorCode": "130400353", "barcode": "4006396053978", "amount": 4}],
            ),
            999: {
                **self.request_detail(
                    999,
                    "WH-R-999",
                    "Отгрузка 3PL",
                    "2026-06-01T10:00:00+05:00",
                    "2026-06-01T11:00:00+05:00",
                    "2026-06-01",
                    "OLD REQUEST",
                    "Ташкент",
                    "Терминал",
                    [{"name": "Chapman Brown OP 20", "vendorCode": "130400353", "barcode": "4006396053978", "amount": 1}],
                ),
                "completedAt": "2026-06-01T11:00:00+05:00",
                "archivedAt": "2026-06-01T12:00:00+05:00",
            },
            202: self.request_detail(
                202,
                "WH-R-202",
                "Возврат 3PL",
                "2026-06-08T10:00:00+05:00",
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
                [{"name": "Chapman Gold SSL", "vendorCode": "4006396054012", "barcode": "4006396054005", "amount": 1, "acceptedAmount": 500}],
            ),
        }
        return details[request_id]

    def post(self, path, payload=None):
        payload = payload or {}
        if path == "/products":
            return {
                "total": 3,
                "data": {
                    "2010857": [{
                        "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                        "name": "Chapman Brown OP 20",
                        "vendor_code": "130400353",
                        "barcode": "4006396053978",
                        "amount": 42,
                    }],
                    "2010858": [{
                        "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                        "name": "Chapman Gold SSL",
                        "vendor_code": "4006396054012",
                        "barcode": "4006396054005",
                        "amount": 500,
                    }],
                    "2010859": [{
                        "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                        "name": "Chapman RED OP 20",
                        "vendor_code": "130400237",
                        "barcode": "4006396053947",
                        "amount": 2,
                    }],
                },
            }
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
                "total": 544,
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
                    {
                        "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                        "product": {"name": "Chapman RED OP 20", "vendorCode": "130400237", "barcode": "4006396053947"},
                        "stock": 2,
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
            "archived": True,
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


class TransientRateLimitDailyReportClient(FakeSkladBotDailyReportClient):
    def __init__(self):
        self.detail_calls = {}

    def get_request_detail(self, request_id):
        self.detail_calls[request_id] = self.detail_calls.get(request_id, 0) + 1
        if request_id == 101 and self.detail_calls[request_id] == 1:
            raise RuntimeError("429 Too Many Requests")
        return super().get_request_detail(request_id)


class PreviousDayReceivingCompletedTodayClient(FakeSkladBotDailyReportClient):
    request_id = 404

    def get(self, path, params=None):
        params = params or {}
        if path == "/requests/filter/fields":
            return {"data": {"types": [{"id": 3391, "name": "Приемка с услугами"}]}}
        if path == "/requests" and params.get("type_id") == 3391:
            return {"data": [{
                "id": self.request_id,
                "delivery_number": "WH-R-404",
                "type": "Приемка с услугами",
                "created_at": "2026-06-19T18:00:00+05:00",
                "archived": True,
                "isCompleted": True,
            }]}
        return {"data": []}

    def get_request_detail(self, request_id):
        if request_id != self.request_id:
            raise AssertionError(request_id)
        return self.request_detail(
            self.request_id,
            "WH-R-404",
            "Приемка с услугами",
            "2026-06-19T18:00:00+05:00",
            "",
            "2026-06-19",
            "BASTION IMPORT",
            "Склад",
            "Приемка",
            [{"name": "Chapman Green OP 20", "vendorCode": "CHPMGreenOP20UZ", "barcode": "4006396104441", "amount": 1, "acceptedAmount": 10000}],
        )


class StaleRequestDetailShouldNotBeCalledClient(PreviousDayReceivingCompletedTodayClient):
    def get_request_detail(self, request_id):
        raise AssertionError("stale request should not be detailed")


class CompletedAfterCutoffReceivingClient(PreviousDayReceivingCompletedTodayClient):
    request_id = 405

    def get(self, path, params=None):
        params = params or {}
        if path == "/requests/filter/fields":
            return {"data": {"types": [{"id": 3391, "name": "Приемка с услугами"}]}}
        if path == "/requests" and params.get("type_id") == 3391:
            return {"data": [{
                "id": self.request_id,
                "delivery_number": "WH-R-405",
                "type": "Приемка с услугами",
                "created_at": "2026-06-20T18:00:00+05:00",
                "archived": True,
                "isCompleted": True,
            }]}
        return {"data": []}

    def get_request_detail(self, request_id):
        if request_id != self.request_id:
            raise AssertionError(request_id)
        detail = self.request_detail(
            self.request_id,
            "WH-R-405",
            "Приемка с услугами",
            "2026-06-20T18:00:00+05:00",
            "",
            "2026-06-20",
            "BASTION IMPORT",
            "Склад",
            "Приемка",
            [{"name": "Chapman Green OP 20", "vendorCode": "CHPMGreenOP20UZ", "barcode": "4006396104441", "amount": 1, "acceptedAmount": 500}],
        )
        detail["completedAt"] = "2026-06-20T23:10:00+05:00"
        return detail


class OldListDateReceivingCompletedTodayClient(PreviousDayReceivingCompletedTodayClient):
    request_id = 406

    def get(self, path, params=None):
        params = params or {}
        if path == "/requests/filter/fields":
            return {"data": {"types": [{"id": 3391, "name": "Приемка с услугами"}]}}
        if path == "/requests" and params.get("type_id") == 3391:
            return {"data": [{
                "id": self.request_id,
                "delivery_number": "WH-R-406",
                "type": "Приемка с услугами",
                "created_at": "2026-06-10T18:00:00+05:00",
                "archived": True,
                "isCompleted": True,
            }]}
        return {"data": []}

    def get_request_detail(self, request_id):
        if request_id != self.request_id:
            raise AssertionError(request_id)
        detail = self.request_detail(
            self.request_id,
            "WH-R-406",
            "Приемка с услугами",
            "2026-06-10T18:00:00+05:00",
            "",
            "2026-06-10",
            "BASTION IMPORT",
            "Склад",
            "Приемка",
            [{"name": "Chapman Green OP 20", "vendorCode": "CHPMGreenOP20UZ", "barcode": "4006396104441", "amount": 1, "acceptedAmount": 700}],
        )
        detail["completedAt"] = "2026-06-20T12:10:00+05:00"
        return detail


class ArchivedAfterCutoffReceivingClient(CompletedAfterCutoffReceivingClient):
    request_id = 407

    def get(self, path, params=None):
        params = params or {}
        if path == "/requests/filter/fields":
            return {"data": {"types": [{"id": 3391, "name": "Приемка с услугами"}]}}
        if path == "/requests" and params.get("type_id") == 3391:
            return {"data": [{
                "id": self.request_id,
                "delivery_number": "WH-R-407",
                "type": "Приемка с услугами",
                "created_at": "2026-06-20T18:00:00+05:00",
                "archived": True,
                "isCompleted": True,
            }]}
        return {"data": []}

    def get_request_detail(self, request_id):
        if request_id != self.request_id:
            raise AssertionError(request_id)
        detail = self.request_detail(
            self.request_id,
            "WH-R-407",
            "Приемка с услугами",
            "2026-06-20T18:00:00+05:00",
            "",
            "2026-06-20",
            "BASTION IMPORT",
            "Склад",
            "Приемка",
            [{"name": "Chapman Green OP 20", "vendorCode": "CHPMGreenOP20UZ", "barcode": "4006396104441", "amount": 1, "acceptedAmount": 900}],
        )
        detail["completedAt"] = "2026-06-20T20:10:00+05:00"
        detail["archivedAt"] = "2026-06-20T23:10:00+05:00"
        return detail


class OldCreatedUnloadingTodayClient(PreviousDayReceivingCompletedTodayClient):
    request_id = 408

    def get(self, path, params=None):
        params = params or {}
        if path == "/requests/filter/fields":
            return {"data": {"types": [{"id": 3389, "name": "Отгрузка 3PL"}]}}
        if path == "/requests" and params.get("type_id") == 3389:
            return {"data": [{
                "id": self.request_id,
                "delivery_number": "WH-R-408",
                "type": "Отгрузка 3PL",
                "created_at": "2026-06-19T18:00:00+05:00",
                "archived": True,
                "isCompleted": True,
            }]}
        return {"data": []}

    def get_request_detail(self, request_id):
        if request_id != self.request_id:
            raise AssertionError(request_id)
        detail = self.request_detail(
            self.request_id,
            "WH-R-408",
            "Отгрузка 3PL",
            "2026-06-19T18:00:00+05:00",
            "",
            "2026-06-20",
            "OLD CREATED UNLOADING TODAY",
            "Ташкент",
            "Терминал",
            [{"name": "Chapman RED OP 20", "vendorCode": "CHPMRedOP20UZ", "barcode": "4006396053947", "amount": 11}],
        )
        return detail


class MovementTodayRequestClient(PreviousDayReceivingCompletedTodayClient):
    request_id = 409

    def get(self, path, params=None):
        params = params or {}
        if path == "/requests/filter/fields":
            return {"data": {"types": [{"id": 3389, "name": "Отгрузка 3PL"}]}}
        if path == "/requests" and params.get("type_id") == 3389:
            return {"data": [{
                "id": self.request_id,
                "delivery_number": "WH-R-409",
                "type": "Отгрузка 3PL",
                "created_at": "2026-06-19T18:00:00+05:00",
                "archived": False,
                "isCompleted": False,
            }]}
        return {"data": []}

    def get_request_detail(self, request_id):
        if request_id != self.request_id:
            raise AssertionError(request_id)
        detail = self.request_detail(
            self.request_id,
            "WH-R-409",
            "Отгрузка 3PL",
            "2026-06-19T18:00:00+05:00",
            "",
            "2026-06-19",
            "MOVEMENT TODAY",
            "Ташкент",
            "Терминал",
            [{"name": "Chapman Brown OP 20", "vendorCode": "CHPMBrownOP20UZ", "barcode": "4006396053978", "amount": 5}],
        )
        detail["isCompleted"] = False
        detail["archived"] = False
        return detail

    def post(self, path, payload=None):
        payload = payload or {}
        if path == "/warehouse/transactions":
            if payload.get("type") == "out":
                return {"data": [{
                    "date": "2026-06-20 12:00:00",
                    "delivery_number": "WH-R-409",
                    "type": "out",
                    "product": {"name": "Chapman Brown OP 20", "vendorCode": "CHPMBrownOP20UZ", "barcode": "4006396053978"},
                    "amount": 5,
                    "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                }]}
            return {"data": []}
        return super().post(path, payload)


class CompletedArchivedMovementTodayRequestClient(MovementTodayRequestClient):
    def get(self, path, params=None):
        params = params or {}
        if path == "/requests/filter/fields":
            return {"data": {"types": [{"id": 3389, "name": "Отгрузка 3PL"}]}}
        if path == "/requests" and params.get("type_id") == 3389:
            return {"data": [{
                "id": self.request_id,
                "delivery_number": "WH-R-409",
                "type": "Отгрузка 3PL",
                "created_at": "2026-06-19T18:00:00+05:00",
                "archived": True,
                "isCompleted": True,
            }]}
        return {"data": []}

    def get_request_detail(self, request_id):
        detail = super().get_request_detail(request_id)
        detail["isCompleted"] = True
        detail["archived"] = True
        return detail


class MixedDateMovementsClient(MovementTodayRequestClient):
    def post(self, path, payload=None):
        payload = payload or {}
        if path == "/warehouse/transactions":
            if payload.get("type") == "out":
                return {"data": [
                    {
                        "date": "2026-06-19 23:59:00",
                        "delivery_number": "WH-R-OLD",
                        "type": "out",
                        "product": {"name": "Chapman Brown OP 20", "vendorCode": "CHPMBrownOP20UZ", "barcode": "4006396053978"},
                        "amount": 99,
                        "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                    },
                    {
                        "date": "2026-06-20 12:00:00",
                        "delivery_number": "WH-R-409",
                        "type": "out",
                        "product": {"name": "Chapman Brown OP 20", "vendorCode": "CHPMBrownOP20UZ", "barcode": "4006396053978"},
                        "amount": 5,
                        "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                    },
                    {
                        "date": "",
                        "delivery_number": "WH-R-NO-DATE",
                        "type": "out",
                        "product": {"name": "Chapman Brown OP 20", "vendorCode": "CHPMBrownOP20UZ", "barcode": "4006396053978"},
                        "amount": 7,
                        "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                    },
                ]}
            return {"data": []}
        return super().post(path, payload)


class MovementTodayRequestAfterStaleListItemsClient(MovementTodayRequestClient):
    def get(self, path, params=None):
        params = params or {}
        if path == "/requests/filter/fields":
            return {"data": {"types": [{"id": 3389, "name": "Отгрузка 3PL"}]}}
        if path == "/requests" and params.get("type_id") == 3389:
            return {"data": [
                {
                    "id": 999,
                    "delivery_number": "WH-R-999",
                    "type": "Отгрузка 3PL",
                    "created_at": "2026-06-01T18:00:00+05:00",
                    "archived": True,
                    "isCompleted": True,
                },
                {
                    "id": self.request_id,
                    "delivery_number": "WH-R-409",
                    "type": "Отгрузка 3PL",
                    "created_at": "2026-06-20T18:00:00+05:00",
                    "archived": True,
                    "isCompleted": True,
                },
            ]}
        return {"data": []}

    def get_request_detail(self, request_id):
        if request_id == 999:
            raise AssertionError("stale request should not consume today's request detail limit")
        if request_id != self.request_id:
            raise AssertionError(request_id)
        return self.request_detail(
            self.request_id,
            "WH-R-409",
            "Отгрузка 3PL",
            "2026-06-20T18:00:00+05:00",
            "",
            "2026-06-20",
            "MOVEMENT TODAY",
            "Ташкент",
            "Терминал",
            [{"name": "Chapman Brown OP 20", "vendorCode": "CHPMBrownOP20UZ", "barcode": "4006396053978", "amount": 5}],
        )


class SkladBotDailyReportTests(unittest.TestCase):
    def test_collects_requests_movements_stock_and_builds_xlsx(self):
        original_delay = os.environ.get("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS")
        try:
            os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = "0"
            report = collect_skladbot_daily_report(
                report_date=date(2026, 6, 8),
                client=FakeSkladBotDailyReportClient(),
            )
        finally:
            if original_delay is None:
                os.environ.pop("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS", None)
            else:
                os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = original_delay

        summary = report["summary"]
        self.assertEqual(summary["requests_total"], 3)
        self.assertEqual(summary["category_counts"]["Отгрузка"], 1)
        self.assertEqual(summary["category_counts"]["Отгрузка в браке"], 0)
        self.assertEqual(summary["category_counts"]["Возврат"], 1)
        self.assertEqual(summary["category_counts"]["Приемка"], 1)
        self.assertEqual(summary["request_blocks_by_category"]["Приемка"], 500)
        self.assertEqual(summary["movement_in_amount"], 500)
        self.assertEqual(summary["movement_out_amount"], 4)
        self.assertEqual(summary["stock_total"], 544)
        self.assertEqual(report["errors"], [])

        content, filename = build_skladbot_daily_report_xlsx(report)
        self.assertEqual(filename, "TakSklad_SkladBot_daily_08.06.2026.xlsx")
        workbook = openpyxl.load_workbook(BytesIO(content), data_only=False)
        self.assertEqual(workbook.sheetnames, ["Сводка", "Заявки", "Товары заявок", "Движения", "Остатки", "Ошибки"])

        summary_sheet = workbook["Сводка"]
        self.assertEqual(summary_sheet["A1"].value, "Показатель")
        self.assertEqual(summary_sheet["B1"].value, "Значение")
        self.assertEqual(summary_sheet["A6"].value, "Отчет о движении остатков за день")
        self.assertEqual(summary_sheet["B7"].value, "Всего блоков")
        self.assertEqual(summary_sheet["C7"].value, "Chapman Brown OP 20")
        self.assertEqual(summary_sheet["D7"].value, "Chapman Gold SSL")
        self.assertEqual(summary_sheet["E7"].value, "Chapman RED OP 20")
        self.assertEqual(summary_sheet["F7"].value, "Заявок")
        self.assertEqual(summary_sheet["A8"].value, "Остаток на начало дня ")
        self.assertEqual(summary_sheet["B8"].value, "=B13-B9-B10-B11-B12")
        self.assertEqual(summary_sheet["C8"].value, "=C13-C9-C10-C11-C12")
        self.assertEqual(summary_sheet["D8"].value, "=D13-D9-D10-D11-D12")
        self.assertEqual(summary_sheet["E8"].value, "=E13-E9-E10-E11-E12")
        self.assertEqual(summary_sheet["A9"].value, "Приемка")
        self.assertEqual(summary_sheet["B9"].value, 500)
        self.assertEqual(summary_sheet["C9"].value, 0)
        self.assertEqual(summary_sheet["D9"].value, 500)
        self.assertEqual(summary_sheet["E9"].value, 0)
        self.assertEqual(summary_sheet["F9"].value, 1)
        self.assertEqual(summary_sheet["A10"].value, "Отгрузка")
        self.assertEqual(summary_sheet["B10"].value, -4)
        self.assertEqual(summary_sheet["C10"].value, -4)
        self.assertEqual(summary_sheet["D10"].value, 0)
        self.assertEqual(summary_sheet["E10"].value, 0)
        self.assertEqual(summary_sheet["F10"].value, 1)
        self.assertEqual(summary_sheet["A11"].value, "Отгрузка в браке")
        self.assertEqual(summary_sheet["B11"].value, 0)
        self.assertEqual(summary_sheet["C11"].value, 0)
        self.assertEqual(summary_sheet["D11"].value, 0)
        self.assertEqual(summary_sheet["E11"].value, 0)
        self.assertEqual(summary_sheet["F11"].value, 0)
        self.assertEqual(summary_sheet["A12"].value, "Возврат")
        self.assertEqual(summary_sheet["B12"].value, 2)
        self.assertEqual(summary_sheet["C12"].value, 0)
        self.assertEqual(summary_sheet["D12"].value, 0)
        self.assertEqual(summary_sheet["E12"].value, 2)
        self.assertEqual(summary_sheet["F12"].value, 1)
        self.assertEqual(summary_sheet["A13"].value, "Остаток на конец дня")
        self.assertEqual(summary_sheet["B13"].value, 544)
        self.assertEqual(summary_sheet["C13"].value, 42)
        self.assertEqual(summary_sheet["D13"].value, 500)
        self.assertEqual(summary_sheet["E13"].value, 2)
        self.assertEqual(summary_sheet.freeze_panes, "A2")
        self.assertEqual(summary_sheet["A8"].border.left.style, "thin")
        self.assertEqual(summary_sheet["F13"].border.right.style, "thin")
        self.assertEqual(summary_sheet.column_dimensions["A"].width, 28)
        self.assertEqual(summary_sheet.column_dimensions["B"].width, 21)

        requests_sheet = workbook["Заявки"]
        self.assertEqual([cell.value for cell in requests_sheet[1]], REQUEST_HEADERS)
        self.assertEqual(requests_sheet.max_row, 4)
        request_rows = [
            {header: requests_sheet.cell(row=row, column=index + 1).value for index, header in enumerate(REQUEST_HEADERS)}
            for row in range(2, requests_sheet.max_row + 1)
        ]
        request_by_number = {row["Номер"]: row for row in request_rows}
        self.assertEqual(request_by_number["WH-R-101"]["Юрлицо/точка"], "XASAN XUSAN SAVDO SERVIS XK")
        self.assertEqual(request_by_number["WH-R-101"]["Торговый представитель"], "ТП-1 Умид")
        self.assertEqual(request_by_number["WH-R-101"]["Блоков план"], 4)
        self.assertEqual(request_by_number["WH-R-101"]["Блоков факт"], 4)
        self.assertEqual(request_by_number["WH-R-101"]["Отклонение"], 0)
        self.assertEqual(request_by_number["WH-R-303"]["Торговый представитель"], None)
        self.assertEqual(request_by_number["WH-R-303"]["Блоков план"], 1)
        self.assertEqual(request_by_number["WH-R-303"]["Блоков факт"], 500)
        self.assertEqual(request_by_number["WH-R-303"]["Отклонение"], 499)

        products_sheet = workbook["Товары заявок"]
        self.assertEqual([cell.value for cell in products_sheet[1]], REQUEST_PRODUCT_HEADERS)
        product_rows = [
            {header: products_sheet.cell(row=row, column=index + 1).value for index, header in enumerate(REQUEST_PRODUCT_HEADERS)}
            for row in range(2, products_sheet.max_row + 1)
        ]
        self.assertIn("Chapman Gold SSL", [row["Товар"] for row in product_rows])
        brown_product_row = next(row for row in product_rows if row["Товар"] == "Chapman Brown OP 20")
        self.assertEqual(brown_product_row["Торговый представитель"], "ТП-1 Умид")
        gold_product_row = next(row for row in product_rows if row["Товар"] == "Chapman Gold SSL")
        self.assertEqual(gold_product_row["Блоков план"], 1)
        self.assertEqual(gold_product_row["Принято факт"], 500)
        self.assertEqual(gold_product_row["Блоков факт"], 500)
        self.assertEqual(gold_product_row["Отклонение"], 499)

        movements_sheet = workbook["Движения"]
        self.assertEqual([cell.value for cell in movements_sheet[1]], MOVEMENT_HEADERS)
        movement_rows = [
            {header: movements_sheet.cell(row=row, column=index + 1).value for index, header in enumerate(MOVEMENT_HEADERS)}
            for row in range(2, movements_sheet.max_row + 1)
        ]
        self.assertIn("Приход", [row["Направление"] for row in movement_rows])
        self.assertIn("Расход", [row["Направление"] for row in movement_rows])

        stock_sheet = workbook["Остатки"]
        self.assertEqual([cell.value for cell in stock_sheet[1]], STOCK_HEADERS)
        self.assertEqual(stock_sheet.max_row, 4)
        stock_rows = [
            {header: stock_sheet.cell(row=row, column=index + 1).value for index, header in enumerate(STOCK_HEADERS)}
            for row in range(2, stock_sheet.max_row + 1)
        ]
        self.assertEqual(sum(row["Остаток"] for row in stock_rows), 544)
        self.assertIn("Chapman Brown OP 20", [row["Товар"] for row in stock_rows])
        self.assertIn("Chapman Gold SSL", [row["Товар"] for row in stock_rows])
        self.assertIn("Chapman RED OP 20", [row["Товар"] for row in stock_rows])

        errors_sheet = workbook["Ошибки"]
        self.assertEqual(errors_sheet["A1"].value, "Ошибка")

        message = build_skladbot_daily_report_message(report)
        self.assertIn("SkladBot отчет за 08.06.2026", message)
        self.assertIn("Отгрузка: 1 заявок, 4 блоков", message)
        self.assertIn("Актуальный остаток: 544", message)

    def test_daily_report_xlsx_handles_partial_data_and_errors(self):
        report = {
            "report_date": date(2026, 6, 8),
            "generated_at": datetime(2026, 6, 8, 22, 0),
            "customer_id": 6211,
            "requests": [],
            "movements": [],
            "stock": {"total": 0, "rows": []},
            "errors": ["Не удалось получить остаток SkladBot"],
            "summary": {
                "requests_total": 0,
                "category_counts": {"Отгрузка": 0, "Отгрузка в браке": 0, "Возврат": 0, "Приемка": 0, "Прочее": 0},
                "type_counts": {},
                "request_blocks_by_category": {"Отгрузка": 0, "Отгрузка в браке": 0, "Возврат": 0, "Приемка": 0, "Прочее": 0},
                "movements_total": 0,
                "movement_in_rows": 0,
                "movement_out_rows": 0,
                "movement_in_amount": 0,
                "movement_out_amount": 0,
                "stock_total": 0,
                "stock_rows": 0,
                "errors": 1,
            },
        }

        content, _filename = build_skladbot_daily_report_xlsx(report)
        workbook = openpyxl.load_workbook(BytesIO(content), data_only=False)

        self.assertEqual(workbook["Сводка"]["B9"].value, 0)
        self.assertEqual(workbook["Сводка"]["B10"].value, 0)
        self.assertEqual(workbook["Сводка"]["B11"].value, 0)
        self.assertEqual(workbook["Сводка"]["B12"].value, 0)
        self.assertEqual(workbook["Сводка"]["B13"].value, 0)
        self.assertEqual(workbook["Остатки"].max_row, 2)
        self.assertEqual(workbook["Остатки"]["E2"].value, 0)
        self.assertEqual(workbook["Ошибки"]["A2"].value, "Не удалось получить остаток SkladBot")

    def test_daily_report_retries_request_detail_after_rate_limit(self):
        client = TransientRateLimitDailyReportClient()
        original_delay = os.environ.get("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS")
        original_retry_seconds = os.environ.get("SKLADBOT_DAILY_REPORT_429_RETRY_SECONDS")
        try:
            os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = "0"
            os.environ["SKLADBOT_DAILY_REPORT_429_RETRY_SECONDS"] = "0"
            report = collect_skladbot_daily_report(
                report_date=date(2026, 6, 8),
                client=client,
            )
        finally:
            if original_delay is None:
                os.environ.pop("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS", None)
            else:
                os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = original_delay
            if original_retry_seconds is None:
                os.environ.pop("SKLADBOT_DAILY_REPORT_429_RETRY_SECONDS", None)
            else:
                os.environ["SKLADBOT_DAILY_REPORT_429_RETRY_SECONDS"] = original_retry_seconds

        self.assertEqual(client.detail_calls[101], 2)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["summary"]["requests_total"], 3)

    def test_daily_report_skips_completed_request_when_created_date_is_stale(self):
        original_delay = os.environ.get("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS")
        try:
            os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = "0"
            report = collect_skladbot_daily_report(
                report_date=date(2026, 6, 20),
                client=PreviousDayReceivingCompletedTodayClient(),
            )
        finally:
            if original_delay is None:
                os.environ.pop("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS", None)
            else:
                os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = original_delay

        self.assertEqual(report["summary"]["requests_total"], 0)
        self.assertEqual(report["summary"]["category_counts"]["Приемка"], 0)
        self.assertEqual(report["summary"]["request_blocks_by_category"]["Приемка"], 0)

    def test_daily_report_does_not_detail_stale_created_requests(self):
        original_delay = os.environ.get("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS")
        try:
            os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = "0"
            report = collect_skladbot_daily_report(
                report_date=date(2026, 6, 20),
                client=StaleRequestDetailShouldNotBeCalledClient(),
            )
        finally:
            if original_delay is None:
                os.environ.pop("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS", None)
            else:
                os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = original_delay

        self.assertEqual(report["summary"]["requests_total"], 0)
        self.assertEqual(report["errors"], [])

    def test_daily_report_skips_completed_request_created_before_report_date(self):
        original_delay = os.environ.get("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS")
        try:
            os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = "0"
            report = collect_skladbot_daily_report(
                report_date=date(2026, 6, 21),
                client=PreviousDayReceivingCompletedTodayClient(),
            )
        finally:
            if original_delay is None:
                os.environ.pop("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS", None)
            else:
                os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = original_delay

        self.assertEqual(report["summary"]["requests_total"], 0)
        self.assertEqual(report["summary"]["request_blocks_by_category"]["Приемка"], 0)

    def test_daily_report_includes_created_and_completed_today_request(self):
        original_delay = os.environ.get("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS")
        try:
            os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = "0"
            report_20 = collect_skladbot_daily_report(
                report_date=date(2026, 6, 20),
                client=CompletedAfterCutoffReceivingClient(),
            )
            report_21 = collect_skladbot_daily_report(
                report_date=date(2026, 6, 21),
                client=CompletedAfterCutoffReceivingClient(),
            )
        finally:
            if original_delay is None:
                os.environ.pop("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS", None)
            else:
                os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = original_delay

        self.assertEqual(report_20["summary"]["requests_total"], 1)
        self.assertEqual(report_20["summary"]["request_blocks_by_category"]["Приемка"], 500)
        self.assertEqual(report_20["requests"][0]["include_reasons"], ["создана"])
        self.assertEqual(report_21["summary"]["requests_total"], 0)
        self.assertEqual(report_21["summary"]["request_blocks_by_category"]["Приемка"], 0)

    def test_daily_report_skips_completed_today_request_when_created_date_is_stale(self):
        original_delay = os.environ.get("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS")
        try:
            os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = "0"
            report = collect_skladbot_daily_report(
                report_date=date(2026, 6, 20),
                client=OldListDateReceivingCompletedTodayClient(),
            )
        finally:
            if original_delay is None:
                os.environ.pop("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS", None)
            else:
                os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = original_delay

        self.assertEqual(report["summary"]["requests_total"], 0)
        self.assertEqual(report["summary"]["request_blocks_by_category"]["Приемка"], 0)

    def test_daily_report_ignores_archive_cutoff_when_created_on_report_date(self):
        original_delay = os.environ.get("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS")
        try:
            os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = "0"
            report_20 = collect_skladbot_daily_report(
                report_date=date(2026, 6, 20),
                client=ArchivedAfterCutoffReceivingClient(),
            )
            report_21 = collect_skladbot_daily_report(
                report_date=date(2026, 6, 21),
                client=ArchivedAfterCutoffReceivingClient(),
            )
        finally:
            if original_delay is None:
                os.environ.pop("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS", None)
            else:
                os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = original_delay

        self.assertEqual(report_20["summary"]["requests_total"], 1)
        self.assertEqual(report_20["summary"]["request_blocks_by_category"]["Приемка"], 900)
        self.assertEqual(report_20["requests"][0]["include_reasons"], ["создана"])
        self.assertEqual(report_21["summary"]["requests_total"], 0)
        self.assertEqual(report_21["summary"]["request_blocks_by_category"]["Приемка"], 0)

    def test_daily_report_skips_request_by_unloading_date_when_created_before_report_date(self):
        original_delay = os.environ.get("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS")
        try:
            os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = "0"
            report = collect_skladbot_daily_report(
                report_date=date(2026, 6, 20),
                client=OldCreatedUnloadingTodayClient(),
            )
        finally:
            if original_delay is None:
                os.environ.pop("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS", None)
            else:
                os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = original_delay

        self.assertEqual(report["summary"]["requests_total"], 0)
        self.assertEqual(report["summary"]["category_counts"]["Отгрузка"], 0)
        self.assertEqual(report["summary"]["request_blocks_by_category"]["Отгрузка"], 0)

    def test_daily_report_records_warehouse_movement_without_including_stale_request(self):
        original_delay = os.environ.get("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS")
        try:
            os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = "0"
            report = collect_skladbot_daily_report(
                report_date=date(2026, 6, 20),
                client=MovementTodayRequestClient(),
            )
        finally:
            if original_delay is None:
                os.environ.pop("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS", None)
            else:
                os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = original_delay

        self.assertEqual(report["summary"]["requests_total"], 0)
        self.assertEqual(report["summary"]["category_counts"]["Отгрузка"], 0)
        self.assertEqual(report["summary"]["request_blocks_by_category"]["Отгрузка"], 0)
        self.assertEqual(report["summary"]["movement_out_amount"], 5)

    def test_daily_report_records_movement_without_including_old_completed_request(self):
        original_delay = os.environ.get("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS")
        try:
            os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = "0"
            report = collect_skladbot_daily_report(
                report_date=date(2026, 6, 20),
                client=CompletedArchivedMovementTodayRequestClient(),
            )
        finally:
            if original_delay is None:
                os.environ.pop("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS", None)
            else:
                os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = original_delay

        self.assertEqual(report["summary"]["requests_total"], 0)
        self.assertEqual(report["summary"]["category_counts"]["Отгрузка"], 0)
        self.assertEqual(report["summary"]["request_blocks_by_category"]["Отгрузка"], 0)
        self.assertEqual(report["summary"]["movement_out_amount"], 5)

    def test_daily_report_keeps_only_report_date_movements(self):
        original_delay = os.environ.get("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS")
        try:
            os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = "0"
            report = collect_skladbot_daily_report(
                report_date=date(2026, 6, 20),
                client=MixedDateMovementsClient(),
            )
        finally:
            if original_delay is None:
                os.environ.pop("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS", None)
            else:
                os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = original_delay

        self.assertEqual(report["summary"]["movement_out_amount"], 5)
        self.assertEqual([item["request_number"] for item in report["movements"]], ["WH-R-409"])

    def test_daily_report_prioritizes_today_created_request_before_stale_list_items(self):
        original_delay = os.environ.get("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS")
        original_detail_limit = os.environ.get("SKLADBOT_DAILY_REPORT_DETAIL_LIMIT")
        try:
            os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = "0"
            os.environ["SKLADBOT_DAILY_REPORT_DETAIL_LIMIT"] = "1"
            report = collect_skladbot_daily_report(
                report_date=date(2026, 6, 20),
                client=MovementTodayRequestAfterStaleListItemsClient(),
            )
        finally:
            if original_delay is None:
                os.environ.pop("SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS", None)
            else:
                os.environ["SKLADBOT_DAILY_REPORT_REQUEST_DELAY_SECONDS"] = original_delay
            if original_detail_limit is None:
                os.environ.pop("SKLADBOT_DAILY_REPORT_DETAIL_LIMIT", None)
            else:
                os.environ["SKLADBOT_DAILY_REPORT_DETAIL_LIMIT"] = original_detail_limit

        self.assertEqual(report["summary"]["requests_total"], 1)
        self.assertEqual(report["requests"][0]["number"], "WH-R-409")
        self.assertEqual(report["requests"][0]["include_reasons"], ["создана"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["summary"]["movement_out_amount"], 5)

    def test_daily_report_product_breakdown_merges_stock_and_request_aliases(self):
        report = {
            "stock": {
                "rows": [{
                    "product": "",
                    "vendor_code": "130400353",
                    "barcode": "4006396053978",
                    "stock": 42,
                }],
            },
            "requests": [{
                "category": "Отгрузка",
                "products": [{
                    "name": "Chapman Brown OP 20",
                    "vendor_code": "130400353",
                    "barcode": "4006396053978",
                    "amount": 4,
                    "accepted_amount": 0,
                }],
            }],
        }

        rows = product_breakdown_for_summary(report)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ending_stock"], 42)
        self.assertEqual(rows[0]["outbound"], 4)

    def test_daily_report_uses_accepted_amount_for_receiving(self):
        report = {
            "stock": {"rows": []},
            "requests": [{
                "category": "Приемка",
                "products": [{
                    "name": "Chapman Brown OP 20",
                    "vendor_code": "130400353",
                    "barcode": "4006396053978",
                    "amount": 1,
                    "accepted_amount": 1750,
                }],
            }],
        }

        rows = product_breakdown_for_summary(report)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["inbound"], 1750)

    def test_daily_report_uses_zero_accepted_amount_for_receiving_when_skladbot_sent_it(self):
        report = {
            "stock": {"rows": []},
            "requests": [{
                "category": "Приемка",
                "products": [{
                    "name": "Chapman Brown OP 20",
                    "vendor_code": "130400353",
                    "barcode": "4006396053978",
                    "amount": 1,
                    "accepted_amount": 0,
                    "accepted_amount_present": True,
                }],
            }],
        }

        rows = product_breakdown_for_summary(report)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["inbound"], 0)

    def test_daily_report_separates_defect_shipments_in_message_and_summary_sheet(self):
        self.assertEqual(categorize_request_type("Отгрузка 3PL"), "Отгрузка")
        self.assertEqual(categorize_request_type("Отгрузка в браке"), "Отгрузка в браке")

        report = {
            "report_date": date(2026, 6, 23),
            "generated_at": datetime(2026, 6, 23, 22, 0),
            "customer_id": 6211,
            "requests": [
                {
                    "id": 101,
                    "number": "WH-R-101",
                    "category": categorize_request_type("Отгрузка 3PL"),
                    "type": "Отгрузка 3PL",
                    "products": [{
                        "name": "Chapman Brown OP 20",
                        "vendor_code": "130400353",
                        "barcode": "4006396053978",
                        "amount": 10,
                    }],
                },
                {
                    "id": 102,
                    "number": "WH-R-102",
                    "category": categorize_request_type("Отгрузка в браке"),
                    "type": "Отгрузка в браке",
                    "products": [{
                        "name": "Chapman Brown OP 20",
                        "vendor_code": "130400353",
                        "barcode": "4006396053978",
                        "amount": 3,
                    }],
                },
            ],
            "movements": [],
            "stock": {
                "total": 42,
                "rows": [{
                    "product": "Chapman Brown OP 20",
                    "vendor_code": "130400353",
                    "barcode": "4006396053978",
                    "stock": 42,
                }],
            },
            "errors": [],
        }
        report["summary"] = summarize_daily_report(report)

        summary = report["summary"]
        self.assertEqual(summary["category_counts"]["Отгрузка"], 1)
        self.assertEqual(summary["category_counts"]["Отгрузка в браке"], 1)
        self.assertEqual(summary["request_blocks_by_category"]["Отгрузка"], 10)
        self.assertEqual(summary["request_blocks_by_category"]["Отгрузка в браке"], 3)

        message = build_skladbot_daily_report_message(report)
        self.assertIn("Отгрузка: 1 заявок, 10 блоков", message)
        self.assertIn("Отгрузка в браке: 1 заявок, 3 блоков", message)
        self.assertNotIn("Отгрузка: 2 заявок", message)

        content, _filename = build_skladbot_daily_report_xlsx(report)
        workbook = openpyxl.load_workbook(BytesIO(content), data_only=False)
        summary_sheet = workbook["Сводка"]

        self.assertEqual(summary_sheet["A10"].value, "Отгрузка")
        self.assertEqual(summary_sheet["B10"].value, -10)
        self.assertEqual(summary_sheet["C10"].value, -10)
        self.assertEqual(summary_sheet["A11"].value, "Отгрузка в браке")
        self.assertEqual(summary_sheet["B11"].value, -3)
        self.assertEqual(summary_sheet["C11"].value, -3)
        self.assertEqual(summary_sheet["B8"].value, "=B13-B9-B10-B11-B12")

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
                    "category_counts": {"Отгрузка": 0, "Отгрузка в браке": 0, "Возврат": 0, "Приемка": 0, "Прочее": 0},
                    "request_blocks_by_category": {"Отгрузка": 0, "Отгрузка в браке": 0, "Возврат": 0, "Приемка": 0, "Прочее": 0},
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

    def test_scheduled_report_marks_reported_requests_after_success(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        original_session_local = telegram_worker_module.SessionLocal
        original_collect = telegram_worker_module.skladbot_daily_report.collect_skladbot_daily_report
        original_build_xlsx = telegram_worker_module.skladbot_daily_report.build_skladbot_daily_report_xlsx
        try:
            telegram_worker_module.SessionLocal = SessionLocal

            def fake_collect(report_date=None):
                return {
                    "report_date": report_date,
                    "generated_at": datetime(2026, 6, 20, 22, 0),
                    "customer_id": 6211,
                    "requests": [{
                        "id": 404,
                        "number": "WH-R-404",
                        "category": "Приемка",
                        "include_reasons": ["создана"],
                        "products": [],
                    }],
                    "movements": [],
                    "stock": {"total": 0, "rows": []},
                    "errors": [],
                    "summary": {
                        "requests_total": 1,
                        "category_counts": {"Отгрузка": 0, "Отгрузка в браке": 0, "Возврат": 0, "Приемка": 1, "Прочее": 0},
                        "request_blocks_by_category": {"Отгрузка": 0, "Отгрузка в браке": 0, "Возврат": 0, "Приемка": 0, "Прочее": 0},
                        "movement_in_amount": 0,
                        "movement_out_amount": 0,
                        "stock_total": 0,
                    },
                }

            telegram_worker_module.skladbot_daily_report.collect_skladbot_daily_report = fake_collect
            telegram_worker_module.skladbot_daily_report.build_skladbot_daily_report_xlsx = lambda report: (b"xlsx", "daily.xlsx")

            worker = TelegramWorker.__new__(TelegramWorker)
            worker.safe_send_message = lambda chat_id, text, reply_markup=None: True
            worker.safe_send_document = lambda chat_id, content, filename, caption="": {"message_id": 1}

            self.assertTrue(worker.send_skladbot_daily_report("123", report_date=date(2026, 6, 20), scheduled=True))

            with SessionLocal() as db:
                events = db.execute(select(PendingEvent)).scalars().all()

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].event_type, telegram_worker_module.SKLADBOT_DAILY_REPORTED_REQUEST_EVENT_TYPE)
            self.assertEqual((events[0].payload or {}).get("request_id"), 404)
        finally:
            telegram_worker_module.SessionLocal = original_session_local
            telegram_worker_module.skladbot_daily_report.collect_skladbot_daily_report = original_collect
            telegram_worker_module.skladbot_daily_report.build_skladbot_daily_report_xlsx = original_build_xlsx

    def test_telegram_manual_skladbot_daily_rejects_invalid_date(self):
        worker = TelegramWorker.__new__(TelegramWorker)
        worker.allowed_chat_ids = set()
        worker.admin_chat_ids = set()
        messages = []
        sends = []

        worker.safe_send_message = lambda chat_id, text, reply_markup=None: messages.append((chat_id, text))
        worker.send_skladbot_daily_report = lambda *args, **kwargs: sends.append((args, kwargs))

        worker.handle_update({
            "update_id": 1,
            "message": {"chat": {"id": 123}, "text": "/skladbot_daily bad-date"},
        })

        self.assertEqual(sends, [])
        self.assertEqual(messages[0][0], "123")
        self.assertIn("Неверная дата отчета", messages[0][1])

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

    def test_scheduled_report_runs_reconciliation_for_configured_chat(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        original_session_local = telegram_worker_module.SessionLocal
        original_reconciliation = telegram_worker_module.run_daily_reconciliation
        try:
            telegram_worker_module.SessionLocal = SessionLocal
            reconciliations = []
            telegram_worker_module.run_daily_reconciliation = (
                lambda report_date=None, alert_chat_ids=None: reconciliations.append((report_date, list(alert_chat_ids or []))) or {"status": "ok"}
            )
            worker = TelegramWorker.__new__(TelegramWorker)
            worker.skladbot_daily_report_enabled = True
            worker.skladbot_daily_report_chat_ids = {"-5271267499"}
            worker.skladbot_daily_report_hour = 22
            worker.skladbot_daily_report_minute = 0
            worker.skladbot_daily_report_retry_minutes = 15
            worker.daily_reconciliation_enabled = True
            worker.daily_reconciliation_chat_ids = set()
            sends = []
            worker.send_skladbot_daily_report = lambda chat_id, report_date=None, scheduled=False: sends.append((chat_id, report_date, scheduled)) or True

            now = datetime(2026, 6, 8, 22, 5, tzinfo=ZoneInfo("Asia/Tashkent"))
            self.assertEqual(worker.send_due_skladbot_daily_reports(now=now), 1)

            self.assertEqual(sends, [("-5271267499", date(2026, 6, 8), True)])
            self.assertEqual(reconciliations, [(date(2026, 6, 8), ["-5271267499"])])
        finally:
            telegram_worker_module.SessionLocal = original_session_local
            telegram_worker_module.run_daily_reconciliation = original_reconciliation

    def test_scheduled_report_sends_reconciliation_to_configured_private_chat(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        original_session_local = telegram_worker_module.SessionLocal
        original_reconciliation = telegram_worker_module.run_daily_reconciliation
        try:
            telegram_worker_module.SessionLocal = SessionLocal
            reconciliations = []
            telegram_worker_module.run_daily_reconciliation = (
                lambda report_date=None, alert_chat_ids=None: reconciliations.append((report_date, list(alert_chat_ids or []))) or {"status": "ok"}
            )
            worker = TelegramWorker.__new__(TelegramWorker)
            worker.skladbot_daily_report_enabled = True
            worker.skladbot_daily_report_chat_ids = {"-5271267499"}
            worker.skladbot_daily_report_hour = 22
            worker.skladbot_daily_report_minute = 0
            worker.skladbot_daily_report_retry_minutes = 15
            worker.daily_reconciliation_enabled = True
            worker.daily_reconciliation_chat_ids = {"999"}
            sends = []
            worker.send_skladbot_daily_report = lambda chat_id, report_date=None, scheduled=False: sends.append((chat_id, report_date, scheduled)) or True

            now = datetime(2026, 6, 8, 22, 5, tzinfo=ZoneInfo("Asia/Tashkent"))
            self.assertEqual(worker.send_due_skladbot_daily_reports(now=now), 1)

            self.assertEqual(sends, [("-5271267499", date(2026, 6, 8), True)])
            self.assertEqual(reconciliations, [(date(2026, 6, 8), ["999"])])
        finally:
            telegram_worker_module.SessionLocal = original_session_local
            telegram_worker_module.run_daily_reconciliation = original_reconciliation

    def test_scheduled_report_resets_stale_processing_claim(self):
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
            worker.skladbot_daily_report_hour = 22
            worker.skladbot_daily_report_minute = 0
            worker.skladbot_daily_report_retry_minutes = 15
            report_date = date(2026, 6, 8)
            key = worker.skladbot_daily_report_idempotency_key("123", report_date)
            now = datetime(2026, 6, 8, 22, 5, tzinfo=ZoneInfo("Asia/Tashkent"))
            now_utc = now.astimezone(timezone.utc)
            with SessionLocal() as db:
                db.add(PendingEvent(
                    event_type=SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
                    idempotency_key=key,
                    status="processing",
                    attempts=1,
                    payload={"chat_id": "123", "report_date": report_date.isoformat()},
                    updated_at=now_utc - timedelta(minutes=30),
                    last_error="worker died",
                ))
                db.commit()

            event_id = worker.claim_scheduled_skladbot_daily_report("123", report_date, now=now)

            with SessionLocal() as db:
                event = db.execute(select(PendingEvent)).scalar_one()

            self.assertTrue(event_id)
            self.assertEqual(event.status, "processing")
            self.assertEqual(event.attempts, 2)
            self.assertEqual(event.last_error, None)
            self.assertEqual((event.payload or {}).get("reset_reason"), "stale SkladBot daily report reset")
        finally:
            telegram_worker_module.SessionLocal = original_session_local

    def test_scheduled_report_failed_claim_respects_retry_window(self):
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
            worker.skladbot_daily_report_hour = 22
            worker.skladbot_daily_report_minute = 0
            worker.skladbot_daily_report_retry_minutes = 15
            report_date = date(2026, 6, 8)
            key = worker.skladbot_daily_report_idempotency_key("123", report_date)
            now = datetime(2026, 6, 8, 22, 5, tzinfo=ZoneInfo("Asia/Tashkent"))
            now_utc = now.astimezone(timezone.utc)
            with SessionLocal() as db:
                db.add(PendingEvent(
                    event_type=SKLADBOT_DAILY_REPORT_SEND_EVENT_TYPE,
                    idempotency_key=key,
                    status="failed",
                    attempts=1,
                    payload={"chat_id": "123", "report_date": report_date.isoformat()},
                    updated_at=now_utc - timedelta(minutes=5),
                    last_error="telegram_send_failed",
                ))
                db.commit()

            self.assertEqual(worker.claim_scheduled_skladbot_daily_report("123", report_date, now=now), "")

            retry_now = now + timedelta(minutes=16)
            event_id = worker.claim_scheduled_skladbot_daily_report("123", report_date, now=retry_now)
            self.assertTrue(event_id)
            with SessionLocal() as db:
                event = db.execute(select(PendingEvent)).scalar_one()
            self.assertEqual(event.status, "processing")
            self.assertEqual(event.attempts, 2)
            self.assertEqual(event.last_error, None)
        finally:
            telegram_worker_module.SessionLocal = original_session_local


if __name__ == "__main__":
    unittest.main()
