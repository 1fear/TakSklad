import re
import unittest

from taksklad import sheets
from taksklad.config import SHEET_NAME
from taksklad.utils import validate_kiz_code


def col_to_index(col):
    result = 0
    for char in col:
        result = result * 26 + (ord(char) - ord("A") + 1)
    return result - 1


class FakeSheet:
    def __init__(self, rows):
        self.rows = rows
        self.batch_update_options = []

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
        self.batch_update_options.append(value_input_option)
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

    def test_get_today_orders_reopens_incomplete_row_with_stale_completed_status(self):
        sheet = self.make_sheet()
        status_idx = sheet.rows[0].index("Статус")
        sheet.rows[1][status_idx] = "Выполнено"
        spreadsheet = FakeSpreadsheet(sheet)
        sheets.get_google_client = lambda: FakeClient(spreadsheet)

        orders, _loaded_sheet = sheets.get_today_orders(apply_skladbot_filter=False)

        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["Статус"], "Не выполнено")
        self.assertEqual(sheet.rows[1][status_idx], "Не выполнено")

    def test_get_today_orders_keeps_group_when_only_one_position_completed(self):
        header = sheets.build_import_sheet_header()
        completed_row = sheets.build_import_record_row({
            "Дата отгрузки": "29.05.2026",
            "Тип оплаты": "Перечисление",
            "Клиент": "Client",
            "Адрес": "Address",
            "Торговый представитель": "Rep",
            "Товары": "Chapman RED OP 20",
            "Кол-во ШТ": 10,
            "Кол-во блок": 1,
            "Отсканированные коды": "01012345678901234567RED",
            "Статус": "Выполнено",
            "ID заказа": "order-1",
            "ID импорта": "import-red",
        })
        incomplete_row = sheets.build_import_record_row({
            "Дата отгрузки": "29.05.2026",
            "Тип оплаты": "Перечисление",
            "Клиент": "Client",
            "Адрес": "Address",
            "Торговый представитель": "Rep",
            "Товары": "Chapman Brown OP 20",
            "Кол-во ШТ": 10,
            "Кол-во блок": 1,
            "Отсканированные коды": "",
            "Статус": "Не выполнено",
            "ID заказа": "order-1",
            "ID импорта": "import-brown",
        })
        sheet = FakeSheet([header, completed_row, incomplete_row])
        spreadsheet = FakeSpreadsheet(sheet)
        sheets.get_google_client = lambda: FakeClient(spreadsheet)

        orders, _loaded_sheet = sheets.get_today_orders(apply_skladbot_filter=False)

        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["Товары"], "Chapman Brown OP 20")

    def test_update_scanned_codes_writes_kiz_cell_as_raw(self):
        sheet = self.make_sheet()
        spreadsheet = FakeSpreadsheet(sheet)
        sheets.get_google_client = lambda: FakeClient(spreadsheet)
        orders, _ = sheets.get_today_orders(apply_skladbot_filter=False)

        ok, message = sheets.update_scanned_codes_to_gsheet(
            sheet,
            orders[0],
            ["01012345678901234567ABC,DEF"],
        )

        self.assertTrue(ok, message)
        self.assertEqual(sheet.batch_update_options[-1], "RAW")
        codes_idx = sheet.rows[0].index("Отсканированные коды")
        self.assertEqual(sheet.rows[1][codes_idx], "01012345678901234567ABC,DEF")

    def test_kiz_line_breaks_are_rejected_by_scanner_validation_and_split_as_rows(self):
        is_valid, message, _code = validate_kiz_code("01012345678901234567A\n01012345678901234567B")

        self.assertFalse(is_valid)
        self.assertIn("переносы", message)
        self.assertEqual(
            sheets.split_codes("01012345678901234567A\n01012345678901234567B"),
            ["01012345678901234567A", "01012345678901234567B"],
        )

    def test_update_scanned_codes_does_not_use_shared_order_id_for_wrong_sku(self):
        header = sheets.build_import_sheet_header()
        shared_order_id = "shared-order"
        brown_row = sheets.build_import_record_row({
            "Дата отгрузки": "29.05.2026",
            "Тип оплаты": "Перечисление",
            "Клиент": "Client",
            "Адрес": "Address",
            "Торговый представитель": "Rep",
            "Товары": "Chapman Brown OP 20",
            "Кол-во ШТ": 10,
            "Кол-во блок": 1,
            "Отсканированные коды": "",
            "Статус": "Не выполнено",
            "ID заказа": shared_order_id,
            "ID импорта": "brown-import",
        })
        red_row = sheets.build_import_record_row({
            "Дата отгрузки": "29.05.2026",
            "Тип оплаты": "Перечисление",
            "Клиент": "Client",
            "Адрес": "Address",
            "Торговый представитель": "Rep",
            "Товары": "Chapman RED OP 20",
            "Кол-во ШТ": 10,
            "Кол-во блок": 1,
            "Отсканированные коды": "",
            "Статус": "Не выполнено",
            "ID заказа": shared_order_id,
            "ID импорта": "red-import",
        })
        sheet = FakeSheet([header, brown_row, red_row])
        order = {
            "Дата отгрузки": "29.05.2026",
            "Тип оплаты": "Перечисление",
            "Клиент": "Client",
            "Адрес": "Address",
            "Торговый представитель": "Rep",
            "Товары": "Chapman RED OP 20",
            "Кол-во ШТ": 10,
            "Кол-во блок": 1,
            "ID заказа": shared_order_id,
            "_row_number": 2,
        }

        ok, message = sheets.update_scanned_codes_to_gsheet(sheet, order, ["01012345678901234567RED"])

        self.assertTrue(ok, message)
        codes_idx = sheet.rows[0].index("Отсканированные коды")
        self.assertEqual(sheet.rows[1][codes_idx], "")
        self.assertEqual(sheet.rows[2][codes_idx], "01012345678901234567RED")

    def test_update_scanned_codes_can_clear_active_row_for_undo(self):
        sheet = self.make_sheet()
        codes_idx = sheet.rows[0].index("Отсканированные коды")
        status_idx = sheet.rows[0].index("Статус")
        sheet.rows[1][codes_idx] = "01012345678901234567OLD"
        sheet.rows[1][status_idx] = "Выполнено"
        order = {
            "Дата отгрузки": "29.05.2026",
            "Тип оплаты": "Перечисление",
            "Клиент": '"NILUFAR SANOBAR" MChJ',
            "Адрес": "Ташкент, улица Сакичмон, 10C",
            "Торговый представитель": "ОПТ",
            "Товары": "Chapman RED OP 20",
            "Кол-во ШТ": 10,
            "Кол-во блок": 1,
            "_row_number": 2,
        }

        ok, message = sheets.update_scanned_codes_to_gsheet(sheet, order, [], allow_empty=True)

        self.assertTrue(ok, message)
        self.assertEqual(sheet.rows[1][codes_idx], "")
        self.assertEqual(sheet.rows[1][status_idx], "Не выполнено")


if __name__ == "__main__":
    unittest.main()
