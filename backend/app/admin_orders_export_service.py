from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font

from .admin_service import admin_sql_expressions, admin_sql_filter_predicates, query_admin_page
from .spreadsheet_safety import MAX_XLSX_DATA_ROWS, force_workbook_text_literals


class AdminOrdersExportError(ValueError):
    pass


def build_admin_orders_xlsx(
    db,
    *,
    status_bucket="",
    shipment_date="",
    search="",
    scan_state="",
    skladbot_filter="",
):
    expressions = admin_sql_expressions(db)
    predicates = admin_sql_filter_predicates(
        expressions,
        status_bucket=status_bucket,
        shipment_date=shipment_date,
        search=search,
        scan_state=scan_state,
        skladbot_filter=skladbot_filter,
    )
    rows = query_admin_page(
        db,
        expressions,
        predicates,
        limit=MAX_XLSX_DATA_ROWS + 1,
        offset=0,
    )
    if len(rows) > MAX_XLSX_DATA_ROWS:
        raise AdminOrdersExportError("export_row_limit_exceeded")

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Заказы"
    headers = (
        "ID заказа", "Дата отгрузки", "Клиент", "Адрес", "Тип оплаты", "Торговый представитель",
        "Товар", "Штук", "Блоков", "Отсканировано", "Осталось", "Статус заказа", "Статус позиции",
        "Номер SkladBot", "ID SkladBot", "Статус SkladBot", "Источник файла",
    )
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in rows:
        sheet.append((
            str(row["order_id"]),
            row["order_date"].isoformat() if row["order_date"] else "",
            row["client"],
            row["address"],
            row["payment_type"],
            row["representative"] or "",
            row["product"],
            int(row["quantity_pieces"] or 0),
            int(row["quantity_blocks"] or 0),
            int(row["scanned_blocks"] or 0),
            int(row["remaining_blocks"] or 0),
            row["order_status"],
            row["item_status"],
            row["skladbot_request_number"] or "",
            row["skladbot_request_id"] or "",
            row["skladbot_status"] or "",
            row["source_file"] or "",
        ))
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    force_workbook_text_literals(workbook)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue(), "TakSklad_заказы.xlsx", len(rows)
