import unittest

from config import (
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
from skladbot_sync import sync_skladbot_request_numbers
from utils import column_index_to_letter


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


if __name__ == "__main__":
    unittest.main()
