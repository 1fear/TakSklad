import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import String, cast, or_, select
from sqlalchemy.orm import selectinload

from .db import SessionLocal
from .models import Order, OrderItem
from .orders_service import COMPLETED_STATUSES, HIDDEN_ITEM_STATUSES
from .skladbot_worker import order_has_skladbot_number


def active_order_condition(marker):
    return or_(
        Order.client == marker,
        Order.external_id == marker,
        cast(Order.raw_payload, String).contains(marker),
    )


def visible_items(order):
    return [
        item
        for item in (getattr(order, "items", []) or [])
        if getattr(item, "status", "") not in HIDDEN_ITEM_STATUSES
    ]


def order_label(order):
    raw_payload = getattr(order, "raw_payload", None) or {}
    return {
        "order_id": str(getattr(order, "id", "")),
        "date": order.order_date.isoformat() if order.order_date else "",
        "client": order.client,
        "payment": order.payment_type,
        "skladbot_request_number": raw_payload.get("skladbot_request_number") or "",
        "skladbot_request_id": raw_payload.get("skladbot_request_id") or "",
        "skladbot_status": raw_payload.get("skladbot_status") or "",
        "skladbot_checked_at": raw_payload.get("skladbot_checked_at") or "",
        "items": len(visible_items(order)),
    }


def verify_skladbot_coverage(orders, detail_limit=20):
    detail_limit = max(1, min(int(detail_limit or 20), 100))
    active_orders = [order for order in orders if visible_items(order)]
    numbered_orders = [order for order in active_orders if order_has_skladbot_number(order)]
    missing_orders = [order for order in active_orders if not order_has_skladbot_number(order)]
    missing_statuses = {}
    for order in missing_orders:
        status = str((order.raw_payload or {}).get("skladbot_status") or "missing").strip() or "missing"
        missing_statuses[status] = missing_statuses.get(status, 0) + 1

    errors = []
    if missing_orders:
        errors.append(f"active orders without SkladBot number: {len(missing_orders)}")

    return {
        "status": "failed" if errors else "ok",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "errors": errors,
        "active_orders": len(active_orders),
        "numbered_orders": len(numbered_orders),
        "missing_orders": len(missing_orders),
        "missing_statuses": missing_statuses,
        "missing_details": [order_label(order) for order in missing_orders[:detail_limit]],
        "numbered_details": [order_label(order) for order in numbered_orders[:detail_limit]],
    }


def diagnose_skladbot_coverage(marker="", detail_limit=20):
    with SessionLocal() as db:
        stmt = (
            select(Order)
            .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
            .where(~Order.status.in_(COMPLETED_STATUSES))
            .order_by(Order.order_date.asc(), Order.client.asc())
        )
        if marker:
            stmt = stmt.where(active_order_condition(marker))
        orders = db.execute(stmt).scalars().all()
    result = verify_skladbot_coverage(orders, detail_limit=detail_limit)
    result["marker"] = marker
    return result


def main():
    parser = argparse.ArgumentParser(description="Read-only active backend SkladBot coverage verifier.")
    parser.add_argument("--marker", default="", help="Optional marker to restrict active backend orders.")
    parser.add_argument("--detail-limit", type=int, default=20, help="Max order details to include.")
    args = parser.parse_args()
    print(json.dumps(
        diagnose_skladbot_coverage(marker=args.marker, detail_limit=args.detail_limit),
        ensure_ascii=False,
        sort_keys=True,
    ))


if __name__ == "__main__":
    main()
