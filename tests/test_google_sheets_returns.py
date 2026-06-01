import re
import unittest

from taksklad import sheets
from taksklad.config import ARCHIVE_SHEET_NAME, RETURNS_SHEET_NAME


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


class FakeClient:
    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet

    def open_by_key(self, spreadsheet_id):
        return self.spreadsheet


class GoogleSheetsReturnsTests(unittest.TestCase):
    def setUp(self):
        header = sheets.build_import_sheet_header()
        row = sheets.build_import_record_row({
            "Дата отгрузки": "31.05.2026",
            "Тип оплаты": "Перечисление",
            "Клиент": "Client One",
            "Адрес": "Tashkent, Test 1",
            "Торговый представитель": "Rep One",
            "Товары": "Chapman RED OP 20",
            "Кол-во ШТ": 20,
            "Кол-во блок": 2,
            "Отсканированные коды": "0101\n0102",
            "Статус": "Выполнено",
            "ID заказа": "order-1",
            "ID импорта": "import-1",
            "Номер заявки SkladBot": "WH-R-1",
            "ID заявки SkladBot": "1",
        })
        self.archive_sheet = FakeSheet(ARCHIVE_SHEET_NAME, [header, row])
        self.returns_sheet = FakeSheet(RETURNS_SHEET_NAME, [header.copy()])
        self.spreadsheet = FakeSpreadsheet({
            ARCHIVE_SHEET_NAME: self.archive_sheet,
            RETURNS_SHEET_NAME: self.returns_sheet,
        })
        self.original_get_google_client = sheets.get_google_client
        sheets.get_google_client = lambda: FakeClient(self.spreadsheet)

    def tearDown(self):
        sheets.get_google_client = self.original_get_google_client

    def test_lookup_return_order_reads_completed_order_from_archive(self):
        order = sheets.lookup_return_order_in_gsheet("WH-R-1")

        self.assertEqual(order["source"], "google_sheets")
        self.assertEqual(order["client"], "Client One")
        self.assertEqual(order["skladbot_request_number"], "WH-R-1")
        self.assertEqual(order["items"][0]["quantity_blocks"], 2)
        self.assertEqual(order["_row_numbers"], [2])

    def test_mark_return_updates_archive_and_copies_row_to_returns(self):
        order = sheets.lookup_return_order_in_gsheet("1")

        updated = sheets.mark_return_order_in_gsheet(order, return_reference="WH-R-1", returned_by="tester")

        self.assertEqual(updated["status"], "returned")
        archive_header = self.archive_sheet.rows[0]
        status_idx = archive_header.index(sheets.RETURN_STATUS_COLUMN)
        date_idx = archive_header.index(sheets.RETURN_DATE_COLUMN)
        reference_idx = archive_header.index(sheets.RETURN_REFERENCE_COLUMN)
        actor_idx = archive_header.index(sheets.RETURNED_BY_COLUMN)
        self.assertEqual(self.archive_sheet.rows[1][status_idx], "Возврат")
        self.assertTrue(self.archive_sheet.rows[1][date_idx])
        self.assertEqual(self.archive_sheet.rows[1][reference_idx], "WH-R-1")
        self.assertEqual(self.archive_sheet.rows[1][actor_idx], "tester")
        self.assertEqual(len(self.returns_sheet.rows), 2)
        self.assertEqual(self.returns_sheet.rows[1][status_idx], "Возврат")

    def test_fetch_returned_orders_reads_returns_sheet(self):
        order = sheets.lookup_return_order_in_gsheet("WH-R-1")
        sheets.mark_return_order_in_gsheet(order, return_reference="WH-R-1", returned_by="tester")

        returned_orders = sheets.fetch_returned_orders_from_gsheet()

        self.assertEqual(len(returned_orders), 1)
        self.assertEqual(returned_orders[0]["return_status"], "returned")
        self.assertEqual(returned_orders[0]["return_reference"], "WH-R-1")


if __name__ == "__main__":
    unittest.main()
