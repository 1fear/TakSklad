import re
from datetime import datetime
from io import BytesIO
from re import sub

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .client_points_service import client_point_delivery_slot_map, delivery_slot_for_order
from .models import Order, OrderItem
from .orders_service import ApiError, STATUS_RETURNED
from .reports_service import parse_report_date


LOGISTICS_HEADERS = [
    "Тип заказа",
    "Внешний ID",
    "Описание",
    "Имя клиента",
    "Телефон",
    "Email",
    "Заметки",
    "Широта (забор)",
    "Долгота (забор)",
    "Адрес забора",
    "Окно времени С (забор)",
    "Окно времени ПО (забор)",
    "Окно перерыва С (забор)",
    "Окно перерыва ПО (забор)",
    "Детали адреса забора",
    "Время обслуживания забора",
    "Широта (доставка)",
    "Долгота (доставка)",
    "Адрес доставки",
    "Окно времени С (доставка)",
    "Окно времени ПО (доставка)",
    "Окно перерыва С (доставка)",
    "Окно перерыва ПО (доставка)",
    "Детали адреса доставки",
    "Время обслуживания доставки",
    "Навыки",
    "Название товара",
    "Айди товара",
    "Вес (кг)",
    "Объем (m3)",
    "Короба",
]

LOGISTICS_COORDINATE_PROBLEM_HEADERS = [
    "Клиент",
    "Адрес",
    "Внешний ID",
    "Причина",
    "Товары",
    "Тип оплаты",
    "Дата отгрузки",
    "Складская заявка",
]

PICKUP_ADDRESS = "Самовывоз со склада"
LOGISTICS_DATETIME_FORMAT = "yyyy-mm-dd hh:mm"
LOGISTICS_TEMPLATE_COLUMN_WIDTHS = {
    "A": 14,
    "B": 18,
    "C": 22,
    "D": 30,
    "E": 18,
    "F": 28,
    "G": 30,
    "H": 14,
    "I": 14,
    "J": 30,
    "K": 22,
    "L": 22,
    "Q": 14,
    "R": 14,
    "S": 30,
    "T": 22,
    "U": 22,
    "Z": 24,
    "AA": 22,
    "AB": 18,
    "AC": 14,
    "AD": 14,
    "AE": 10,
}


def list_logistics_dates(db: Session):
    orders = db.execute(
        select(Order)
        .where(Order.order_date.is_not(None))
        .order_by(Order.order_date.asc())
    ).scalars().all()
    dates = []
    for order in orders:
        if not order.order_date or not is_logistics_candidate_order(order):
            continue
        value = order.order_date.isoformat()
        if value not in dates:
            dates.append(value)
    return dates


def build_logistics_report_xlsx(db: Session, shipment_date: str):
    report_date = parse_report_date(shipment_date)
    orders = db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.order_date == report_date)
        .order_by(Order.client.asc(), Order.created_at.asc())
    ).scalars().all()
    if not orders:
        raise ApiError(404, f"No orders for shipment date {report_date.isoformat()}")
    candidate_orders = [order for order in orders if is_logistics_candidate_order(order)]
    if not candidate_orders:
        raise ApiError(404, f"No logistics delivery orders for shipment date {report_date.isoformat()}")
    delivery_orders = [order for order in candidate_orders if is_logistics_delivery_order(order)]
    coordinate_problem_orders = [order for order in candidate_orders if not is_logistics_delivery_order(order)]
    delivery_slots = client_point_delivery_slot_map(db, delivery_orders)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Orders"
    sheet.append(LOGISTICS_HEADERS)
    apply_orders_template_style(sheet)

    for order in delivery_orders:
        coordinates = normalize_coordinates((order.raw_payload or {}).get("coordinates"))
        latitude, longitude = split_coordinates(coordinates)
        delivery_from, delivery_to = delivery_slot_for_order(order, delivery_slots)
        for item in sorted(order.items, key=lambda value: (value.product, str(value.id))):
            quantity_blocks = item_quantity_blocks(item)
            row = [""] * len(LOGISTICS_HEADERS)
            set_cell(row, 1, "delivery")
            set_cell(row, 2, logistics_external_id(order, item))
            set_cell(row, 4, order.client)
            set_cell(row, 7, order.representative or "")
            set_cell(row, 17, latitude)
            set_cell(row, 18, longitude)
            set_cell(row, 19, order.address)
            set_cell(row, 20, delivery_window_datetime(report_date, delivery_from))
            set_cell(row, 21, delivery_window_datetime(report_date, delivery_to))
            set_cell(row, 27, item.product)
            set_cell(row, 29, 0)
            set_cell(row, 30, 0)
            set_cell(row, 31, quantity_blocks)
            sheet.append(row)
            apply_orders_row_style(sheet, sheet.max_row)

    if coordinate_problem_orders:
        problem_sheet = workbook.create_sheet("Требуют координаты")
        problem_sheet.append(LOGISTICS_COORDINATE_PROBLEM_HEADERS)
        apply_header_style(problem_sheet)
        for order in coordinate_problem_orders:
            problem_sheet.append([
                order.client,
                order.address,
                logistics_external_id(order),
                logistics_coordinate_problem_reason(order),
                order_product_summary(order),
                order.payment_type,
                report_date.strftime("%d.%m.%Y"),
                (order.raw_payload or {}).get("skladbot_request_number") or "",
            ])
        autosize_columns(problem_sheet)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue(), logistics_report_filename(report_date)


def logistics_report_filename(report_date):
    return f"TakSklad_логистика_{report_date.strftime('%d.%m.%Y')}.xlsx"


def set_cell(row, one_based_index, value):
    row[one_based_index - 1] = value


def normalize_coordinates(value):
    text = str(value or "").strip()
    if not text:
        return ""
    numbers = re.findall(r"-?\d+(?:[.,]\d+)?", text)
    if len(numbers) < 2:
        return ""
    try:
        latitude = float(numbers[0].replace(",", "."))
        longitude = float(numbers[1].replace(",", "."))
    except ValueError:
        return ""
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return ""
    return f"{format_coordinate(latitude)},{format_coordinate(longitude)}"


def format_coordinate(value):
    return f"{value:.12f}".rstrip("0").rstrip(".")


def is_logistics_delivery_order(order):
    if not is_logistics_candidate_order(order):
        return False
    return bool(normalize_coordinates((order.raw_payload or {}).get("coordinates")))


def is_logistics_candidate_order(order):
    if is_returned_order(order):
        return False
    if is_skladbot_stock_shortage_blocked_order(order):
        return False
    if is_pickup_address(order.address):
        return False
    return True


def is_returned_order(order):
    raw_payload = order.raw_payload or {}
    return (
        str(order.status or "").strip().casefold() == STATUS_RETURNED
        or str(raw_payload.get("return_status") or "").strip().casefold() in {"returned", "return", "возврат"}
    )


def logistics_coordinate_problem_reason(order):
    raw_coordinates = str((order.raw_payload or {}).get("coordinates") or "").strip()
    if raw_coordinates:
        return "Невалидные координаты"
    return "Нет координат"


def order_product_summary(order):
    parts = []
    for item in sorted(order.items, key=lambda value: (value.product, str(value.id))):
        quantity = item_quantity_blocks(item)
        suffix = f" - {quantity} блоков" if quantity else ""
        parts.append(f"{item.product}{suffix}")
    return "; ".join(parts)


def is_skladbot_stock_shortage_blocked_order(order):
    raw_payload = order.raw_payload or {}
    skladbot_status = str(raw_payload.get("skladbot_status") or "").strip()
    if skladbot_status == "cancelled_stock_shortage":
        return True
    if skladbot_status == "create_failed" and "автоотмена пропущена" in str(raw_payload.get("skladbot_error") or ""):
        return True
    if skladbot_status == "create_failed" and "недостат" in str(raw_payload.get("skladbot_error") or "").casefold():
        return True
    return False


def is_pickup_address(value):
    text = normalize_lookup_text(value)
    return text == normalize_lookup_text(PICKUP_ADDRESS) or text.startswith("самовывоз")


def normalize_lookup_text(value):
    text = str(value or "").strip().casefold().replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я]+", "", text)


def split_coordinates(value):
    parts = [part.strip() for part in str(value or "").split(",")]
    if len(parts) < 2:
        return "", ""
    return parts[0], parts[1]


def delivery_window_datetime(report_date, value):
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return datetime(report_date.year, report_date.month, report_date.day, hour, minute)


def logistics_external_id(order, item=None):
    raw_payload = order.raw_payload or {}
    if raw_payload.get("skladbot_request_number"):
        return raw_payload.get("skladbot_request_number")
    if raw_payload.get("source_order_id"):
        return raw_payload.get("source_order_id")
    if item is not None:
        item_payload = item.raw_payload or {}
        if item_payload.get("source_order_id"):
            return item_payload.get("source_order_id")
        if item_payload.get("source_import_id"):
            return item_payload.get("source_import_id")
    for order_item in sorted(order.items, key=lambda value: (value.product, str(value.id))):
        item_payload = order_item.raw_payload or {}
        if item_payload.get("source_order_id"):
            return item_payload.get("source_order_id")
        if item_payload.get("source_import_id"):
            return item_payload.get("source_import_id")
    return ""


def item_quantity_blocks(item):
    if item.quantity_blocks and item.quantity_blocks > 0:
        return item.quantity_blocks
    pieces = item.quantity_pieces or 0
    pieces_per_block = item.pieces_per_block or 10
    if pieces <= 0:
        return 0
    return (pieces + pieces_per_block - 1) // pieces_per_block


def apply_header_style(sheet, *, freeze_panes=True):
    fill = PatternFill("solid", fgColor="1E293B")
    bottom_border = Border(bottom=Side(style="thin", color="000000"))
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = bottom_border
    if freeze_panes:
        sheet.freeze_panes = "A2"


def apply_orders_template_style(sheet):
    apply_header_style(sheet, freeze_panes=False)
    sheet.row_dimensions[1].height = 17.55
    for column_letter, width in LOGISTICS_TEMPLATE_COLUMN_WIDTHS.items():
        sheet.column_dimensions[column_letter].width = width


def apply_orders_row_style(sheet, row_number):
    for column_letter in ("T", "U"):
        sheet[f"{column_letter}{row_number}"].number_format = LOGISTICS_DATETIME_FORMAT


def autosize_columns(sheet):
    for column_cells in sheet.columns:
        column_letter = get_column_letter(column_cells[0].column)
        max_length = max(len(str(cell.value or "")) for cell in column_cells)
        sheet.column_dimensions[column_letter].width = min(max(max_length + 2, 10), 45)


def safe_filename(value):
    return sub(r"[^0-9A-Za-zА-Яа-я_.-]+", "_", str(value or "")).strip("_")
