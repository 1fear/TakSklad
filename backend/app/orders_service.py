import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .models import AuditLog, Order, OrderItem, ScanCode
from .schemas import OrderItemRead, OrderRead, ScanCreate, ScanRead


STATUS_COMPLETED = "completed"
STATUS_NOT_COMPLETED = "not_completed"
COMPLETED_STATUSES = (STATUS_COMPLETED, "done", "closed")


class ApiError(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


def parse_uuid(value, field_name="id"):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ApiError(422, f"Invalid {field_name}") from exc


def list_active_orders(db: Session):
    stmt = (
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(~Order.status.in_(COMPLETED_STATUSES))
        .order_by(Order.order_date.asc(), Order.created_at.asc())
    )
    return [order_to_read(order) for order in db.execute(stmt).scalars().all()]


def create_scan(db: Session, payload: ScanCreate):
    order_item_id = parse_uuid(payload.order_item_id, "order_item_id")
    code = payload.code.strip()
    if not code:
        raise ApiError(422, "Code must not be empty")

    item = db.execute(
        select(OrderItem)
        .options(selectinload(OrderItem.order))
        .where(OrderItem.id == order_item_id)
        .with_for_update()
    ).scalar_one_or_none()
    if item is None:
        raise ApiError(404, "Order item not found")
    if item.status in COMPLETED_STATUSES or (
        item.quantity_blocks > 0 and item.scanned_blocks >= item.quantity_blocks
    ):
        raise ApiError(409, "Order item is already fully scanned")

    existing_scan = db.execute(select(ScanCode).where(ScanCode.code == code)).scalar_one_or_none()
    if existing_scan is not None:
        raise ApiError(409, "Code already scanned")

    scan_id = uuid.uuid4()
    scan = ScanCode(
        id=scan_id,
        order_item_id=item.id,
        code=code,
        workstation_id=payload.workstation_id,
        scanned_by=payload.scanned_by,
        scanned_at=payload.scanned_at or datetime.now(timezone.utc),
        raw_payload=payload.raw_payload,
    )
    db.add(scan)

    item.scanned_blocks += 1
    if item.quantity_blocks > 0 and item.scanned_blocks >= item.quantity_blocks:
        item.status = STATUS_COMPLETED
    else:
        item.status = STATUS_NOT_COMPLETED

    db.add(AuditLog(
        action="scan_code_created",
        entity_type="scan_code",
        entity_id=str(scan_id),
        payload={
            "order_item_id": str(item.id),
            "code": code,
            "workstation_id": payload.workstation_id,
            "scanned_by": payload.scanned_by,
        },
    ))

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, "Code already scanned") from exc

    db.refresh(scan)
    db.refresh(item)
    return ScanRead(
        id=str(scan.id),
        order_item_id=str(item.id),
        code=scan.code,
        scanned_blocks=item.scanned_blocks,
        item_status=item.status,
        scanned_at=scan.scanned_at,
    )


def complete_order(db: Session, order_id):
    parsed_order_id = parse_uuid(order_id, "order_id")
    order = db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(Order.id == parsed_order_id)
        .with_for_update()
    ).scalar_one_or_none()
    if order is None:
        raise ApiError(404, "Order not found")
    if order.status in COMPLETED_STATUSES:
        return order_to_read(order)

    incomplete_items = [
        item
        for item in order.items
        if item.requires_kiz and item.quantity_blocks > 0 and item.scanned_blocks < item.quantity_blocks
    ]
    if incomplete_items:
        raise ApiError(409, {
            "message": "Order has incomplete required items",
            "items": [
                {
                    "id": str(item.id),
                    "product": item.product,
                    "required_blocks": item.quantity_blocks,
                    "scanned_blocks": item.scanned_blocks,
                }
                for item in incomplete_items
            ],
        })

    for item in order.items:
        item.status = STATUS_COMPLETED
    order.status = STATUS_COMPLETED
    db.add(AuditLog(
        action="order_completed",
        entity_type="order",
        entity_id=str(order.id),
        payload={"items_count": len(order.items)},
    ))
    db.commit()
    db.refresh(order)
    return order_to_read(order)


def order_to_read(order: Order):
    raw_payload = order.raw_payload or {}
    return OrderRead(
        id=str(order.id),
        order_date=order.order_date,
        payment_type=order.payment_type,
        client=order.client,
        address=order.address,
        representative=order.representative,
        status=order.status,
        skladbot_request_number=raw_payload.get("skladbot_request_number") or "",
        skladbot_request_id=raw_payload.get("skladbot_request_id") or "",
        items=[
            item_to_read(item)
            for item in sorted(order.items, key=lambda value: (str(value.created_at or ""), str(value.id)))
        ],
    )


def item_to_read(item: OrderItem):
    return OrderItemRead(
        id=str(item.id),
        product=item.product,
        quantity_pieces=item.quantity_pieces,
        quantity_blocks=item.quantity_blocks,
        scanned_blocks=item.scanned_blocks,
        status=item.status,
        scan_codes=[
            scan.code
            for scan in sorted(item.scan_codes, key=lambda value: (str(value.scanned_at or ""), str(value.id)))
        ],
    )
