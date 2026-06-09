import uuid
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .google_sheets_pending import queue_google_sheets_export
from .kiz_movements_service import (
    MOVEMENT_RETURN,
    MOVEMENT_UNDO,
    find_other_item_scan,
    find_same_item_scan,
    kiz_is_available_for_outbound,
    latest_kiz_movement,
    outbound_movement_type_for,
    record_kiz_movement,
)
from .models import AuditLog, Order, OrderItem, ScanCode
from .schemas import OrderItemRead, OrderRead, ScanCreate, ScanRead, ScanUndo


STATUS_COMPLETED = "completed"
STATUS_NOT_COMPLETED = "not_completed"
STATUS_RETURNED = "returned"
STATUS_ARCHIVED_NO_KIZ = "archived_no_kiz"
STATUS_CANCELLED = "cancelled"
STATUS_REMOVED_FROM_GOOGLE = "removed_from_google_sheet"
COMPLETED_STATUSES = (STATUS_COMPLETED, "done", "closed", STATUS_RETURNED)
INACTIVE_ORDER_STATUSES = (*COMPLETED_STATUSES, STATUS_ARCHIVED_NO_KIZ, STATUS_CANCELLED)
HIDDEN_ITEM_STATUSES = (STATUS_REMOVED_FROM_GOOGLE,)
logger = logging.getLogger(__name__)


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
        .where(~Order.status.in_(INACTIVE_ORDER_STATUSES))
        .order_by(Order.order_date.asc(), Order.created_at.asc())
    )
    active_orders = []
    for order in db.execute(stmt).scalars().all():
        read_order = order_to_read(order)
        if read_order.items:
            active_orders.append(read_order)
    return active_orders


def list_returned_orders(db: Session, limit=50):
    stmt = (
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(Order.status == STATUS_RETURNED)
        .order_by(Order.updated_at.desc(), Order.order_date.desc(), Order.created_at.desc())
        .limit(max(1, min(int(limit or 50), 200)))
    )
    return [order_to_read(order) for order in db.execute(stmt).scalars().all()]


def create_scan(db: Session, payload: ScanCreate):
    order_item_id = parse_uuid(payload.order_item_id, "order_item_id")
    code = str(payload.code or "").strip(" \t\r\n")
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

    same_item_scan = find_same_item_scan(db, code=code, order_item_id=item.id)
    if same_item_scan is not None:
        return scan_to_read(same_item_scan, item)

    other_item_scan = find_other_item_scan(db, code=code, order_item_id=item.id)
    latest_movement = latest_kiz_movement(db, code)
    if other_item_scan is not None and latest_movement is None:
        return existing_scan_response_or_error(other_item_scan, item)
    if other_item_scan is not None and not kiz_is_available_for_outbound(latest_movement):
        return existing_scan_response_or_error(other_item_scan, item)
    if other_item_scan is None and latest_movement is not None and not kiz_is_available_for_outbound(latest_movement):
        raise ApiError(409, {
            "message": "Code already scanned in another order item",
            "existing_order_item_id": str(latest_movement.order_item_id or ""),
            "order_item_id": str(item.id),
        })
    movement_type = outbound_movement_type_for(latest_movement)

    if item.status in COMPLETED_STATUSES or (
        item.quantity_blocks > 0 and item.scanned_blocks >= item.quantity_blocks
    ):
        raise ApiError(409, "Order item is already fully scanned")

    scan_id = uuid.uuid4()
    scan_raw_payload = dict(payload.raw_payload or {})
    if payload.scanned_at:
        scan_raw_payload.setdefault("scanned_at", payload.scanned_at.isoformat())

    scan = ScanCode(
        id=scan_id,
        order_item_id=item.id,
        code=code,
        workstation_id=payload.workstation_id,
        scanned_by=payload.scanned_by,
        scanned_at=payload.scanned_at or datetime.now(timezone.utc),
        raw_payload=scan_raw_payload,
    )
    db.add(scan)
    db.flush()
    movement = record_kiz_movement(
        db,
        code=code,
        movement_type=movement_type,
        order_id=item.order_id,
        order_item_id=item.id,
        scan_code_id=scan_id,
        source="backend",
        actor=payload.scanned_by or "",
        workstation_id=payload.workstation_id or "",
        occurred_at=scan.scanned_at,
        raw_payload={
            "scan_source": scan.source,
            "previous_movement_type": latest_movement.movement_type if latest_movement else "",
            "previous_order_item_id": str(latest_movement.order_item_id or "") if latest_movement else "",
        },
    )

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
            "kiz_movement_id": str(movement.id) if movement else "",
            "kiz_movement_type": movement_type,
        },
    ))

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        item = db.execute(select(OrderItem).where(OrderItem.id == order_item_id)).scalar_one_or_none()
        if item is not None:
            same_item_scan = find_same_item_scan(db, code=code, order_item_id=item.id)
            if same_item_scan is not None:
                return scan_to_read(same_item_scan, item)
            other_item_scan = find_other_item_scan(db, code=code, order_item_id=item.id)
            if other_item_scan is not None:
                return existing_scan_response_or_error(other_item_scan, item)
        raise ApiError(409, "Code already scanned") from exc

    db.refresh(scan)
    db.refresh(item)
    response = scan_to_read(scan, item)
    export_order_item_scan_to_google_sheets_best_effort(db, item.id)
    return response


def undo_scan(db: Session, payload: ScanUndo):
    order_item_id = parse_uuid(payload.order_item_id, "order_item_id")
    code = str(payload.code or "").strip(" \t\r\n")
    if not code:
        raise ApiError(422, "Code must not be empty")

    item = db.execute(
        select(OrderItem)
        .options(selectinload(OrderItem.order), selectinload(OrderItem.scan_codes))
        .where(OrderItem.id == order_item_id)
        .with_for_update()
    ).scalar_one_or_none()
    if item is None:
        raise ApiError(404, "Order item not found")
    if item.order.status in INACTIVE_ORDER_STATUSES:
        raise ApiError(409, "Cannot undo scan for inactive order")

    scan = db.execute(
        select(ScanCode)
        .where(ScanCode.order_item_id == item.id)
        .where(ScanCode.code == code)
        .with_for_update()
    ).scalar_one_or_none()
    if scan is None:
        existing_scan = db.execute(select(ScanCode).where(ScanCode.code == code)).scalar_one_or_none()
        if existing_scan is not None:
            raise ApiError(409, {
                "message": "Code belongs to another order item",
                "existing_order_item_id": str(existing_scan.order_item_id),
                "order_item_id": str(item.id),
            })
        raise ApiError(404, "Scan code was not found for this order item")

    scan_id = scan.id
    scanned_at = scan.scanned_at
    movement = record_kiz_movement(
        db,
        code=code,
        movement_type=MOVEMENT_UNDO,
        order_id=item.order_id,
        order_item_id=item.id,
        scan_code_id=scan_id,
        source="backend",
        actor=payload.actor or "",
        workstation_id=payload.workstation_id or "",
        occurred_at=datetime.now(timezone.utc),
        raw_payload={
            "undone_scan_scanned_at": scanned_at.isoformat() if scanned_at else "",
        },
    )
    db.delete(scan)
    remaining_codes = [
        existing.code
        for existing in item.scan_codes
        if existing.id != scan_id
    ]
    item.scanned_blocks = len(remaining_codes)
    if item.quantity_blocks > 0 and item.scanned_blocks >= item.quantity_blocks:
        item.status = STATUS_COMPLETED
    else:
        item.status = STATUS_NOT_COMPLETED
    if item.order.status not in INACTIVE_ORDER_STATUSES:
        item.order.status = STATUS_NOT_COMPLETED

    db.add(AuditLog(
        action="scan_code_deleted",
        entity_type="scan_code",
        entity_id=str(scan_id),
        payload={
            "order_item_id": str(item.id),
            "code": code,
            "workstation_id": payload.workstation_id,
            "actor": payload.actor,
            "kiz_movement_id": str(movement.id) if movement else "",
        },
    ))
    db.commit()
    db.refresh(item)
    response = ScanRead(
        id=str(scan_id),
        order_item_id=str(item.id),
        code=code,
        scanned_blocks=item.scanned_blocks,
        item_status=item.status,
        scanned_at=scanned_at,
    )
    export_order_item_scan_to_google_sheets_best_effort(db, item.id)
    return response


def existing_scan_response_or_error(existing_scan, item):
    if existing_scan.order_item_id == item.id:
        return scan_to_read(existing_scan, item)
    raise ApiError(409, {
        "message": "Code already scanned in another order item",
        "existing_order_item_id": str(existing_scan.order_item_id),
        "order_item_id": str(item.id),
    })


def scan_to_read(scan, item):
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
    if order.status in (STATUS_ARCHIVED_NO_KIZ, STATUS_CANCELLED):
        raise ApiError(409, "Order is not active")
    if order.status in COMPLETED_STATUSES:
        response = order_to_read(order)
        if order.status != STATUS_RETURNED:
            export_order_archive_to_google_sheets_best_effort(db, order.id)
        return response

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
    response = order_to_read(order)
    export_order_archive_to_google_sheets_best_effort(db, order.id)
    return response


def lookup_return_order(db: Session, lookup_value):
    lookup = normalize_text(lookup_value)
    if not lookup:
        raise ApiError(422, "Return lookup value is required")

    orders = db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(Order.status.in_(COMPLETED_STATUSES))
        .order_by(Order.order_date.desc(), Order.created_at.desc())
    ).scalars().all()

    matches = [
        order
        for order in orders
        if return_lookup_matches(order, lookup)
    ]
    if not matches:
        raise ApiError(404, "Completed order was not found in archive")
    if len(matches) > 1:
        raise ApiError(409, {
            "message": "Multiple completed orders found for return lookup",
            "orders": [
                {
                    "id": str(order.id),
                    "order_date": order.order_date.isoformat() if order.order_date else "",
                    "client": order.client,
                    "skladbot_request_number": (order.raw_payload or {}).get("skladbot_request_number") or "",
                    "skladbot_request_id": (order.raw_payload or {}).get("skladbot_request_id") or "",
                }
                for order in matches[:10]
            ],
        })
    return order_to_read(matches[0])


def mark_order_returned(db: Session, order_id, return_reference="", returned_by="desktop", confirmed_items=None):
    parsed_order_id = parse_uuid(order_id, "order_id")
    order = db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(Order.id == parsed_order_id)
        .with_for_update()
    ).scalar_one_or_none()
    if order is None:
        raise ApiError(404, "Order not found")
    raw_payload = dict(order.raw_payload or {})
    if order.status == STATUS_RETURNED or raw_payload.get("return_status") == "returned":
        raise ApiError(409, "Order is already returned")
    if order.status not in COMPLETED_STATUSES:
        raise ApiError(409, "Only completed archived orders can be returned")

    confirmed = validate_return_confirmed_items(order, confirmed_items)
    returned_at = datetime.now(timezone.utc)
    raw_payload["return_status"] = "returned"
    raw_payload["returned_at"] = returned_at.isoformat()
    raw_payload["return_reference"] = normalize_text(return_reference)
    raw_payload["returned_by"] = normalize_text(returned_by) or "desktop"
    raw_payload["skladbot_return_confirmed_items"] = confirmed
    order.raw_payload = raw_payload
    order.status = STATUS_RETURNED
    from .skladbot_return_requests import queue_skladbot_return_request_create

    event = queue_skladbot_return_request_create(db, order, confirmed)
    return_movements = []
    for item in order.items:
        for scan in item.scan_codes or []:
            movement = record_kiz_movement(
                db,
                code=scan.code,
                movement_type=MOVEMENT_RETURN,
                order_id=order.id,
                order_item_id=item.id,
                scan_code_id=scan.id,
                return_reference=raw_payload["return_reference"],
                source="backend",
                actor=raw_payload["returned_by"],
                occurred_at=returned_at,
                raw_payload={
                    "return_reference": raw_payload["return_reference"],
                    "returned_by": raw_payload["returned_by"],
                },
            )
            if movement is not None:
                return_movements.append(str(movement.id))

    db.add(AuditLog(
        action="order_returned",
        entity_type="order",
        entity_id=str(order.id),
        payload={
            "return_reference": raw_payload["return_reference"],
            "returned_by": raw_payload["returned_by"],
            "returned_at": raw_payload["returned_at"],
            "confirmed_items": confirmed,
            "skladbot_return_create_event_id": str(event.id) if event else "",
            "kiz_return_movements": return_movements,
        },
    ))
    db.commit()
    db.refresh(order)
    response = order_to_read(order)
    export_order_archive_to_google_sheets_best_effort(db, order.id)
    export_order_return_to_google_sheets_best_effort(db, order.id)
    return response


def validate_return_confirmed_items(order, confirmed_items):
    items = [
        item
        for item in order.items
        if item.status not in HIDDEN_ITEM_STATUSES
    ]
    if not items:
        raise ApiError(422, "Order has no returnable items")
    if not confirmed_items:
        raise ApiError(422, "Return confirmed_items are required")

    by_id = {str(item.id): item for item in items}
    used_ids = set()
    confirmed = []
    for raw_item in confirmed_items or []:
        if hasattr(raw_item, "model_dump"):
            raw_item = raw_item.model_dump()
        raw_item = raw_item if isinstance(raw_item, dict) else {}
        item_id = normalize_text(raw_item.get("item_id") or raw_item.get("order_item_id") or raw_item.get("id"))
        if not item_id or item_id not in by_id:
            raise ApiError(422, f"Return item does not belong to order: {item_id or 'empty'}")
        if item_id in used_ids:
            raise ApiError(422, f"Duplicate return item: {item_id}")
        item = by_id[item_id]
        product = normalize_text(raw_item.get("product") or raw_item.get("sku"))
        quantity_blocks = parse_int(raw_item.get("quantity_blocks"))
        quantity_pieces = parse_int(raw_item.get("quantity_pieces"))
        if normalize_lookup(product) != normalize_lookup(item.product):
            raise ApiError(422, f"Return SKU mismatch for {item.product}")
        if quantity_blocks != int(item.quantity_blocks or 0):
            raise ApiError(422, f"Return blocks mismatch for {item.product}")
        if quantity_pieces and quantity_pieces != int(item.quantity_pieces or 0):
            raise ApiError(422, f"Return pieces mismatch for {item.product}")
        used_ids.add(item_id)
        confirmed.append({
            "item_id": item_id,
            "product": item.product,
            "sku": item.product,
            "quantity_blocks": int(item.quantity_blocks or 0),
            "quantity_pieces": int(item.quantity_pieces or 0),
        })

    missing_items = [item.product for item in items if str(item.id) not in used_ids]
    if missing_items:
        raise ApiError(422, f"Return confirmation is incomplete: {', '.join(missing_items)}")
    return confirmed


def export_order_item_scan_to_google_sheets_best_effort(db: Session, item_id):
    item = db.execute(
        select(OrderItem)
        .options(selectinload(OrderItem.order), selectinload(OrderItem.scan_codes))
        .where(OrderItem.id == item_id)
    ).scalar_one_or_none()
    if item is None:
        return
    record_google_sheets_export_result(
        db,
        action="google_sheets_scan_export",
        entity_type="order_item",
        entity_id=str(item.id),
    )


def export_order_archive_to_google_sheets_best_effort(db: Session, order_id):
    order = db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(Order.id == order_id)
    ).scalar_one_or_none()
    if order is None:
        return
    record_google_sheets_export_result(
        db,
        action="google_sheets_archive_export",
        entity_type="order",
        entity_id=str(order.id),
    )


def export_order_return_to_google_sheets_best_effort(db: Session, order_id):
    order = db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(Order.id == order_id)
    ).scalar_one_or_none()
    if order is None:
        return
    record_google_sheets_export_result(
        db,
        action="google_sheets_return_export",
        entity_type="order",
        entity_id=str(order.id),
    )


def record_google_sheets_export_result(db: Session, action, entity_type, entity_id):
    result = {"status": "queued", "queued": True, "error": ""}
    try:
        event = queue_google_sheets_export(db, action, entity_type, entity_id, result=result)
        result = {**result, "pending_event_id": str(event.id) if event else ""}
        db.add(AuditLog(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=result,
        ))
        db.commit()
    except Exception:
        logger.exception("Failed to store Google Sheets export audit: %s", action)
        db.rollback()


def return_lookup_matches(order, lookup):
    raw_payload = order.raw_payload or {}
    candidates = [
        raw_payload.get("skladbot_request_number"),
        raw_payload.get("skladbot_request_id"),
        order.external_id,
    ]
    return normalize_lookup(lookup) in {normalize_lookup(value) for value in candidates if normalize_text(value)}


def order_to_read(order: Order):
    raw_payload = order.raw_payload or {}
    return OrderRead(
        id=str(order.id),
        order_date=order.order_date,
        payment_type=order.payment_type,
        client=order.client,
        address=order.address,
        coordinates=raw_payload.get("coordinates") or "",
        representative=order.representative,
        status=order.status,
        skladbot_request_number=raw_payload.get("skladbot_request_number") or "",
        skladbot_request_id=raw_payload.get("skladbot_request_id") or "",
        skladbot_return_request_number=raw_payload.get("skladbot_return_request_number") or "",
        skladbot_return_request_id=raw_payload.get("skladbot_return_request_id") or "",
        skladbot_return_status=raw_payload.get("skladbot_return_request_status") or "",
        return_status=raw_payload.get("return_status") or "",
        returned_at=raw_payload.get("returned_at") or "",
        return_reference=raw_payload.get("return_reference") or "",
        items=[
            item_to_read(item)
            for item in sorted(order.items, key=lambda value: (str(value.created_at or ""), str(value.id)))
            if item.status not in HIDDEN_ITEM_STATUSES
        ],
    )


def item_to_read(item: OrderItem):
    raw_payload = item.raw_payload or {}
    return OrderItemRead(
        id=str(item.id),
        product=item.product,
        quantity_pieces=item.quantity_pieces,
        quantity_blocks=item.quantity_blocks,
        scanned_blocks=item.scanned_blocks,
        block_price=parse_int(raw_payload.get("block_price")),
        line_total=parse_int(raw_payload.get("line_total")),
        status=item.status,
        scan_codes=[
            scan.code
            for scan in sorted(item.scan_codes, key=lambda value: (str(value.scanned_at or ""), str(value.id)))
        ],
    )


def parse_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_text(value):
    return str(value or "").strip()


def normalize_lookup(value):
    return "".join(char.casefold() for char in normalize_text(value) if char.isalnum())
