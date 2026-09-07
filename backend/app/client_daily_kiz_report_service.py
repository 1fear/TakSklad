"""Read-only per-client KIZ export shaped like the SkladBot daily workbook.

The daily workbook is rebuilt from the SkladBot API and covers the whole day.
This module answers a different question: give one legal entity its own copy
for one shipment date, from data TakSklad already owns. Sheet names, column
order and widths are imported from the daily template so both files stay in
step when the template changes.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .kiz_reports_service import (
    apply_header_style,
    autosize_columns,
    parse_int,
    parse_report_date,
    safe_filename,
)
from .models import Order, OrderItem, ScanCode
from .order_statuses import COMPLETED_STATUSES, HIDDEN_ITEM_STATUSES, STATUS_RETURNED
from .orders_service import ApiError
from .representative_contacts import (
    display_representative_name,
    find_representative_contact,
    normalize_phone,
)
from .scan_quantities import scan_block_quantity, scan_type_for_code
from .skladbot_contracts import (
    business_timezone,
    format_internal_smartup_ids,
    normalize_text,
)
from .skladbot_daily_report import (
    CODE_TYPE_LABELS,
    REQUEST_CATEGORY_DEFECT_SHIPMENT,
    REQUEST_CATEGORY_RECEIVING,
    REQUEST_CATEGORY_RETURN,
    REQUEST_CATEGORY_SHIPMENT,
    REQUEST_HEADERS,
    REQUEST_PRODUCT_HEADERS,
    apply_report_template_widths,
    apply_thin_border,
)
from .spreadsheet_safety import force_workbook_text_literals


SUMMARY_HEADERS = ["Показатель", "Блоков", "Заявок"]
SUMMARY_CATEGORIES = (
    REQUEST_CATEGORY_SHIPMENT,
    REQUEST_CATEGORY_DEFECT_SHIPMENT,
    REQUEST_CATEGORY_RETURN,
    REQUEST_CATEGORY_RECEIVING,
)
STOCK_SUMMARY_LABEL = "Актуальный остаток"
ALL_CLIENTS_LABEL = "Все юрлица"
VALID_SCAN_TYPES = frozenset(CODE_TYPE_LABELS)


def list_daily_kiz_clients(db: Session, shipment_date: Any) -> list[dict[str, Any]]:
    """Return legal entities that have orders on one shipment date."""
    target_date = require_report_date(shipment_date)
    result = []
    for label, orders in group_orders_by_client(load_orders(db, target_date)):
        items = [item for order in orders for item in visible_items(order)]
        result.append({
            "client": label,
            "orders": len(orders),
            "planned_blocks": sum(parse_int(item.quantity_blocks) for item in items),
            "scanned_blocks": sum(parse_int(item.scanned_blocks) for item in items),
            "kiz_codes": sum(len(item.scan_codes) for item in items),
        })
    return result


def build_client_daily_kiz_xlsx(db: Session, shipment_date: Any, client: Any = ""):
    """Build the daily-shaped workbook for one legal entity, or for all of them."""
    target_date = require_report_date(shipment_date)
    client_key = client_match_key(client)
    orders = [
        order
        for order in load_orders(db, target_date)
        if not client_key or client_match_key(order.client) == client_key
    ]
    if not orders:
        raise ApiError(404, missing_orders_detail(target_date, client))

    label = client_label(orders) if client_key else ALL_CLIENTS_LABEL
    contacts = representative_contacts(db, orders)
    rows = [order_row(order, contacts) for order in sorted(orders, key=order_sort_key)]

    workbook = Workbook()
    write_summary_sheet(workbook.active, rows)
    write_requests_sheet(workbook.create_sheet("Заявки"), rows)
    write_request_products_sheet(workbook.create_sheet("Товары заявок"), rows)
    for sheet in workbook.worksheets:
        autosize_columns(sheet)
    apply_report_template_widths(workbook)

    buffer = BytesIO()
    force_workbook_text_literals(workbook)
    workbook.save(buffer)
    return buffer.getvalue(), report_filename(label, target_date)


def write_summary_sheet(sheet, rows: list[dict[str, Any]]) -> None:
    sheet.title = "Сводка"
    sheet.append(SUMMARY_HEADERS)
    for category in SUMMARY_CATEGORIES:
        category_rows = [row for row in rows if row["category"] == category]
        sheet.append([
            category,
            sum(row["planned_blocks"] for row in category_rows),
            len(category_rows),
        ])
    # Остаток склада не принадлежит юрлицу, поэтому ячейки остаются пустыми,
    # а не нулевыми: ноль читался бы как «остатка нет»
    sheet.append([STOCK_SUMMARY_LABEL, None, None])
    apply_header_style(sheet)
    for cell in ("A6", "B6"):
        sheet[cell].font = Font(bold=True)
    apply_thin_border(sheet, "A2:C6")


def write_requests_sheet(sheet, rows: list[dict[str, Any]]) -> None:
    sheet.append(REQUEST_HEADERS)
    for row in rows:
        sheet.append([
            row["number"],
            row["smartup_id"],
            row["category"],
            "Выполнена" if row["is_completed"] else "Не выполнена",
            row["created_at"],
            row["unloading_date"],
            row["recipient"],
            row["representative"],
            row["address"],
            row["payment_type"],
            row["work_phone"],
            row["personal_phone"],
            row["planned_blocks"],
            row["scanned_blocks"],
            row_kiz_count(row),
        ])
    apply_header_style(sheet)


def write_request_products_sheet(sheet, rows: list[dict[str, Any]]) -> None:
    """Write one row per marking code, keeping products without codes visible."""
    sheet.append(REQUEST_PRODUCT_HEADERS)
    for row in rows:
        prefix = [
            row["number"],
            row["smartup_id"],
            row["category"],
            row["unloading_date"],
            row["payment_type"],
            row["recipient"],
            row["representative"],
        ]
        for product in row["products"]:
            product_prefix = [*prefix, product["product"], product["barcode"]]
            if not product["codes"]:
                sheet.append([*product_prefix, "", "", ""])
                continue
            for code in product["codes"]:
                sheet.append([
                    *product_prefix,
                    code["code"],
                    CODE_TYPE_LABELS.get(code["scan_type"], code["scan_type"]),
                    code["block_quantity"],
                ])
    apply_header_style(sheet)


def order_row(order: Order, contacts: dict[str, Any]) -> dict[str, Any]:
    raw_payload = order.raw_payload or {}
    items = sorted(visible_items(order), key=item_sort_key)
    contact = contacts.get(representative_key(order.representative))
    category = (
        REQUEST_CATEGORY_RETURN
        if normalize_text(order.status) == STATUS_RETURNED
        else REQUEST_CATEGORY_SHIPMENT
    )
    return {
        "number": request_number(raw_payload, category),
        "smartup_id": format_internal_smartup_ids([
            raw_payload.get("source_order_id"),
            *((item.raw_payload or {}).get("source_order_id") for item in items),
        ]),
        "category": category,
        "is_completed": normalize_text(order.status) in COMPLETED_STATUSES,
        "created_at": display_datetime(order.created_at),
        "unloading_date": display_date(order.order_date),
        "recipient": normalize_text(order.client),
        "representative": display_representative_name(order.representative, contact),
        "address": normalize_text(order.address),
        "payment_type": normalize_text(order.payment_type),
        "work_phone": normalize_phone(getattr(contact, "work_phone", "")),
        "personal_phone": normalize_phone(getattr(contact, "personal_phone", "")),
        "planned_blocks": sum(parse_int(item.quantity_blocks) for item in items),
        "scanned_blocks": sum(parse_int(item.scanned_blocks) for item in items),
        "products": [product_row(item) for item in items],
    }


def product_row(item: OrderItem) -> dict[str, Any]:
    return {
        "product": normalize_text(item.product),
        # Штрихкод приходит только из карточки заявки SkladBot, в нашей базе его нет
        "barcode": "",
        "codes": [
            {
                "code": scan.code,
                "scan_type": scan_type(scan),
                "block_quantity": scan_block_quantity(scan),
            }
            for scan in sorted(item.scan_codes, key=scan_sort_key)
        ],
    }


def load_orders(db: Session, target_date: date) -> list[Order]:
    return list(db.execute(
        select(Order)
        .where(Order.order_date == target_date)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
    ).scalars().all())


def group_orders_by_client(orders: list[Order]) -> list[tuple[str, list[Order]]]:
    grouped: dict[str, list[Order]] = {}
    for order in orders:
        grouped.setdefault(client_match_key(order.client), []).append(order)
    return sorted(
        ((client_label(client_orders), client_orders) for client_orders in grouped.values()),
        key=lambda pair: pair[0].casefold(),
    )


def representative_contacts(db: Session, orders: list[Order]) -> dict[str, Any]:
    contacts: dict[str, Any] = {}
    for order in orders:
        key = representative_key(order.representative)
        if not key or key in contacts:
            continue
        contacts[key] = find_representative_contact(db, order.representative)
    return contacts


def visible_items(order: Order) -> list[OrderItem]:
    return [item for item in order.items if item.status not in HIDDEN_ITEM_STATUSES]


def request_number(raw_payload: dict[str, Any], category: str) -> str:
    shipment = normalize_text(raw_payload.get("skladbot_request_number"))
    returned = normalize_text(raw_payload.get("skladbot_return_request_number"))
    if category == REQUEST_CATEGORY_RETURN:
        return returned or shipment
    return shipment or returned


def scan_type(scan: ScanCode) -> str:
    stored = normalize_text((scan.raw_payload or {}).get("scan_type"))
    return stored if stored in VALID_SCAN_TYPES else scan_type_for_code(scan.code)


def row_kiz_count(row: dict[str, Any]) -> int:
    return sum(len(product["codes"]) for product in row["products"])


def client_match_key(value: Any) -> str:
    return normalize_text(value).casefold()


def client_label(orders: list[Order]) -> str:
    labels = sorted({normalize_text(order.client) for order in orders if normalize_text(order.client)})
    return labels[0] if labels else ""


def representative_key(value: Any) -> str:
    return normalize_text(value).casefold()


def order_sort_key(order: Order) -> tuple[str, str, str]:
    raw_payload = order.raw_payload or {}
    return (
        normalize_text(order.client).casefold(),
        normalize_text(raw_payload.get("skladbot_request_number")),
        str(order.id),
    )


def item_sort_key(item: OrderItem) -> tuple[str, str]:
    return (normalize_text(item.product).casefold(), str(item.id))


def scan_sort_key(scan: ScanCode) -> tuple[str, str]:
    return (str(scan.scanned_at or ""), str(scan.id))


def require_report_date(value: Any) -> date:
    target_date = parse_report_date(value)
    if not target_date:
        raise ApiError(422, "shipment_date is required")
    return target_date


def missing_orders_detail(target_date: date, client: Any) -> str:
    client_text = normalize_text(client)
    if client_text:
        return f"No orders for {client_text} on shipment date {target_date.isoformat()}"
    return f"No orders for shipment date {target_date.isoformat()}"


def report_filename(label: str, target_date: date) -> str:
    display = target_date.strftime("%d.%m.%Y")
    return f"TakSklad_КИЗ_дейли_{safe_filename(label)}_{display}.xlsx"


def display_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().strftime("%d.%m.%Y")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    return normalize_text(value)


def display_datetime(value: Any) -> str:
    if not isinstance(value, datetime):
        return normalize_text(value)
    moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return moment.astimezone(business_timezone()).strftime("%d.%m.%Y %H:%M")
