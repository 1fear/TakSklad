import re
import unittest
from datetime import date
from types import SimpleNamespace
from unittest import mock

from backend.app import google_sheets_exporter as exporter


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

    def delete_rows(self, row_number):
        del self.rows[row_number - 1]


class FakeSpreadsheet:
    def __init__(self, sheets):
        self.sheets = dict(sheets)
        for sheet in self.sheets.values():
            sheet.spreadsheet = self

    def worksheet(self, title):
        if title not in self.sheets:
            raise Exception("sheet not found")
        return self.sheets[title]

    def add_worksheet(self, title, rows=1000, cols=32):
        sheet = FakeSheet(title, [])
        sheet.spreadsheet = self
        self.sheets[title] = sheet
        return sheet


class BackendGoogleSheetsExporterTests(unittest.TestCase):
    def test_split_codes_keeps_comma_inside_kiz(self):
        first = "01012345678901234567ABC,DEF"
        second = "01012345678901234567XYZ"

        self.assertEqual(exporter.split_codes(f"{first}\n{second}"), [first, second])

    def make_scan(self, code, scanned_at="2026-06-01T10:00:00+05:00"):
        return SimpleNamespace(id=code, code=code, scanned_at=scanned_at)

    def make_item(
        self,
        import_id="import-1",
        order_id="order-1",
        codes=None,
        status="completed",
        product="Chapman RED OP 20",
        quantity_pieces=20,
        quantity_blocks=None,
    ):
        codes = codes or ["0101", "0102"]
        quantity_blocks = len(codes) if quantity_blocks is None else quantity_blocks
        return SimpleNamespace(
            id=import_id,
            product=product,
            raw_payload={
                "source_import_id": import_id,
                "source_order_id": order_id,
            },
            status=status,
            scanned_blocks=len(codes),
            quantity_pieces=quantity_pieces,
            quantity_blocks=quantity_blocks,
            scan_codes=[self.make_scan(code) for code in codes],
        )

    def make_order(self, items, **raw_payload):
        return SimpleNamespace(
            id="backend-order-1",
            order_date=date(2026, 6, 1),
            payment_type="Перечисление",
            client="Client",
            address="Tashkent, Test 1",
            representative="Rep",
            items=items,
            raw_payload=raw_payload,
        )

    def make_row(self, import_id, order_id, codes="", status="Не выполнено"):
        return exporter.build_import_record_row({
            "Дата отгрузки": "01.06.2026",
            "Тип оплаты": "Перечисление",
            "Клиент": "Client",
            "Адрес": "Tashkent, Test 1",
            "Торговый представитель": "Rep",
            "Товары": "Chapman RED OP 20",
            "Кол-во ШТ": 20,
            "Кол-во блок": 2,
            "Отсканированные коды": codes,
            "Статус": status,
            "ID заказа": order_id,
            "ID импорта": import_id,
        })

    def test_google_client_uses_timeout_http_client(self):
        fake_credentials = {"client_email": "service@example.test"}

        with mock.patch.object(exporter, "load_google_credentials", return_value=fake_credentials), mock.patch(
            "gspread.service_account_from_dict",
            return_value=object(),
        ) as service_account:
            client = exporter.get_google_client()

        self.assertIsNotNone(client)
        service_account.assert_called_once()
        self.assertEqual(service_account.call_args.args[0], fake_credentials)
        self.assertEqual(service_account.call_args.kwargs["http_client"], exporter.GoogleTimeoutHTTPClient)
        self.assertGreaterEqual(exporter.GOOGLE_API_TIMEOUT_SECONDS, 1)

    def test_update_backend_order_item_row_replaces_codes_and_status_from_backend(self):
        header = exporter.build_import_sheet_header()
        sheet = FakeSheet("data", [header, self.make_row("import-1", "order-1", codes="0100")])
        item = self.make_item()

        result = exporter.update_backend_order_item_row(sheet, item)

        self.assertEqual(result["status"], "completed")
        header_idx = exporter.get_header_index(sheet.rows[0])
        self.assertEqual(sheet.rows[1][header_idx["Отсканированные коды"]], "0101\n0102")
        self.assertEqual(sheet.rows[1][header_idx["Статус"]], "Выполнено")

    def test_restore_import_records_updates_existing_row_instead_of_skipping_duplicate(self):
        header = exporter.build_import_sheet_header()
        sheet = FakeSheet("data", [
            header.copy(),
            self.make_row("import-1", "order-1", codes="stale-code", status="Выполнено"),
        ])
        spreadsheet = FakeSpreadsheet({"data": sheet})
        record = {
            "Дата отгрузки": "01.06.2026",
            "Тип оплаты": "Перечисление",
            "Клиент": "Client",
            "Адрес": "Tashkent, Test 1",
            "Торговый представитель": "Rep",
            "Товары": "Chapman RED OP 20",
            "Кол-во ШТ": 20,
            "Кол-во блок": 2,
            "Отсканированные коды": "",
            "Статус": "Не выполнено",
            "ID заказа": "order-1",
            "ID импорта": "import-1",
        }

        with mock.patch.object(exporter, "get_google_client", return_value=SimpleNamespace(open_by_key=lambda _key: spreadsheet)):
            result = exporter.restore_import_records_to_google_sheets([record])

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["imported"], 0)
        header_idx = exporter.get_header_index(sheet.rows[0])
        self.assertEqual(sheet.rows[1][header_idx["Отсканированные коды"]], "")
        self.assertEqual(sheet.rows[1][header_idx["Статус"]], "Не выполнено")

    def test_update_backend_orders_skladbot_rows_writes_request_fields(self):
        header = exporter.build_import_sheet_header()
        sheet = FakeSheet("data", [
            header,
            self.make_row("import-1", "order-1"),
            self.make_row("import-2", "order-2"),
        ])
        item_one = self.make_item("import-1", "order-1", codes=[])
        item_two = self.make_item("import-2", "order-2", codes=[])
        order = self.make_order(
            [item_one, item_two],
            skladbot_request_number="WH-R-191794",
            skladbot_request_id="191794",
            skladbot_status="found",
            skladbot_checked_at="2026-06-01T11:30:00+05:00",
        )

        result = exporter.update_backend_orders_skladbot_rows(sheet, [order])

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["updated"], 2)
        header_idx = exporter.get_header_index(sheet.rows[0])
        for row in sheet.rows[1:]:
            self.assertEqual(row[header_idx["Номер заявки SkladBot"]], "WH-R-191794")
            self.assertEqual(row[header_idx["ID заявки SkladBot"]], "191794")
            self.assertEqual(row[header_idx["Статус SkladBot"]], "Найдено")
            self.assertEqual(row[header_idx["Последняя проверка SkladBot"]], "2026-06-01T11:30:00+05:00")

    def test_update_backend_orders_skladbot_rows_does_not_clear_existing_number_on_pending(self):
        header = exporter.build_import_sheet_header()
        row = self.make_row("import-1", "order-1")
        header_idx = exporter.get_header_index(header)
        row[header_idx["Номер заявки SkladBot"]] = "WH-R-OLD"
        row[header_idx["ID заявки SkladBot"]] = "old-id"
        sheet = FakeSheet("data", [header, row])
        item = self.make_item("import-1", "order-1", codes=[])
        order = self.make_order(
            [item],
            skladbot_status="pending",
            skladbot_checked_at="2026-06-03T12:00:00+05:00",
        )

        result = exporter.update_backend_orders_skladbot_rows(sheet, [order])

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["updated"], 1)
        self.assertEqual(sheet.rows[1][header_idx["Номер заявки SkladBot"]], "WH-R-OLD")
        self.assertEqual(sheet.rows[1][header_idx["ID заявки SkladBot"]], "old-id")
        self.assertEqual(sheet.rows[1][header_idx["Статус SkladBot"]], "Проверяется")
        self.assertEqual(sheet.rows[1][header_idx["Последняя проверка SkladBot"]], "2026-06-03T12:00:00+05:00")

    def test_update_backend_orders_skladbot_rows_uses_business_key_when_ids_changed(self):
        header = exporter.build_import_sheet_header()
        sheet = FakeSheet("data", [
            header,
            self.make_row("old-import", "old-order"),
        ])
        item = self.make_item("new-import", "new-order", codes=[], quantity_blocks=2, quantity_pieces=20)
        order = self.make_order(
            [item],
            skladbot_request_number="WH-R-193025",
            skladbot_request_id="193025",
            skladbot_status="found",
            skladbot_checked_at="2026-06-03T12:20:00+05:00",
        )

        result = exporter.update_backend_orders_skladbot_rows(sheet, [order])

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["updated"], 1)
        header_idx = exporter.get_header_index(sheet.rows[0])
        self.assertEqual(sheet.rows[1][header_idx["Номер заявки SkladBot"]], "WH-R-193025")
        self.assertEqual(sheet.rows[1][header_idx["ID заявки SkladBot"]], "193025")
        self.assertEqual(sheet.rows[1][header_idx["Статус SkladBot"]], "Найдено")

    def test_archive_backend_order_rows_moves_rows_to_archive(self):
        header = exporter.build_import_sheet_header()
        item_one = self.make_item("import-1", "order-1", codes=["0101", "0102"])
        item_two = self.make_item("import-2", "order-2", codes=["0103"])
        data_sheet = FakeSheet("data", [
            header.copy(),
            self.make_row("import-1", "order-1"),
            self.make_row("import-2", "order-2"),
        ])
        archive_sheet = FakeSheet("Архив", [header.copy()])

        result = exporter.archive_backend_order_rows(data_sheet, archive_sheet, self.make_order([item_one, item_two]))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["updated"], 2)
        self.assertEqual(len(data_sheet.rows), 1)
        self.assertEqual(len(archive_sheet.rows), 3)
        header_idx = exporter.get_header_index(archive_sheet.rows[0])
        self.assertEqual(archive_sheet.rows[1][header_idx["Отсканированные коды"]], "0101\n0102")
        self.assertEqual(archive_sheet.rows[2][header_idx["Отсканированные коды"]], "0103")
        self.assertEqual(archive_sheet.rows[1][header_idx["Статус"]], "Выполнено")

    def test_archive_without_kiz_uses_separate_google_sheet(self):
        header = exporter.build_import_sheet_header()
        item = self.make_item("import-1", "order-1", codes=[])
        data_sheet = FakeSheet("data", [header.copy(), self.make_row("import-1", "order-1")])
        spreadsheet = FakeSpreadsheet({"data": data_sheet})
        fake_client = SimpleNamespace(open_by_key=lambda _key: spreadsheet)

        with mock.patch.object(exporter, "get_google_client", return_value=fake_client):
            result = exporter.archive_backend_order_without_kiz_to_google_sheets(self.make_order([item]))

        self.assertEqual(result["status"], "completed")
        self.assertIn(exporter.ARCHIVE_NO_KIZ_SHEET_NAME, spreadsheet.sheets)
        self.assertNotIn(exporter.ARCHIVE_SHEET_NAME, spreadsheet.sheets)
        target_sheet = spreadsheet.sheets[exporter.ARCHIVE_NO_KIZ_SHEET_NAME]
        header_idx = exporter.get_header_index(target_sheet.rows[0])
        self.assertEqual(target_sheet.rows[1][header_idx["Статус"]], exporter.STATUS_ARCHIVED_NO_KIZ)
        self.assertEqual(len(data_sheet.rows), 1)

    def test_cancel_order_uses_separate_google_sheet(self):
        header = exporter.build_import_sheet_header()
        item = self.make_item("import-1", "order-1", codes=[])
        data_sheet = FakeSheet("data", [header.copy(), self.make_row("import-1", "order-1")])
        spreadsheet = FakeSpreadsheet({"data": data_sheet})
        fake_client = SimpleNamespace(open_by_key=lambda _key: spreadsheet)

        with mock.patch.object(exporter, "get_google_client", return_value=fake_client):
            result = exporter.cancel_backend_order_in_google_sheets(self.make_order([item]))

        self.assertEqual(result["status"], "completed")
        self.assertIn(exporter.CANCELLED_SHEET_NAME, spreadsheet.sheets)
        self.assertNotIn(exporter.ARCHIVE_SHEET_NAME, spreadsheet.sheets)
        target_sheet = spreadsheet.sheets[exporter.CANCELLED_SHEET_NAME]
        header_idx = exporter.get_header_index(target_sheet.rows[0])
        self.assertEqual(target_sheet.rows[1][header_idx["Статус"]], exporter.STATUS_CANCELLED)
        self.assertEqual(len(data_sheet.rows), 1)

    def test_mark_backend_return_rows_updates_archive_and_returns_sheet(self):
        header = exporter.build_import_sheet_header()
        item = self.make_item("import-1", "order-1", codes=["0101", "0102"])
        archive_sheet = FakeSheet("Архив", [header.copy(), self.make_row("import-1", "order-1", status="Выполнено")])
        returns_sheet = FakeSheet("Возвраты", [header.copy()])
        order = self.make_order(
            [item],
            returned_at="2026-06-01T12:00:00+05:00",
            return_reference="WH-R-1",
            returned_by="tester",
        )

        result = exporter.mark_backend_return_rows(archive_sheet, returns_sheet, order)

        self.assertEqual(result["status"], "completed")
        archive_header_idx = exporter.get_header_index(archive_sheet.rows[0])
        self.assertEqual(archive_sheet.rows[1][archive_header_idx[exporter.RETURN_STATUS_COLUMN]], "Возврат")
        self.assertEqual(archive_sheet.rows[1][archive_header_idx[exporter.RETURN_REFERENCE_COLUMN]], "WH-R-1")
        self.assertEqual(archive_sheet.rows[1][archive_header_idx[exporter.RETURNED_BY_COLUMN]], "tester")
        self.assertEqual(len(returns_sheet.rows), 2)
        returns_header_idx = exporter.get_header_index(returns_sheet.rows[0])
        self.assertEqual(returns_sheet.rows[1][returns_header_idx[exporter.RETURN_STATUS_COLUMN]], "Возврат")


if __name__ == "__main__":
    unittest.main()
