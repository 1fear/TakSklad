from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .client_points_service import client_point_delivery_slot_map, delivery_slot_for_order
from .kiz_reports_service import source_file_for_items
from .logistics_service import is_logistics_candidate_order, is_returned_order
from .logistics_zone_service import ZONE_CITY, classify_order, load_region_index
from .models import Order
from .reports_service import report_timezone
from .skladbot_contracts import canonical_skladbot_request_number


def list_logistics_calendar_day_orders(db: Session, service_date: date) -> dict[str, Any]:
    orders = db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.order_date == service_date)
        .order_by(Order.client.asc(), Order.created_at.asc())
    ).scalars().all()
    region_index = load_region_index(db)
    region_directory_empty = len(region_index) == 0
    listed = [order for order in orders if is_returned_order(order) or is_logistics_candidate_order(order)]
    delivery_slots = client_point_delivery_slot_map(db, listed)
    today = datetime.now(report_timezone()).date()
    rows = []
    for order in listed:
        returned_order = is_returned_order(order)
        zone = ZONE_CITY if region_directory_empty else classify_order(
            order.client,
            (order.raw_payload or {}).get("coordinates"),
            region_index,
        )
        delivery_from, delivery_to = delivery_slot_for_order(order, delivery_slots)
        quantity_blocks = sum(int(item.quantity_blocks or 0) for item in order.items)
        scanned_blocks = sum(int(item.scanned_blocks or 0) for item in order.items)
        raw_payload = order.raw_payload or {}
        rows.append({
            "order_id": str(order.id),
            "zone": zone,
            "is_returned": returned_order,
            "lifecycle_status": order_lifecycle_status(order, today),
            "client": order.client or "",
            "address": order.address or "",
            "representative": order.representative or "",
            "products": order_products_text(order),
            "source_file": source_file_for_items(order.items),
            "quantity_blocks": quantity_blocks,
            "scanned_blocks": scanned_blocks,
            "remaining_blocks": max(0, quantity_blocks - scanned_blocks),
            "status": order.status or "",
            "delivery_from": str(delivery_from or ""),
            "delivery_to": str(delivery_to or ""),
            "skladbot_request_number": canonical_skladbot_request_number(
                raw_payload.get("skladbot_request_number")
            ) or "",
            "smartup_id": str(raw_payload.get("source_order_id") or ""),
            "line_total": sum(int((item.raw_payload or {}).get("line_total") or 0) for item in order.items),
        })
    return {
        "date": service_date,
        "generated_at": datetime.now(timezone.utc),
        "region_directory_empty": region_directory_empty,
        "orders": rows,
    }


def order_lifecycle_status(order: Order, today: date) -> str:
    """Вычислить статус жизненного цикла заказа для календаря логистики

    Фактов отгрузки и доставки в системе нет, поэтому статус выводится из
    собранности заказа, даты доставки против сегодняшней даты и признака
    возврата: ничего не сохраняется, вызывающий обязан передать today, вычисленную
    один раз на запрос (ташкентский деловой пояс), а не звать report_timezone
    заново на каждый заказ
    """
    if is_returned_order(order):
        return "returned"
    quantity_blocks = sum(int(item.quantity_blocks or 0) for item in order.items)
    scanned_blocks = sum(int(item.scanned_blocks or 0) for item in order.items)
    if scanned_blocks < quantity_blocks:
        return "assembling"
    delivery_date = order.order_date
    if delivery_date > today:
        return "assembled"
    if delivery_date == today:
        return "shipped"
    return "delivered"


def order_products_text(order: Order) -> str:
    names = []
    for item in sorted(order.items, key=lambda value: (value.product, str(value.id))):
        if item.product and item.product not in names:
            names.append(item.product)
    return "; ".join(names)
