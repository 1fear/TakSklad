from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import AuditLog, Order, OrderItem, PendingEvent
from .orders_service import (
    COMPLETED_STATUSES,
    STATUS_COMPLETED,
    STATUS_ARCHIVED_NO_KIZ,
    STATUS_CANCELLED,
    STATUS_REMOVED_FROM_GOOGLE,
    STATUS_RETURNED,
    parse_int,
)
from .schemas import AdminActivityRead, AdminTableRead, AdminTableRow, AdminTableTotals


GOOGLE_EXPORT_EVENT_TYPE = "google_sheets_export"
PENDING_STATUSES = ("pending", "failed")


def build_admin_table(db: Session, limit=1000, activity_limit=30):
    row_limit = max(1, min(int(limit or 1000), 5000))
    activity_row_limit = max(0, min(int(activity_limit or 30), 100))
    pending_by_entity, pending_total = pending_google_exports_by_entity(db)

    orders = db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .order_by(Order.order_date.asc(), Order.created_at.asc())
    ).scalars().all()

    rows = []
    for order in orders:
        for item in sorted(order.items, key=lambda value: (str(value.created_at or ""), str(value.id))):
            rows.append(order_item_to_admin_row(order, item, pending_by_entity))
            if len(rows) >= row_limit:
                break
        if len(rows) >= row_limit:
            break

    return AdminTableRead(
        generated_at=datetime.now(timezone.utc),
        totals=build_totals(rows, pending_total),
        rows=rows,
        recent_activity=list_recent_activity(db, activity_row_limit),
    )


def pending_google_exports_by_entity(db: Session):
    pending = db.execute(
        select(PendingEvent).where(
            PendingEvent.event_type == GOOGLE_EXPORT_EVENT_TYPE,
            PendingEvent.status.in_(PENDING_STATUSES),
        )
    ).scalars().all()
    by_entity = {}
    for event in pending:
        payload = event.payload or {}
        entity_id = normalize_text(payload.get("entity_id"))
        if not entity_id:
            continue
        by_entity[entity_id] = by_entity.get(entity_id, 0) + 1
    return by_entity, len(pending)


def order_item_to_admin_row(order: Order, item: OrderItem, pending_by_entity):
    order_raw = order.raw_payload or {}
    item_raw = item.raw_payload or {}
    blocks = int(item.quantity_blocks or 0)
    scanned = int(item.scanned_blocks or 0)
    pending_count = pending_by_entity.get(str(order.id), 0) + pending_by_entity.get(str(item.id), 0)

    return AdminTableRow(
        order_id=str(order.id),
        item_id=str(item.id),
        order_date=order.order_date,
        payment_type=order.payment_type,
        client=order.client,
        address=order.address,
        coordinates=normalize_text(order_raw.get("coordinates")),
        representative=order.representative,
        order_status=order.status,
        item_status=item.status,
        status_bucket=status_bucket(order, item),
        product=item.product,
        quantity_pieces=int(item.quantity_pieces or 0),
        quantity_blocks=blocks,
        scanned_blocks=scanned,
        remaining_blocks=max(0, blocks - scanned),
        scan_codes_count=len(item.scan_codes or []),
        block_price=parse_int(item_raw.get("block_price")),
        line_total=parse_int(item_raw.get("line_total")),
        skladbot_request_number=normalize_text(order_raw.get("skladbot_request_number")),
        skladbot_request_id=normalize_text(order_raw.get("skladbot_request_id")),
        skladbot_status=normalize_text(order_raw.get("skladbot_status")),
        source_file=normalize_text(item_raw.get("source_file")),
        google_sheet_status=google_sheet_status(item, pending_count),
        google_sheet_row_number=parse_optional_int(item_raw.get("google_sheet_row_number")),
        google_sheet_synced_at=normalize_text(item_raw.get("google_sheet_synced_at")),
        pending_google_exports=pending_count,
        return_status=normalize_text(order_raw.get("return_status")),
        returned_at=normalize_text(order_raw.get("returned_at")),
        return_reference=normalize_text(order_raw.get("return_reference")),
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


def status_bucket(order: Order, item: OrderItem):
    if order.status == STATUS_RETURNED:
        return "returned"
    if item.status == STATUS_REMOVED_FROM_GOOGLE:
        return "removed_from_google"
    if order.status == STATUS_ARCHIVED_NO_KIZ:
        return "archive_no_kiz"
    if order.status == STATUS_CANCELLED:
        return "cancelled"
    if order.status in (STATUS_COMPLETED, "done", "closed"):
        return "archive"
    if order.status not in COMPLETED_STATUSES:
        return "active"
    return order.status or "other"


def google_sheet_status(item: OrderItem, pending_count=0):
    if pending_count:
        return "pending"
    raw_payload = item.raw_payload or {}
    if item.status == STATUS_REMOVED_FROM_GOOGLE:
        return "removed_from_google"
    if normalize_text(raw_payload.get("google_sheet_synced_at")):
        return "synced"
    return "unknown"


def build_totals(rows, pending_total):
    order_ids = {row.order_id for row in rows}
    active_order_ids = {row.order_id for row in rows if row.status_bucket == "active"}
    archived_order_ids = {row.order_id for row in rows if row.status_bucket == "archive"}
    returned_order_ids = {row.order_id for row in rows if row.status_bucket == "returned"}
    return AdminTableTotals(
        orders=len(order_ids),
        items=len(rows),
        active_orders=len(active_order_ids),
        archived_orders=len(archived_order_ids),
        returned_orders=len(returned_order_ids),
        planned_blocks=sum(row.quantity_blocks for row in rows),
        scanned_blocks=sum(row.scanned_blocks for row in rows),
        remaining_blocks=sum(row.remaining_blocks for row in rows if row.status_bucket == "active"),
        total_price=sum(row.line_total for row in rows),
        pending_google_exports=pending_total,
    )


def list_recent_activity(db: Session, limit):
    if limit <= 0:
        return []
    rows = db.execute(
        select(AuditLog)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
    ).scalars().all()
    return [
        AdminActivityRead(
            id=str(row.id),
            action=row.action,
            entity_type=row.entity_type or "",
            entity_id=row.entity_id or "",
            payload={},
            created_at=row.created_at,
        )
        for row in rows
    ]


def parse_optional_int(value):
    text = normalize_text(value)
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def normalize_text(value):
    return str(value or "").strip()
