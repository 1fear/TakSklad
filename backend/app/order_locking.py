from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from .kiz_movements_service import lock_kiz_codes_for_transaction
from .models import Order, OrderItem, ScanCode


def lock_order_graphs_for_kiz(db: Session, order_ids, *, additional_codes=()):
    """Lock KIZ writer state in one global order

    The protocol is orders, then order items, then scan rows, then ordered KIZ
    advisory locks  Every runtime and repair writer must enter through this
    function before mutating the graph
    """

    normalized_order_ids = sorted({value for value in order_ids if value}, key=str)
    orders = []
    if normalized_order_ids:
        orders = db.execute(
            select(Order)
            .where(Order.id.in_(normalized_order_ids))
            .order_by(Order.id.asc())
            .with_for_update(of=Order)
            .execution_options(populate_existing=True)
        ).scalars().all()

    locked_order_ids = [order.id for order in orders]
    items = []
    if locked_order_ids:
        items = db.execute(
            select(OrderItem)
            .where(OrderItem.order_id.in_(locked_order_ids))
            .order_by(OrderItem.order_id.asc(), OrderItem.created_at.asc(), OrderItem.id.asc())
            .with_for_update(of=OrderItem)
            .execution_options(populate_existing=True)
        ).scalars().all()

    item_ids = [item.id for item in items]
    scans = []
    if item_ids:
        scans = db.execute(
            select(ScanCode)
            .where(ScanCode.order_item_id.in_(item_ids))
            .order_by(ScanCode.order_item_id.asc(), ScanCode.scanned_at.asc(), ScanCode.id.asc())
            .with_for_update(of=ScanCode)
            .execution_options(populate_existing=True)
        ).scalars().all()

    lock_kiz_codes_for_transaction(
        db,
        [*(scan.code for scan in scans), *additional_codes],
    )
    scans_by_item_id = defaultdict(list)
    for scan in scans:
        scans_by_item_id[scan.order_item_id].append(scan)
    return orders, items, dict(scans_by_item_id)


def lock_order_item_for_kiz(db: Session, order_item_id, *, additional_codes=()):
    order_id = db.execute(
        select(OrderItem.order_id).where(OrderItem.id == order_item_id)
    ).scalar_one_or_none()
    if order_id is None:
        return None, None, []

    orders, items, scans_by_item_id = lock_order_graphs_for_kiz(
        db,
        [order_id],
        additional_codes=additional_codes,
    )
    order = next((value for value in orders if value.id == order_id), None)
    item = next((value for value in items if value.id == order_item_id), None)
    return order, item, list(scans_by_item_id.get(order_item_id) or [])


def lock_repair_targets_for_kiz(db: Session, targets):
    """Lock direct repair targets without allowing a tool-specific lock order"""

    prepared = [
        (order_id, item_id, code)
        for order_id, item_id, code in targets
        if order_id and item_id and code
    ]
    orders, items, scans_by_item_id = lock_order_graphs_for_kiz(
        db,
        [order_id for order_id, _item_id, _code in prepared],
        additional_codes=[code for _order_id, _item_id, code in prepared],
    )
    locked_item_ids = {item.id for item in items}
    missing_item_ids = sorted(
        {item_id for _order_id, item_id, _code in prepared} - locked_item_ids,
        key=str,
    )
    if missing_item_ids:
        raise RuntimeError("KIZ repair target disappeared while acquiring writer locks")
    return orders, items, scans_by_item_id
