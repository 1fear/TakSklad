import unittest

from taksklad import excel_import
from taksklad.config import SHEET_NAME


class FakeSheet:
    def __init__(self):
        self.rows = [["Дата отгрузки"]]
        self.batch_updates = []

    def get_all_values(self):
        return self.rows

    def batch_update(self, updates, value_input_option=None):
        self.batch_updates.append((updates, value_input_option))


class FakeSpreadsheet:
    def __init__(self, sheet):
        self.sheet = sheet
        self.requested_titles = []

    def worksheet(self, title):
        self.requested_titles.append(title)
        if title != SHEET_NAME:
            raise AssertionError(f"unexpected worksheet access: {title}")
        return self.sheet


class FakeClient:
    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet

    def open_by_key(self, spreadsheet_id):
        return self.spreadsheet


class ExcelImportAppendTests(unittest.TestCase):
    def test_append_import_records_writes_only_data_sheet(self):
        sheet = FakeSheet()
        spreadsheet = FakeSpreadsheet(sheet)

        original_get_google_client = excel_import.get_google_client
        original_ensure_layout = excel_import.ensure_import_sheet_layout
        original_get_import_keys = excel_import.get_existing_import_keys
        original_get_duplicate_keys = excel_import.get_existing_order_duplicate_keys
        original_build_row = excel_import.build_import_record_row
        original_save_data_section = excel_import.save_data_section
        original_load_data_section = excel_import.load_data_section
        try:
            excel_import.get_google_client = lambda: FakeClient(spreadsheet)
            excel_import.ensure_import_sheet_layout = lambda sheet: None
            excel_import.get_existing_import_keys = lambda rows: (set(), set())
            excel_import.get_existing_order_duplicate_keys = lambda rows: set()
            excel_import.build_import_record_row = lambda record: ["25.05.2026", record["Клиент"]]
            excel_import.load_data_section = lambda key, default=None: []
            excel_import.save_data_section = lambda key, value: None

            result = excel_import.append_import_records([{
                "ID импорта": "import-1",
                "ID заказа": "order-1",
                "Клиент": "Test Client",
            }])
        finally:
            excel_import.get_google_client = original_get_google_client
            excel_import.ensure_import_sheet_layout = original_ensure_layout
            excel_import.get_existing_import_keys = original_get_import_keys
            excel_import.get_existing_order_duplicate_keys = original_get_duplicate_keys
            excel_import.build_import_record_row = original_build_row
            excel_import.save_data_section = original_save_data_section
            excel_import.load_data_section = original_load_data_section

        self.assertEqual(result, {"imported": 1, "duplicates": 0})
        self.assertEqual(spreadsheet.requested_titles, [SHEET_NAME])
        self.assertEqual(len(sheet.batch_updates), 1)


if __name__ == "__main__":
    unittest.main()
