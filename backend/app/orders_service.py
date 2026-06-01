import uuid
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .google_sheets_exporter import (
    archive_backend_order_to_google_sheets,
    mark_backend_order_returned_in_google_sheets,
    sync_backend_order_item_to_google_sheets,
)
from .google_sheets_pending import (
    mark_google_sheets_export_synced,
    queue_google_sheets_export,
    should_queue_google_sheets_export,
)
from .models import AuditLog, Order, OrderItem, ScanCode
from .schemas import OrderItemRead, OrderRead, ScanCreate, ScanRead


STATUS_COMPLETED = "completed"
STATUS_NOT_COMPLETED = "not_completed"
STATUS_RETURNED = "returned"
STATUS_REMOVED_FROM_GOOGLE = "removed_from_google_sheet"
COMPLETED_STATUSES = (STATUS_COMPLETED, "done", "closed", STATUS_RETURNED)
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
        .where(~Order.status.in_(COMPLETED_STATUSES))
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
        return existing_scan_response_or_error(existing_scan, item)

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
        existing_scan = db.execute(select(ScanCode).where(ScanCode.code == code)).scalar_one_or_none()
        item = db.execute(select(OrderItem).where(OrderItem.id == order_item_id)).scalar_one_or_none()
        if existing_scan is not None and item is not None:
            return existing_scan_response_or_error(existing_scan, item)
        raise ApiError(409, "Code already scanned") from exc

    db.refresh(scan)
    db.refresh(item)
    response = scan_to_read(scan, item)
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


def mark_order_returned(db: Session, order_id, return_reference="", returned_by="desktop"):
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

    returned_at = datetime.now(timezone.utc)
    raw_payload["return_status"] = "returned"
    raw_payload["returned_at"] = returned_at.isoformat()
    raw_payload["return_reference"] = normalize_text(return_reference)
    raw_payload["returned_by"] = normalize_text(returned_by) or "desktop"
    order.raw_payload = raw_payload
    order.status = STATUS_RETURNED

    db.add(AuditLog(
        action="order_returned",
        entity_type="order",
        entity_id=str(order.id),
        payload={
            "return_reference": raw_payload["return_reference"],
            "returned_by": raw_payload["returned_by"],
            "returned_at": raw_payload["returned_at"],
        },
    ))
    db.commit()
    db.refresh(order)
    response = order_to_read(order)
    export_order_archive_to_google_sheets_best_effort(db, order.id)
    export_order_return_to_google_sheets_best_effort(db, order.id)
    return response


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
        callback=lambda: sync_backend_order_item_to_google_sheets(item),
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
        callback=lambda: archive_backend_order_to_google_sheets(order),
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
        callback=lambda: mark_backend_order_returned_in_google_sheets(order),
    )


def record_google_sheets_export_result(db: Session, action, entity_type, entity_id, callback):
    try:
        result = callback()
    except Exception as exc:
        logger.exception("Google Sheets export failed: %s", action)
        result = {"status": "error", "error": str(exc)}

    try:
        if should_queue_google_sheets_export(result):
            event = queue_google_sheets_export(db, action, entity_type, entity_id, result=result)
            result = {**result, "queued": True, "pending_event_id": str(event.id) if event else ""}
        else:
            completed_events = mark_google_sheets_export_synced(db, action, entity_id, result=result)
            if completed_events:
                result = {**result, "completed_pending_events": completed_events}
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
