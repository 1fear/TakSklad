import unittest
from datetime import date

from taksklad.config import (
    ORDER_DATE_COLUMN,
    SKLADBOT_CHECKED_AT_COLUMN,
    SKLADBOT_REQUEST_ID_COLUMN,
    SKLADBOT_REQUEST_NUMBER_COLUMN,
    SKLADBOT_STATUS_COLUMN,
    SKLADBOT_STATUS_FOUND,
    SKLADBOT_STATUS_MULTIPLE,
    SKLADBOT_STATUS_NOT_FOUND,
    STATUS_COLUMN,
    STATUS_NOT_COMPLETED,
)
from taksklad.skladbot import (
    fetch_candidate_requests,
    format_skladbot_error,
    load_skladbot_settings,
    product_names_match,
    request_type_matches,
)
from taksklad import skladbot_sync
from taksklad.skladbot_sync import sync_skladbot_request_numbers
from taksklad.utils import column_index_to_letter


class FakeSheet:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []

    def get_all_values(self):
        return self.rows

    def batch_update(self, updates, value_input_option=None):
        self.updates.extend(updates)
        for update in updates:
            cell = update["range"]
            value = update["values"][0][0]
            col_letters = "".join(ch for ch in cell if ch.isalpha())
            row_number = int("".join(ch for ch in cell if ch.isdigit()))
            col_idx = 0
            for ch in col_letters:
                col_idx = col_idx * 26 + (ord(ch.upper()) - 64)
            col_idx -= 1
            while len(self.rows[row_number - 1]) <= col_idx:
                self.rows[row_number - 1].append("")
            self.rows[row_number - 1][col_idx] = value


class FailingReadSheet:
    def get_all_values(self):
        raise RuntimeError("Google timeout")


class FailingWriteSheet(FakeSheet):
    def batch_update(self, updates, value_input_option=None):
        raise RuntimeError("Google write timeout")


def header():
    return [
        ORDER_DATE_COLUMN,
        "Тип оплаты",
        "Клиент",
        "Адрес",
        "Торговый представитель",
        "Товары",
        "Кол-во ШТ",
        "Кол-во блок",
        "Отсканированные коды",
        STATUS_COLUMN,
        SKLADBOT_REQUEST_NUMBER_COLUMN,
        SKLADBOT_REQUEST_ID_COLUMN,
        SKLADBOT_STATUS_COLUMN,
        SKLADBOT_CHECKED_AT_COLUMN,
    ]


def order_row(product, quantity, blocks):
    return [
        "25.05.2026",
        "ПЕРЕЧИСЛЕНИЕ",
        '"MARKET AL-KABIR" MChJ',
        "19-й квартал, 18, массив Юнусабад, Юнусабадский район, Ташкент",
        "ТП1",
        product,
        quantity,
        blocks,
        "",
        STATUS_NOT_COMPLETED,
        "",
        "",
        "",
        "",
    ]


def request(number="WH-R-189337", request_id=189337):
    return {
        "id": request_id,
        "number": number,
        "customer_name": "ООО Bastion Import Chapman MCHJ",
        "type": "Отгрузка 3PL",
        "is_completed": False,
        "archived": False,
        "created_at": "22.05.2026",
        "unloading_date": "25.05.2026",
        "recipient": '"MARKET AL-KABIR" MChJ',
        "address": "19-й квартал, 18, массив Юнусабад, Юнусабадский район, Ташкент",
        "comment": "ПЕРЕЧИСЛЕНИЕ",
        "products": [
            {
                "name": "Chapman Brown OP 20 UZ - KingSize",
                "vendor_code": "CHPMBrownOP20UZ",
                "barcode": "4006396053978",
                "amount": 1,
            },
            {
                "name": "Chapman Gold SSL 20 UZ - SuperSlim",
                "vendor_code": "CHPMGoldSSL20UZ",
                "barcode": "4006396054005",
                "amount": 2,
            },
        ],
    }


class SkladBotSyncTests(unittest.TestCase):
    def test_product_match_accepts_concatenated_vendor_code(self):
        self.assertTrue(product_names_match("Chapman Brown OP 20", {"vendor_code": "CHPMBrownOP20UZ"}))
        self.assertTrue(product_names_match("Chapman Brown SSL 100`20", {"vendor_code": "CHPMBrownSSL20UZ"}))
        self.assertTrue(product_names_match("Chapman Gold SSL 100`20", {"vendor_code": "CHPMGoldSSL20UZ"}))
        self.assertTrue(product_names_match("Chapman Green OP 20", {"vendor_code": "CHPMGreenOP20UZ"}))
        self.assertTrue(product_names_match("Chapman RED SSL 100 20", {"vendor_code": "CHPMRedSSL20UZ"}))
        self.assertFalse(product_names_match("Chapman Brown OP 20", {"vendor_code": "CHPMRedOP20UZ"}))
        self.assertFalse(product_names_match("Chapman Brown SSL 100`20", {"vendor_code": "CHPMBrownOP20UZ"}))

    def test_writes_request_number_when_one_exact_match_exists(self):
        sheet = FakeSheet([
            header(),
            order_row("Chapman Brown OP 20", 10, 1),
            order_row("Chapman Gold SSL 20", 20, 2),
        ])

        result = sync_skladbot_request_numbers(sheet, candidate_requests=[request()])

        self.assertEqual(result["matched"], 1)
        self.assertEqual(sheet.rows[1][10], "WH-R-189337")
        self.assertEqual(sheet.rows[2][10], "WH-R-189337")
        self.assertEqual(sheet.rows[1][12], SKLADBOT_STATUS_FOUND)

    def test_matches_when_skladbot_request_contains_extra_products(self):
        sheet = FakeSheet([
            header(),
            order_row("Chapman Brown OP 20", 10, 1),
        ])

        result = sync_skladbot_request_numbers(sheet, candidate_requests=[request()])

        self.assertEqual(result["matched"], 1)
        self.assertEqual(sheet.rows[1][10], "WH-R-189337")
        self.assertEqual(sheet.rows[1][12], SKLADBOT_STATUS_FOUND)

    def test_marks_multiple_when_partial_match_is_ambiguous(self):
        sheet = FakeSheet([
            header(),
            order_row("Chapman Brown OP 20", 10, 1),
        ])

        first = request("WH-R-189337", 189337)
        second = request("WH-R-189338", 189338)
        second["products"] = [
            {
                "name": "Chapman Brown OP 20 UZ - KingSize",
                "vendor_code": "CHPMBrownOP20UZ",
                "barcode": "4006396053978",
                "amount": 1,
            },
            {
                "name": "Chapman Red SSL 20 UZ - SuperSlim",
                "vendor_code": "CHPMRedSSL20UZ",
                "barcode": "4006396054006",
                "amount": 4,
            },
        ]

        result = sync_skladbot_request_numbers(sheet, candidate_requests=[first, second])

        self.assertEqual(result["multiple"], 1)
        self.assertEqual(sheet.rows[1][10], "")
        self.assertEqual(sheet.rows[1][12], SKLADBOT_STATUS_MULTIPLE)

    def test_dry_run_does_not_write_request_number(self):
        sheet = FakeSheet([
            header(),
            order_row("Chapman Brown OP 20", 10, 1),
            order_row("Chapman Gold SSL 20", 20, 2),
        ])

        result = sync_skladbot_request_numbers(
            sheet,
            candidate_requests=[request()],
            dry_run=True,
        )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["updated"], 0)
        self.assertGreater(result["would_update"], 0)
        self.assertEqual(sheet.updates, [])
        self.assertEqual(sheet.rows[1][10], "")

    def test_skladbot_timeout_gets_actionable_message(self):
        message = format_skladbot_error(RuntimeError("timed out"))

        self.assertIn("SkladBot временно недоступен", message)
        self.assertIn("номера заявок подтянутся позже", message)

    def test_skladbot_auth_error_gets_actionable_message(self):
        message = format_skladbot_error(RuntimeError("SkladBot HTTP 401: unauthorized"))

        self.assertIn("API-токен", message)

    def test_read_failure_does_not_raise(self):
        result = sync_skladbot_request_numbers(FailingReadSheet(), candidate_requests=[request()])

        self.assertEqual(result["errors"], 1)
        self.assertIn("Google Sheets", result["message"])

    def test_write_failure_does_not_raise_or_block_orders(self):
        sheet = FailingWriteSheet([
            header(),
            order_row("Chapman Brown OP 20", 10, 1),
            order_row("Chapman Gold SSL 20", 20, 2),
        ])

        result = sync_skladbot_request_numbers(sheet, candidate_requests=[request()])

        self.assertGreater(result["errors"], 0)
        self.assertEqual(result["updated"], 0)
        self.assertIn("Google Sheets временно не принял запись", result["message"])

    def test_marks_not_found_without_guessing(self):
        sheet = FakeSheet([
            header(),
            order_row("Chapman Brown OP 20", 10, 1),
        ])

        result = sync_skladbot_request_numbers(sheet, candidate_requests=[])

        self.assertEqual(result["not_found"], 1)
        self.assertEqual(sheet.rows[1][10], "")
        self.assertEqual(sheet.rows[1][12], SKLADBOT_STATUS_NOT_FOUND)

    def test_marks_multiple_matches_without_writing_number(self):
        sheet = FakeSheet([
            header(),
            order_row("Chapman Brown OP 20", 10, 1),
            order_row("Chapman Gold SSL 20", 20, 2),
        ])

        result = sync_skladbot_request_numbers(sheet, candidate_requests=[
            request("WH-R-189337", 189337),
            request("WH-R-189338", 189338),
        ])

        self.assertEqual(result["multiple"], 1)
        self.assertEqual(sheet.rows[1][10], "")
        self.assertEqual(sheet.rows[1][12], SKLADBOT_STATUS_MULTIPLE)

    def test_does_not_match_request_with_different_unloading_date(self):
        # Регрессия: дата отгрузки в data и дата выгрузки в SkladBot
        # должны строго совпадать. Заявка того же клиента, но за другой день
        # не должна попадать в матчинг.
        wrong_day_request = request("WH-R-189337", 189337)
        wrong_day_request["unloading_date"] = "24.05.2026"

        sheet = FakeSheet([
            header(),
            order_row("Chapman Brown OP 20", 10, 1),
            order_row("Chapman Gold SSL 20", 20, 2),
        ])

        result = sync_skladbot_request_numbers(sheet, candidate_requests=[wrong_day_request])

        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["not_found"], 1)
        self.assertEqual(sheet.rows[1][10], "")
        self.assertEqual(sheet.rows[1][12], SKLADBOT_STATUS_NOT_FOUND)

    def test_does_not_match_request_when_unloading_date_is_missing(self):
        # Регрессия: если дата выгрузки в SkladBot пуста (или пуста дата в data),
        # привязка делаться не должна, даже если всё остальное совпадает.
        no_date_request = request("WH-R-189337", 189337)
        no_date_request["unloading_date"] = ""

        sheet = FakeSheet([
            header(),
            order_row("Chapman Brown OP 20", 10, 1),
            order_row("Chapman Gold SSL 20", 20, 2),
        ])

        result = sync_skladbot_request_numbers(sheet, candidate_requests=[no_date_request])

        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["not_found"], 1)
        self.assertEqual(sheet.rows[1][10], "")

    def test_matches_request_when_client_quotes_differ(self):
        # Регрессия: после введения строгого сравнения клиента появилась
        # обратная проблема — заявки переставали матчиться, потому что
        # в Excel клиент с кавычками '"MARKET AL-KABIR" MChJ', а SkladBot
        # отдаёт без кавычек или с типографскими «»“”. normalize_company_name
        # должен снимать различия по пунктуации, но сохранять различия по
        # словам и цифрам.
        from taksklad.skladbot import normalize_company_name
        self.assertEqual(
            normalize_company_name('"MARKET AL-KABIR" MChJ'),
            normalize_company_name('MARKET AL-KABIR MChJ'),
        )
        self.assertEqual(
            normalize_company_name('«MARKET AL-KABIR» MChJ'),
            normalize_company_name('"MARKET AL-KABIR" MChJ'),
        )
        # Разные слова — разные результаты:
        self.assertNotEqual(
            normalize_company_name('"MARKET AL-KABIR" MChJ'),
            normalize_company_name('"MARKET AL-KEBIR" MChJ'),
        )

        # Полная регрессия: заявка с тем же клиентом, но без кавычек,
        # теперь матчится с группой, где клиент с кавычками.
        no_quotes_request = request("WH-R-189337", 189337)
        no_quotes_request["recipient"] = "MARKET AL-KABIR MChJ"

        sheet = FakeSheet([
            header(),
            order_row("Chapman Brown OP 20", 10, 1),
            order_row("Chapman Gold SSL 20", 20, 2),
        ])

        result = sync_skladbot_request_numbers(sheet, candidate_requests=[no_quotes_request])

        self.assertEqual(result["matched"], 1)
        self.assertEqual(sheet.rows[1][10], "WH-R-189337")
        self.assertEqual(sheet.rows[2][10], "WH-R-189337")

    def test_does_not_match_request_from_different_client(self):
        # Регрессия на баг "номер заявки сползает к соседнему клиенту":
        # у двух клиентов на 26.05 совпали адрес, оплата и количество товара.
        # Раньше fuzzy токен-матч клиента ошибочно привязывал заявку соседа.
        # Сейчас сравнение клиента строгое — чужая заявка матчиться не должна.
        foreign_request = request("WH-R-189871", 189871)
        foreign_request["recipient"] = '"DAILY MART GROUP" MCHJ'
        foreign_request["products"] = [
            {
                "name": "Chapman Brown OP 20 UZ - KingSize",
                "vendor_code": "CHPMBrownOP20UZ",
                "barcode": "4006396053978",
                "amount": 1,
            },
        ]

        sheet = FakeSheet([
            header(),
            order_row("Chapman Brown OP 20", 10, 1),
        ])

        result = sync_skladbot_request_numbers(sheet, candidate_requests=[foreign_request])

        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["not_found"], 1)
        self.assertEqual(sheet.rows[1][10], "")
        self.assertEqual(sheet.rows[1][12], SKLADBOT_STATUS_NOT_FOUND)

    def test_matches_request_when_address_differs(self):
        different_address_request = request("WH-R-189337", 189337)
        different_address_request["address"] = "Совсем другой адрес из SkladBot"

        sheet = FakeSheet([
            header(),
            order_row("Chapman Brown OP 20", 10, 1),
            order_row("Chapman Gold SSL 20", 20, 2),
        ])

        result = sync_skladbot_request_numbers(sheet, candidate_requests=[different_address_request])

        self.assertEqual(result["matched"], 1)
        self.assertEqual(sheet.rows[1][10], "WH-R-189337")
        self.assertEqual(sheet.rows[1][12], SKLADBOT_STATUS_FOUND)

    def test_matches_request_type_when_words_are_reordered(self):
        reordered_type_request = request("WH-R-189337", 189337)
        reordered_type_request["type"] = "3PL отгрузка"

        sheet = FakeSheet([
            header(),
            order_row("Chapman Brown OP 20", 10, 1),
            order_row("Chapman Gold SSL 20", 20, 2),
        ])

        result = sync_skladbot_request_numbers(sheet, candidate_requests=[reordered_type_request])

        self.assertEqual(result["matched"], 1)
        self.assertEqual(sheet.rows[1][10], "WH-R-189337")

    def test_rejects_return_request_type_even_if_it_mentions_shipment(self):
        self.assertFalse(request_type_matches("Возврат 3PL отгрузка"))

    def test_api_failure_does_not_overwrite_sheet_statuses(self):
        sheet = FakeSheet([
            header(),
            order_row("Chapman Brown OP 20", 10, 1),
        ])
        original_fetch = skladbot_sync.fetch_candidate_requests
        try:
            def fail_fetch(settings=None):
                raise RuntimeError("temporary skladbot failure")

            skladbot_sync.fetch_candidate_requests = fail_fetch
            result = sync_skladbot_request_numbers(
                sheet,
                settings={"enabled": True, "api_token": "token"},
            )

            self.assertEqual(result["updated"], 0)
            self.assertEqual(result["errors"], 1)
            self.assertEqual(sheet.updates, [])
            self.assertEqual(sheet.rows[1][12], "")
        finally:
            skladbot_sync.fetch_candidate_requests = original_fetch

    def test_fetches_details_only_for_today_and_yesterday_requests(self):
        class FakeClient:
            def __init__(self):
                self.detail_ids = []

            def list_requests(self, customer_id, type_id, limit=500):
                return [
                    {
                        "id": 1,
                        "delivery_number": "WH-R-1",
                        "created_at": "2026-05-27",
                        "customer": "ООО Bastion Import Chapman MCHJ",
                        "type": "Отгрузка 3PL",
                        "is_completed": 0,
                        "archived": 0,
                    },
                    {
                        "id": 2,
                        "delivery_number": "WH-R-2",
                        "created_at": "2026-05-26",
                        "customer": "ООО Bastion Import Chapman MCHJ",
                        "type": "Отгрузка 3PL",
                        "is_completed": 1,
                        "archived": 1,
                    },
                    {
                        "id": 3,
                        "delivery_number": "WH-R-3",
                        "created_at": "2026-05-25",
                        "customer": "ООО Bastion Import Chapman MCHJ",
                        "type": "Отгрузка 3PL",
                        "is_completed": 0,
                        "archived": 0,
                    },
                ]

            def get_request_detail(self, request_id):
                self.detail_ids.append(request_id)
                unloading_date = "2026-05-27" if request_id == 1 else "2026-05-26"
                return {
                    "id": request_id,
                    "delivery_number": f"WH-R-{request_id}",
                    "customer": {"name": "ООО Bastion Import Chapman MCHJ"},
                    "type": "Отгрузка 3PL",
                    "createdAt": unloading_date,
                    "fields": [
                        {"field": "unloading_date", "value": unloading_date},
                    ],
                }

        client = FakeClient()
        result = fetch_candidate_requests(
            settings={
                "enabled": True,
                "api_token": "token",
                "customer_id": 6211,
                "customer_name": "ООО Bastion Import Chapman MCHJ",
                "shipment_type_id": 3389,
                "shipment_type_name": "Отгрузка 3PL",
                "requests_limit": 500,
                "completed_detail_limit": 500,
                "sync_lookback_days": 1,
                "request_delay_seconds": 0,
            },
            client=client,
            today=date(2026, 5, 27),
        )

        self.assertEqual(client.detail_ids, [1, 2])
        self.assertEqual([item["id"] for item in result], [1, 2])

    def test_load_settings_keeps_minimum_skladbot_request_limit(self):
        original_load_data_section = load_skladbot_settings.__globals__["load_data_section"]
        try:
            def fake_load_data_section(section, default=None):
                if section == "skladbot_settings":
                    return {
                        "enabled": True,
                        "api_token": "token",
                        "requests_limit": 100,
                        "completed_detail_limit": 25,
                        "request_delay_seconds": 0.05,
                    }
                return default

            load_skladbot_settings.__globals__["load_data_section"] = fake_load_data_section
            settings = load_skladbot_settings()

            self.assertEqual(settings["requests_limit"], 500)
            self.assertEqual(settings["completed_detail_limit"], 25)
            self.assertGreaterEqual(settings["request_delay_seconds"], 2.0)
        finally:
            load_skladbot_settings.__globals__["load_data_section"] = original_load_data_section


if __name__ == "__main__":
    unittest.main()
