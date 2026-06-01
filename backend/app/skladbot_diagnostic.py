import argparse
import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import String, cast, or_, select
from sqlalchemy.orm import selectinload

from .db import SessionLocal
from .models import Order, OrderItem
from .orders_service import COMPLETED_STATUSES
from .skladbot_worker import (
    fetch_candidate_requests,
    normalize_payment_type,
    request_match_diagnostics,
)


logging.getLogger("httpx").setLevel(logging.WARNING)


def active_order_condition(marker):
    return or_(
        Order.client == marker,
        Order.external_id == marker,
        cast(Order.raw_payload, String).contains(marker),
    )


def order_payload(order):
    return {
        "id": str(order.id),
        "date": order.order_date.isoformat() if order.order_date else "",
        "client": order.client,
        "payment": order.payment_type,
        "items": [
            {
                "product": item.product,
                "blocks": item.quantity_blocks,
                "scanned_blocks": item.scanned_blocks,
                "status": item.status,
            }
            for item in order.items
        ],
    }


def request_payload(request, diagnostic):
    checks = diagnostic.get("checks") or {}
    return {
        "id": request.get("id"),
        "number": request.get("number") or "",
        "unloading_date": request.get("unloading_date") or "",
        "recipient": request.get("recipient") or "",
        "payment": normalize_payment_type(request.get("comment")),
        "products": len(request.get("products") or []),
        "matched": diagnostic.get("matched", False),
        "score": diagnostic.get("score", 0),
        "address_soft_match": diagnostic.get("address_soft_match", False),
        "failed_checks": [name for name, ok in checks.items() if not ok],
        "product_checks": diagnostic.get("products") or [],
    }


def diagnose_skladbot_matches(marker="", limit=20, request_limit=20):
    with SessionLocal() as db:
        stmt = (
            select(Order)
            .options(selectinload(Order.items))
            .where(~Order.status.in_(COMPLETED_STATUSES))
            .order_by(Order.order_date.asc(), Order.client.asc())
            .limit(limit)
        )
        if marker:
            stmt = stmt.where(active_order_condition(marker))
        orders = db.execute(stmt).scalars().all()

    if not orders:
        return {
            "status": "ok",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "marker": marker,
            "active_orders": 0,
            "candidate_requests": 0,
            "orders": [],
        }

    original_limit = os.environ.get("SKLADBOT_REQUESTS_LIMIT")
    if request_limit:
        os.environ["SKLADBOT_REQUESTS_LIMIT"] = str(max(1, request_limit))
    try:
        requests = fetch_candidate_requests(orders=orders)
    finally:
        if original_limit is None:
            os.environ.pop("SKLADBOT_REQUESTS_LIMIT", None)
        else:
            os.environ["SKLADBOT_REQUESTS_LIMIT"] = original_limit

    result_orders = []
    for order in orders:
        diagnostics = []
        for request in requests:
            diagnostic = request_match_diagnostics(order, request)
            diagnostics.append((diagnostic.get("score", 0), request_payload(request, diagnostic)))
        diagnostics.sort(key=lambda item: (item[0], item[1].get("matched")), reverse=True)
        result_orders.append({
            "order": order_payload(order),
            "matched_requests": [item for _score, item in diagnostics if item.get("matched")],
            "nearest_requests": [item for _score, item in diagnostics[:5]],
        })

    return {
        "status": "ok",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "marker": marker,
        "active_orders": len(orders),
        "candidate_requests": len(requests),
        "orders": result_orders,
    }


def main():
    parser = argparse.ArgumentParser(description="Read-only SkladBot matching diagnostic.")
    parser.add_argument("--marker", default="", help="Optional marker to restrict active backend orders.")
    parser.add_argument("--limit", type=int, default=20, help="Max active orders to inspect.")
    parser.add_argument("--request-limit", type=int, default=20, help="Max recent SkladBot requests to fetch.")
    args = parser.parse_args()
    print(json.dumps(
        diagnose_skladbot_matches(marker=args.marker, limit=args.limit, request_limit=args.request_limit),
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
