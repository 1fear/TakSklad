import re
import unittest

from taksklad import sheets
from taksklad.config import ARCHIVE_SHEET_NAME


def col_to_index(col):
    result = 0
    for char in col:
        result = result * 26 + (ord(char) - ord("A") + 1)
    return result - 1


class FakeSheet:
    def __init__(self, title, rows):
        self.title = title
        self.rows = rows
        self.spreadsheet = None

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

    def delete_rows(self, row_number):
        del self.rows[row_number - 1]


class FakeSpreadsheet:
    def __init__(self, sheets_by_title):
        self.sheets_by_title = sheets_by_title
        for sheet in sheets_by_title.values():
            sheet.spreadsheet = self

    def worksheet(self, title):
        return self.sheets_by_title[title]

    def add_worksheet(self, title, rows=1000, cols=32):
        sheet = FakeSheet(title, [])
        sheet.spreadsheet = self
        self.sheets_by_title[title] = sheet
        return sheet


class GoogleSheetsArchiveTests(unittest.TestCase):
    def make_row(self, order_id, import_id, client, product, status="Не выполнено"):
        return sheets.build_import_record_row({
            "Дата отгрузки": "31.05.2026",
            "Тип оплаты": "Перечисление",
            "Клиент": client,
            "Адрес": "Tashkent, Test 1",
            "Торговый представитель": "Rep One",
            "Товары": product,
            "Кол-во ШТ": 20,
            "Кол-во блок": 2,
            "Отсканированные коды": "0101\n0102",
            "Статус": status,
            "ID заказа": order_id,
            "ID импорта": import_id,
        })

    def make_sheets(self, data_rows, archive_rows=None):
        header = sheets.build_import_sheet_header()
        data_sheet = FakeSheet("data", [header.copy(), *data_rows])
        archive_sheet = FakeSheet(ARCHIVE_SHEET_NAME, [header.copy(), *(archive_rows or [])])
        spreadsheet = FakeSpreadsheet({
            "data": data_sheet,
            ARCHIVE_SHEET_NAME: archive_sheet,
        })
        return data_sheet, archive_sheet, spreadsheet

    def test_archive_order_group_moves_rows_from_data_to_archive(self):
        row_one = self.make_row("order-1", "import-1", "Client One", "Chapman RED OP 20")
        row_two = self.make_row("order-2", "import-2", "Client One", "Chapman Brown OP 20")
        other_row = self.make_row("order-3", "import-3", "Other Client", "Chapman Gold SSL 100`20")
        data_sheet, archive_sheet, _ = self.make_sheets([row_one, row_two, other_row])

        ok, message = sheets.archive_order_group_to_gsheet(data_sheet, [
            {"ID заказа": "order-1", "_row_number": 2},
            {"ID заказа": "order-2", "_row_number": 99},
        ])

        self.assertTrue(ok, message)
        self.assertEqual([row[sheets.SERVICE_COLUMN_START_INDEX] for row in archive_sheet.rows[1:]], ["order-1", "order-2"])
        status_idx = archive_sheet.rows[0].index("Статус")
        self.assertEqual(archive_sheet.rows[1][status_idx], "Выполнено")
        self.assertEqual(archive_sheet.rows[2][status_idx], "Выполнено")
        self.assertEqual(len(data_sheet.rows), 2)
        self.assertEqual(data_sheet.rows[1][sheets.SERVICE_COLUMN_START_INDEX], "order-3")

    def test_archive_order_group_does_not_duplicate_already_archived_row(self):
        row_one = self.make_row("order-1", "import-1", "Client One", "Chapman RED OP 20")
        data_sheet, archive_sheet, _ = self.make_sheets([row_one], [row_one.copy()])

        ok, message = sheets.archive_order_group_to_gsheet(data_sheet, [
            {"ID заказа": "order-1", "_row_number": 2},
        ])

        self.assertTrue(ok, message)
        self.assertEqual(len(archive_sheet.rows), 2)
        self.assertEqual(len(data_sheet.rows), 1)


if __name__ == "__main__":
    unittest.main()
