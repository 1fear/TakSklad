from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import Order, OrderItem
from .orders_service import ApiError, COMPLETED_STATUSES
from .schemas import (
    DayReportOrder,
    DayReportPaymentGroup,
    DayReportRead,
    DayReportTotals,
)


def build_day_report(db: Session, report_date: str | None = None):
    parsed_date = parse_report_date(report_date)
    orders = db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .order_by(Order.order_date.asc(), Order.created_at.asc())
    ).scalars().all()

    report_orders = []
    payment_totals = {}
    totals = {
        "orders": 0,
        "completed_orders": 0,
        "active_orders": 0,
        "items": 0,
        "completed_items": 0,
        "planned_blocks": 0,
        "scanned_blocks": 0,
        "scanned_today": 0,
        "remaining_blocks": 0,
        "scan_codes": 0,
        "total_price": 0,
    }

    for order in orders:
        if not should_include_order(order, parsed_date):
            continue

        order_totals = summarize_order(order, parsed_date)
        report_orders.append(DayReportOrder(
            id=str(order.id),
            order_date=order.order_date,
            payment_type=order.payment_type,
            payment_group=payment_group(order.payment_type),
            client=order.client,
            address=order.address,
            coordinates=(order.raw_payload or {}).get("coordinates") or "",
            representative=order.representative,
            status=order.status,
            skladbot_request_number=(order.raw_payload or {}).get("skladbot_request_number") or "",
            **order_totals,
        ))
        add_totals(totals, order_totals, order.status)
        add_payment_totals(payment_totals, order.payment_type, order_totals)

    return DayReportRead(
        report_date=parsed_date,
        source="postgres",
        generated_at=datetime.now(timezone.utc),
        totals=DayReportTotals(**totals),
        payment_groups=[
            DayReportPaymentGroup(
                payment_group=group,
                payment_type=values["payment_type"],
                orders=values["orders"],
                planned_blocks=values["planned_blocks"],
                scanned_blocks=values["scanned_blocks"],
                scanned_today=values["scanned_today"],
                remaining_blocks=values["remaining_blocks"],
                scan_codes=values["scan_codes"],
                total_price=values["total_price"],
            )
            for group, values in sorted(payment_totals.items())
        ],
        orders=report_orders,
    )


def parse_report_date(value: str | None):
    if not value:
        return datetime.now(timezone.utc).date()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text.split()[0], fmt).date()
        except ValueError:
            pass
    raise ApiError(422, "Invalid report_date. Use YYYY-MM-DD or DD.MM.YYYY")


def should_include_order(order: Order, report_date: date):
    if order.order_date == report_date:
        return True
    return any(scan_date(scan.scanned_at) == report_date for item in order.items for scan in item.scan_codes)


def summarize_order(order: Order, report_date: date):
    items = list(order.items)
    planned_blocks = sum(max(0, item.quantity_blocks or 0) for item in items)
    scanned_blocks = sum(max(0, item.scanned_blocks or 0) for item in items)
    scanned_today = sum(
        1
        for item in items
        for scan in item.scan_codes
        if scan_date(scan.scanned_at) == report_date
    )
    completed_items = sum(1 for item in items if item.status in COMPLETED_STATUSES)
    return {
        "items": len(items),
        "completed_items": completed_items,
        "planned_blocks": planned_blocks,
        "scanned_blocks": scanned_blocks,
        "scanned_today": scanned_today,
        "remaining_blocks": max(0, planned_blocks - scanned_blocks),
        "scan_codes": sum(len(item.scan_codes) for item in items),
        "total_price": sum(parse_int((item.raw_payload or {}).get("line_total")) for item in items),
    }


def add_totals(totals, order_totals, order_status):
    totals["orders"] += 1
    if order_status in COMPLETED_STATUSES:
        totals["completed_orders"] += 1
    else:
        totals["active_orders"] += 1
    for key in (
        "items",
        "completed_items",
        "planned_blocks",
        "scanned_blocks",
        "scanned_today",
        "remaining_blocks",
        "scan_codes",
        "total_price",
    ):
        totals[key] += order_totals[key]


def add_payment_totals(payment_totals, payment_type, order_totals):
    group = payment_group(payment_type)
    values = payment_totals.setdefault(group, {
        "payment_type": payment_type or "",
        "orders": 0,
        "planned_blocks": 0,
        "scanned_blocks": 0,
        "scanned_today": 0,
        "remaining_blocks": 0,
        "scan_codes": 0,
        "total_price": 0,
    })
    values["orders"] += 1
    for key in ("planned_blocks", "scanned_blocks", "scanned_today", "remaining_blocks", "scan_codes", "total_price"):
        values[key] += order_totals[key]


def payment_group(value):
    payment = str(value or "").strip().lower().replace("ё", "е")
    if "терминал" in payment or "terminal" in payment:
        return "terminal"
    if "перечис" in payment or "безнал" in payment or "transfer" in payment:
        return "transfer"
    return "unknown"


def scan_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def parse_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
