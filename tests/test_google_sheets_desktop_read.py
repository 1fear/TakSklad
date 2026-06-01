import re
import unittest

from taksklad import sheets
from taksklad.config import SHEET_NAME


def col_to_index(col):
    result = 0
    for char in col:
        result = result * 26 + (ord(char) - ord("A") + 1)
    return result - 1


class FakeSheet:
    def __init__(self, rows):
        self.rows = rows

    def get_all_values(self):
        return self.rows

    def ensure_size(self, row_idx, col_idx):
        while len(self.rows) <= row_idx:
            self.rows.append([])
        while len(self.rows[row_idx]) <= col_idx:
            self.rows[row_idx].append("")

    def update_cell(self, row, col, value):
        self.ensure_size(row - 1, col - 1)
        self.rows[row - 1][col - 1] = value

    def append_row(self, row, value_input_option=None):
        self.rows.append(list(row))

    def batch_update(self, updates, value_input_option=None):
        for update in updates:
            match = re.match(r"([A-Z]+)(\d+)", update["range"])
            if not match:
                continue
            start_col = col_to_index(match.group(1))
            start_row = int(match.group(2)) - 1
            for row_offset, values_row in enumerate(update.get("values") or []):
                for col_offset, value in enumerate(values_row):
                    self.ensure_size(start_row + row_offset, start_col + col_offset)
                    self.rows[start_row + row_offset][start_col + col_offset] = value

    def batch_clear(self, ranges):
        return None


class FakeSpreadsheet:
    def __init__(self, sheet):
        self.sheet = sheet

    def worksheet(self, title):
        self.requested_title = title
        return self.sheet


class FakeClient:
    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet

    def open_by_key(self, spreadsheet_id):
        return self.spreadsheet


class GoogleSheetsDesktopReadTests(unittest.TestCase):
    def setUp(self):
        self.original_get_google_client = sheets.get_google_client

    def tearDown(self):
        sheets.get_google_client = self.original_get_google_client

    def make_sheet(self):
        header = sheets.build_import_sheet_header()
        header[10] = "Цена за блок"
        header[11] = "Сумма позиции"
        header[12] = "Сумма рассчитанная"
        row = sheets.build_import_record_row({
            "Дата отгрузки": "29.05.2026",
            "Тип оплаты": "Перечисление",
            "Клиент": '"NILUFAR SANOBAR" MChJ',
            "Адрес": "Ташкент, улица Сакичмон, 10C",
            "Торговый представитель": "ОПТ",
            "Товары": "Chapman RED OP 20",
            "Кол-во ШТ": 10,
            "Кол-во блок": 1,
            "Отсканированные коды": "",
            "Статус": "Не выполнено",
        })
        row[10] = 240000
        row[11] = 3600000
        row[12] = 3600000
        return FakeSheet([header, row])

    def test_get_today_orders_recalculates_stale_line_total_from_google_blocks(self):
        sheet = self.make_sheet()
        spreadsheet = FakeSpreadsheet(sheet)
        sheets.get_google_client = lambda: FakeClient(spreadsheet)

        orders, loaded_sheet = sheets.get_today_orders(apply_skladbot_filter=False)

        self.assertIs(loaded_sheet, sheet)
        self.assertEqual(spreadsheet.requested_title, SHEET_NAME)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["Кол-во блок"], "1")
        self.assertEqual(orders[0]["Цена за блок"], 240000)
        self.assertEqual(orders[0]["Сумма позиции"], 240000)
        self.assertEqual(orders[0]["Сумма рассчитанная"], 240000)
        self.assertEqual(sheet.rows[1][10], 240000)
        self.assertEqual(sheet.rows[1][11], 240000)
        self.assertEqual(sheet.rows[1][12], 240000)


if __name__ == "__main__":
    unittest.main()
