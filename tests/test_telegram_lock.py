import unittest

import sheets


class FakeWorksheet:
    def __init__(self, rows=None):
        self.rows = [list(row) for row in (rows or [])]

    def get_all_values(self):
        return [row.copy() for row in self.rows]

    def batch_update(self, updates, value_input_option=None):
        for update in updates:
            cell_range = update["range"]
            row_number = int(cell_range.split(":")[0][1:])
            values = update["values"][0]
            while len(self.rows) < row_number:
                self.rows.append([])
            self.rows[row_number - 1] = list(values)


class FakeSpreadsheet:
    def __init__(self, sheet=None):
        self.sheet = sheet

    def worksheet(self, title):
        if self.sheet is None:
            raise sheets.WorksheetNotFound("missing")
        return self.sheet

    def add_worksheet(self, title, rows, cols):
        self.sheet = FakeWorksheet()
        return self.sheet


class FakeClient:
    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet

    def open_by_key(self, key):
        return self.spreadsheet


class TelegramLockTests(unittest.TestCase):
    def setUp(self):
        self.original_get_google_client = sheets.get_google_client

    def tearDown(self):
        sheets.get_google_client = self.original_get_google_client

    def use_spreadsheet(self, spreadsheet):
        sheets.get_google_client = lambda: FakeClient(spreadsheet)

    def test_acquire_creates_lock_sheet_and_writes_owner(self):
        spreadsheet = FakeSpreadsheet()
        self.use_spreadsheet(spreadsheet)

        result = sheets.acquire_telegram_poll_lock("pc-1", "PC 1", now_ts=1000)

        self.assertTrue(result["acquired"])
        rows = spreadsheet.sheet.get_all_values()
        self.assertEqual(rows[0], sheets.TELEGRAM_LOCK_HEADER)
        self.assertEqual(rows[1][1], "pc-1")
        self.assertEqual(rows[1][2], "PC 1")

    def test_active_other_owner_blocks_lock(self):
        sheet = FakeWorksheet([
            sheets.TELEGRAM_LOCK_HEADER,
            [sheets.TELEGRAM_LOCK_KEY, "pc-2", "PC 2", "2026-05-26 10:00:00", "1000"],
        ])
        self.use_spreadsheet(FakeSpreadsheet(sheet))

        result = sheets.acquire_telegram_poll_lock("pc-1", "PC 1", now_ts=1010)

        self.assertFalse(result["acquired"])
        self.assertEqual(result["owner_id"], "pc-2")
        self.assertEqual(sheet.rows[1][1], "pc-2")

    def test_stale_other_owner_can_be_replaced(self):
        sheet = FakeWorksheet([
            sheets.TELEGRAM_LOCK_HEADER,
            [sheets.TELEGRAM_LOCK_KEY, "pc-2", "PC 2", "2026-05-26 10:00:00", "1000"],
        ])
        self.use_spreadsheet(FakeSpreadsheet(sheet))

        result = sheets.acquire_telegram_poll_lock("pc-1", "PC 1", now_ts=1100)

        self.assertTrue(result["acquired"])
        self.assertEqual(sheet.rows[1][1], "pc-1")

    def test_release_clears_only_own_lock(self):
        sheet = FakeWorksheet([
            sheets.TELEGRAM_LOCK_HEADER,
            [sheets.TELEGRAM_LOCK_KEY, "pc-1", "PC 1", "2026-05-26 10:00:00", "1000"],
        ])
        self.use_spreadsheet(FakeSpreadsheet(sheet))

        self.assertTrue(sheets.release_telegram_poll_lock("pc-1"))
        self.assertEqual(sheet.rows[1][1], "")
        self.assertFalse(sheets.release_telegram_poll_lock("pc-2"))


if __name__ == "__main__":
    unittest.main()
