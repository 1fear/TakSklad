import uuid
from datetime import date, datetime, timezone

from sqlalchemy import and_, exists, or_, select, text, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.orm.attributes import set_committed_value

from .kiz_movements_service import (
    MOVEMENT_RETURN,
    MOVEMENT_UNDO,
    find_item_scans,
    find_other_item_scan,
    find_same_item_scan,
    kiz_is_available_for_outbound,
    latest_kiz_movement,
    lookup_kiz_state,
    lock_kiz_code_for_transaction,
    normalize_kiz_code,
    outbound_movement_type_for,
    record_kiz_movement,
    record_kiz_movements,
)
from .models import AuditLog, Order, OrderItem, ScanCode
from .pagination import CursorError, decode_cursor, encode_cursor, normalize_page_limit
from . import outbox_service
from .order_statuses import (
    COMPLETED_STATUSES,
    HIDDEN_ITEM_STATUSES,
    INACTIVE_ORDER_STATUSES,
    STATUS_ARCHIVED_NO_KIZ,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_NOT_COMPLETED,
    STATUS_REMOVED_FROM_GOOGLE,
    STATUS_RETURNED,
)
from .schemas import KizAvailabilityRead, OrderItemRead, OrderRead, ScanCreate, ScanRead, ScanUndo
from .skladbot_contracts import format_internal_smartup_ids
from .scan_quantities import (
    SCAN_TYPE_AGGREGATE_BOX,
    product_key_from_name,
    scan_block_quantity,
    scan_metadata_for_code,
    scan_product_mismatch,
    scanned_blocks_for_scans,
)
from .transfer_kiz_service import (
    queue_transfer_kiz_completion_check,
    queue_transfer_kiz_undo_alert,
)


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


def list_active_orders_page(db: Session, *, limit=50, cursor=""):
    row_limit = normalize_page_limit(limit, default=50, maximum=200)
    id_stmt = (
        select(Order.id, Order.order_date, Order.created_at)
        .where(~Order.status.in_(INACTIVE_ORDER_STATUSES))
        .where(exists(select(OrderItem.id).where(OrderItem.order_id == Order.id)))
        .order_by(Order.order_date.asc().nulls_last(), Order.created_at.asc(), Order.id.asc())
        .limit(row_limit + 1)
    )
    if cursor:
        try:
            cursor_date, cursor_created_at, cursor_id = decode_cursor(cursor, "orders.active")
            parsed_date = date.fromisoformat(str(cursor_date)) if cursor_date else None
            parsed_created_at = datetime.fromisoformat(str(cursor_created_at).replace("Z", "+00:00"))
            parsed_id = uuid.UUID(str(cursor_id))
        except (CursorError, TypeError, ValueError):
            raise CursorError("invalid_cursor") from None
        if parsed_date is None:
            id_stmt = id_stmt.where(and_(
                Order.order_date.is_(None),
                tuple_(Order.created_at, Order.id) > tuple_(parsed_created_at, parsed_id),
            ))
        else:
            id_stmt = id_stmt.where(or_(
                Order.order_date > parsed_date,
                Order.order_date.is_(None),
                and_(
                    Order.order_date == parsed_date,
                    tuple_(Order.created_at, Order.id) > tuple_(parsed_created_at, parsed_id),
                ),
            ))
    id_rows = db.execute(id_stmt).all()
    page_rows = id_rows[:row_limit]
    page_ids = [row.id for row in page_rows]
    if not page_ids:
        return [], "", row_limit
    orders = db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(Order.id.in_(page_ids))
    ).scalars().all()
    by_id = {order.id: order for order in orders}
    result = [order_to_read(by_id[order_id]) for order_id in page_ids]
    next_cursor = ""
    if len(id_rows) > row_limit:
        last = page_rows[-1]
        next_cursor = encode_cursor(
            "orders.active",
            [
                last.order_date.isoformat() if last.order_date else "",
                last.created_at.isoformat(),
                str(last.id),
            ],
        )
    return result, next_cursor, row_limit


def list_active_orders(db: Session, limit=50):
    return list_active_orders_page(db, limit=limit)[0]


def list_returned_orders(db: Session, limit=50, offset=0):
    stmt = (
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(Order.status == STATUS_RETURNED)
        .order_by(Order.updated_at.desc(), Order.order_date.desc(), Order.created_at.desc())
        .limit(max(1, min(int(limit or 50), 201)))
        .offset(max(0, int(offset or 0)))
    )
    return [order_to_read(order) for order in db.execute(stmt).scalars().all()]


def lookup_kiz_availability(db: Session, code, order_item_id=""):
    code = normalize_kiz_code(code)
    if not code:
        raise ApiError(422, "Code must not be empty")

    target_item_id = parse_uuid(order_item_id, "order_item_id") if str(order_item_id or "").strip() else None
    same_item_scan = find_same_item_scan(db, code=code, order_item_id=target_item_id) if target_item_id else None
    other_item_scan = find_other_item_scan(db, code=code, order_item_id=target_item_id) if target_item_id else None
    existing_scan = other_item_scan or same_item_scan
    if existing_scan is None and target_item_id is None:
        existing_scan = db.execute(
            select(ScanCode)
            .where(ScanCode.code == code)
            .order_by(ScanCode.scanned_at.desc(), ScanCode.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    latest_movement = latest_kiz_movement(db, code)
    latest_movement_type = latest_movement.movement_type if latest_movement is not None else ""
    latest_order_item_id = str(latest_movement.order_item_id or "") if latest_movement is not None else ""
    existing_order_item_id = str(existing_scan.order_item_id or "") if existing_scan is not None else ""

    available = True
    reason = "no_backend_history"
    if same_item_scan is not None:
        available = False
        reason = "same_order_item_scan"
    elif other_item_scan is not None and latest_movement is None:
        available = False
        reason = "other_order_item_scan_without_movement"
    elif other_item_scan is not None and not kiz_is_available_for_outbound(latest_movement):
        available = False
        reason = "other_order_item_scan_busy"
    elif other_item_scan is not None:
        available = True
        reason = f"latest_movement_{latest_movement_type}_available"
    elif latest_movement is not None and not kiz_is_available_for_outbound(latest_movement):
        available = False
        reason = "latest_movement_busy"
    elif latest_movement is not None:
        available = True
        reason = f"latest_movement_{latest_movement_type}_available"

    return KizAvailabilityRead(
        code=code,
        available=available,
        reason=reason,
        latest_movement_type=latest_movement_type,
        latest_order_item_id=latest_order_item_id,
        existing_order_item_id=existing_order_item_id,
    )


def create_scan(db: Session, payload: ScanCreate):
    order_item_id = parse_uuid(payload.order_item_id, "order_item_id")
    code = str(payload.code or "").strip(" \t\r\n")
    if not code:
        raise ApiError(422, "Code must not be empty")

    item = db.execute(
        select(OrderItem)
        .options(joinedload(OrderItem.order))
        .where(OrderItem.id == order_item_id)
        .with_for_update(of=OrderItem)
    ).scalar_one_or_none()
    if item is None:
        raise ApiError(404, "Order item not found")

    lock_kiz_code_for_transaction(db, code)
    same_item_scan, other_item_scan = find_item_scans(db, code=code, order_item_id=item.id)
    if same_item_scan is not None:
        return scan_to_read(same_item_scan, item)

    kiz_code, latest_movement = lookup_kiz_state(db, code)
    if other_item_scan is not None and latest_movement is None:
        return existing_scan_response_or_error(db, other_item_scan, item)
    if other_item_scan is not None and not kiz_is_available_for_outbound(latest_movement):
        return existing_scan_response_or_error(db, other_item_scan, item)
    if other_item_scan is None and latest_movement is not None and not kiz_is_available_for_outbound(latest_movement):
        raise ApiError(409, scan_conflict_detail(db, latest_movement.order_item_id, item))
    movement_type = outbound_movement_type_for(latest_movement)

    if item.status in COMPLETED_STATUSES or (
        item.quantity_blocks > 0 and item.scanned_blocks >= item.quantity_blocks
    ):
        latest_scan = latest_scan_for_item(db, item.id)
        if latest_scan is not None:
            return scan_to_read(latest_scan, item)
        raise ApiError(409, "Order item is already fully scanned")

    scan_metadata = scan_metadata_for_code(code)
    block_quantity = scan_metadata["block_quantity"]

    item_product_key = product_key_from_name(item.product)
    if scan_metadata["scan_type"] == SCAN_TYPE_AGGREGATE_BOX:
        if not item_product_key or item_product_key != scan_metadata["aggregate_product_key"]:
            raise ApiError(409, {
                "message": "Aggregate box product does not match order item",
                "order_item_id": str(item.id),
                "product": item.product,
                "aggregate_product_key": scan_metadata["aggregate_product_key"],
            })
        remaining_blocks = max(0, int(item.quantity_blocks or 0) - int(item.scanned_blocks or 0))
        if item.quantity_blocks > 0 and block_quantity > remaining_blocks:
            raise ApiError(409, {
                "message": "Aggregate box exceeds remaining order item blocks",
                "order_item_id": str(item.id),
                "remaining_blocks": remaining_blocks,
                "block_quantity": block_quantity,
            })
    elif scan_product_mismatch(code, item.product):
        raise ApiError(409, {
            "message": "Scan product does not match order item",
            "order_item_id": str(item.id),
            "product": item.product,
            "expected_product_key": item_product_key,
            "scan_product_key": scan_metadata.get("product_key") or "",
        })

    scan_id = uuid.uuid4()
    scan_raw_payload = dict(payload.raw_payload or {})
    if payload.scanned_at:
        scan_raw_payload.setdefault("scanned_at", payload.scanned_at.isoformat())
    scan_raw_payload.update(scan_metadata)

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
    movement_received_at = datetime.now(timezone.utc)
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
        occurred_at=movement_received_at,
        raw_payload={
            "scan_source": scan.source,
            "scanner_scanned_at": scan.scanned_at.isoformat() if scan.scanned_at else "",
            "previous_movement_type": latest_movement.movement_type if latest_movement else "",
            "previous_order_item_id": str(latest_movement.order_item_id or "") if latest_movement else "",
            "scan_type": scan_metadata["scan_type"],
            "block_quantity": block_quantity,
        },
        kiz_code=kiz_code,
    )

    item.scanned_blocks += block_quantity
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
            "scan_type": scan_metadata["scan_type"],
            "block_quantity": block_quantity,
        },
    ))
    queue_transfer_kiz_completion_check(db, scan=scan, item=item)
    response = scan_to_read(scan, item)

    try:
        outbox_service.outbox_fault("before_commit", "scan")
        db.commit()
        outbox_service.outbox_fault("after_commit", "scan")
    except IntegrityError as exc:
        db.rollback()
        item = db.execute(select(OrderItem).where(OrderItem.id == order_item_id)).scalar_one_or_none()
        if item is not None:
            same_item_scan = find_same_item_scan(db, code=code, order_item_id=item.id)
            if same_item_scan is not None:
                return scan_to_read(same_item_scan, item)
            other_item_scan = find_other_item_scan(db, code=code, order_item_id=item.id)
            if other_item_scan is not None:
                return existing_scan_response_or_error(db, other_item_scan, item)
        raise ApiError(409, "Code already scanned") from exc

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

    lock_kiz_code_for_transaction(db, code)
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
    remaining_scans = [
        existing
        for existing in item.scan_codes
        if existing.id != scan_id
    ]
    remaining_codes = [existing.code for existing in remaining_scans]
    item.scanned_blocks = scanned_blocks_for_scans(remaining_scans)
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
            "scan_type": (scan.raw_payload or {}).get("scan_type") or "",
            "block_quantity": scan_block_quantity(scan),
        },
    ))
    queue_transfer_kiz_undo_alert(db, item=item)
    outbox_service.outbox_fault("before_commit", "scan")
    db.commit()
    outbox_service.outbox_fault("after_commit", "scan")
    db.refresh(item)
    response = ScanRead(
        id=str(scan_id),
        order_item_id=str(item.id),
        code=code,
        scanned_blocks=item.scanned_blocks,
        item_status=item.status,
        scanned_at=scanned_at,
        scan_type=(scan.raw_payload or {}).get("scan_type") or "unit",
        block_quantity=scan_block_quantity(scan),
    )
    return response


def existing_scan_response_or_error(db: Session, existing_scan, item):
    if existing_scan.order_item_id == item.id:
        return scan_to_read(existing_scan, item)
    raise ApiError(409, scan_conflict_detail(db, existing_scan.order_item_id, item))


def scan_conflict_detail(db: Session, existing_order_item_id, item):
    detail = {
        "message": "Code already scanned in another order item",
        "existing_order_item_id": str(existing_order_item_id or ""),
        "order_item_id": str(item.id),
    }
    existing_item = None
    if existing_order_item_id:
        existing_item = db.execute(
            select(OrderItem)
            .options(selectinload(OrderItem.order))
            .where(OrderItem.id == existing_order_item_id)
        ).scalar_one_or_none()
    if existing_item is not None and existing_item.order is not None:
        order = existing_item.order
        raw_payload = order.raw_payload or {}
        detail["existing_order"] = {
            "id": str(order.id),
            "order_item_id": str(existing_item.id),
            "order_date": str(order.order_date or ""),
            "order_date_display": format_order_date_display(order.order_date),
            "client": order.client,
            "payment_type": order.payment_type,
            "address": order.address,
            "representative": order.representative or "",
            "product": existing_item.product,
            "skladbot_request_number": raw_payload.get("skladbot_request_number") or "",
            "skladbot_request_id": raw_payload.get("skladbot_request_id") or "",
        }
    return detail


def format_order_date_display(value):
    if hasattr(value, "strftime"):
        return value.strftime("%d.%m.%Y")
    return str(value or "")


def scan_to_read(scan, item):
    return ScanRead(
        id=str(scan.id),
        order_item_id=str(item.id),
        code=scan.code,
        scanned_blocks=item.scanned_blocks,
        item_status=item.status,
        scanned_at=scan.scanned_at,
        scan_type=(scan.raw_payload or {}).get("scan_type") or "unit",
        block_quantity=scan_block_quantity(scan),
    )


def latest_scan_for_item(db: Session, order_item_id):
    return db.execute(
        select(ScanCode)
        .where(ScanCode.order_item_id == order_item_id)
        .order_by(ScanCode.scanned_at.desc(), ScanCode.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def complete_order(db: Session, order_id):
    parsed_order_id = parse_uuid(order_id, "order_id")
    order = db.execute(
        select(Order)
        .options(joinedload(Order.items).joinedload(OrderItem.scan_codes))
        .where(Order.id == parsed_order_id)
        .with_for_update(of=Order)
    ).unique().scalar_one_or_none()
    if order is None:
        raise ApiError(404, "Order not found")
    if order.status in (STATUS_ARCHIVED_NO_KIZ, STATUS_CANCELLED):
        raise ApiError(409, "Order is not active")
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

    db.add(AuditLog(
        action="order_completed",
        entity_type="order",
        entity_id=str(order.id),
        payload={"items_count": len(order.items)},
    ))
    if db.get_bind().dialect.name == "postgresql":
        with db.no_autoflush:
            updated_order_count, updated_item_count = db.execute(text("""
                WITH updated_order AS (
                    UPDATE orders SET status=:status, updated_at=now()
                    WHERE id=:order_id RETURNING id
                ), updated_items AS (
                    UPDATE order_items SET status=:status, updated_at=now()
                    WHERE order_id=:order_id RETURNING id
                )
                SELECT
                    (SELECT count(*) FROM updated_order),
                    (SELECT count(*) FROM updated_items)
            """), {"status": STATUS_COMPLETED, "order_id": order.id}).one()
        if int(updated_order_count) != 1 or int(updated_item_count) != len(order.items):
            raise RuntimeError("complete order status update cardinality mismatch")
        set_committed_value(order, "status", STATUS_COMPLETED)
        for item in order.items:
            set_committed_value(item, "status", STATUS_COMPLETED)
    else:
        for item in order.items:
            item.status = STATUS_COMPLETED
        order.status = STATUS_COMPLETED
    response = order_to_read(order)
    outbox_service.outbox_fault("before_commit", "complete")
    db.commit()
    outbox_service.outbox_fault("after_commit", "complete")
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
    movement_records = []
    for item in order.items:
        for scan in item.scan_codes or []:
            movement_records.append({
                "code": scan.code,
                "movement_type": MOVEMENT_RETURN,
                "order_id": order.id,
                "order_item_id": item.id,
                "scan_code_id": scan.id,
                "return_reference": raw_payload["return_reference"],
                "source": "backend",
                "actor": raw_payload["returned_by"],
                "occurred_at": returned_at,
                "raw_payload": {
                    "return_reference": raw_payload["return_reference"],
                    "returned_by": raw_payload["returned_by"],
                },
            })
    return_movements = [
        str(movement.id)
        for movement in record_kiz_movements(db, movement_records, lock_codes=True)
    ]

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
    response = order_to_read(order)
    outbox_service.outbox_fault("before_commit", "return")
    db.commit()
    outbox_service.outbox_fault("after_commit", "return")
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
        smartup_id=format_internal_smartup_ids([
            raw_payload.get("source_order_id"),
            *((item.raw_payload or {}).get("source_order_id") for item in order.items),
        ]),
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
    scans = sorted(item.scan_codes, key=lambda value: (str(value.scanned_at or ""), str(value.id)))
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
            for scan in scans
        ],
        scan_entries=[
            {
                "code": scan.code,
                "scan_type": (scan.raw_payload or {}).get("scan_type") or "unit",
                "block_quantity": scan_block_quantity(scan),
                "scanned_at": scan.scanned_at,
            }
            for scan in scans
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
