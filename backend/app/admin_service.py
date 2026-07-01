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
from .redaction import redact_secrets


GOOGLE_EXPORT_EVENT_TYPE = "google_sheets_export"
PENDING_STATUSES = ("pending", "failed")


def build_admin_table(
    db: Session,
    limit=None,
    offset=0,
    activity_limit=30,
    status_bucket="",
    shipment_date="",
    search="",
    scan_state="",
    skladbot_filter="",
    google_status="",
):
    row_limit = None if limit is None else max(1, int(limit))
    row_offset = max(0, int(offset or 0))
    activity_row_limit = max(0, min(int(activity_limit or 30), 100))
    pending_by_entity, pending_events = pending_google_exports_by_entity(db)

    orders = db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .order_by(Order.order_date.asc(), Order.created_at.asc())
    ).scalars().all()

    all_rows = []
    for order in orders:
        for item in sorted(order.items, key=lambda value: (str(value.created_at or ""), str(value.id))):
            all_rows.append(order_item_to_admin_row(order, item, pending_by_entity))
    filtered_rows = filter_admin_rows(
        all_rows,
        status_bucket=status_bucket,
        shipment_date=shipment_date,
        search=search,
        scan_state=scan_state,
        skladbot_filter=skladbot_filter,
        google_status=google_status,
    )
    total_rows = len(filtered_rows)
    rows = filtered_rows[row_offset:] if row_limit is None else filtered_rows[row_offset:row_offset + row_limit]

    return AdminTableRead(
        generated_at=datetime.now(timezone.utc),
        totals=build_totals(filtered_rows, pending_events),
        rows=rows,
        recent_activity=list_recent_activity(db, activity_row_limit),
        limit=row_limit if row_limit is not None else len(rows),
        offset=row_offset,
        row_count=len(rows),
        total_rows=total_rows,
        has_more=row_offset + len(rows) < total_rows,
    )


def filter_admin_rows(
    rows,
    *,
    status_bucket="",
    shipment_date="",
    search="",
    scan_state="",
    skladbot_filter="",
    google_status="",
):
    status_bucket = normalize_filter_value(status_bucket)
    shipment_date = normalize_filter_value(shipment_date)
    search = normalize_text(search).casefold()
    scan_state = normalize_filter_value(scan_state)
    skladbot_filter = normalize_filter_value(skladbot_filter)
    google_status = normalize_filter_value(google_status)

    return [
        row for row in rows
        if admin_row_matches_filters(
            row,
            status_bucket=status_bucket,
            shipment_date=shipment_date,
            search=search,
            scan_state=scan_state,
            skladbot_filter=skladbot_filter,
            google_status=google_status,
        )
    ]


def admin_row_matches_filters(
    row,
    *,
    status_bucket="",
    shipment_date="",
    search="",
    scan_state="",
    skladbot_filter="",
    google_status="",
):
    if status_bucket and row.status_bucket != status_bucket:
        return False
    if shipment_date and (row.order_date.isoformat() if row.order_date else "") != shipment_date:
        return False
    if scan_state and admin_row_scan_state(row) != scan_state:
        return False
    if skladbot_filter and not admin_row_matches_skladbot_filter(row, skladbot_filter):
        return False
    if google_status and row.google_sheet_status != google_status:
        return False
    if search and not admin_row_matches_search(row, search):
        return False
    return True


def admin_row_matches_search(row, search):
    values = (
        row.client,
        row.address,
        row.representative or "",
        row.payment_type,
        row.product,
        row.source_file,
        row.skladbot_request_number,
        row.skladbot_request_id,
    )
    return any(search in normalize_text(value).casefold() for value in values)


def admin_row_matches_skladbot_filter(row, value):
    has_number = bool(row.skladbot_request_number or row.skladbot_request_id)
    if value == "found":
        return has_number
    if value == "missing":
        return not has_number
    if value == "problem":
        return row.skladbot_status in {"not_found", "multiple", "error", "pending"}
    return True


def admin_row_scan_state(row):
    if row.quantity_blocks <= 0:
        return "no_plan"
    if row.scanned_blocks > row.quantity_blocks:
        return "over_scanned"
    if row.scanned_blocks >= row.quantity_blocks:
        return "completed"
    if row.scanned_blocks > 0:
        return "in_progress"
    return "not_started"


def normalize_filter_value(value):
    text = normalize_text(value)
    return "" if text == "all" else text


def pending_google_exports_by_entity(db: Session):
    pending = db.execute(
        select(PendingEvent).where(
            PendingEvent.event_type == GOOGLE_EXPORT_EVENT_TYPE,
            PendingEvent.status.in_(PENDING_STATUSES),
        )
    ).scalars().all()
    by_entity = {}
    visible_events = []
    for event in pending:
        payload = event.payload or {}
        if payload.get("action") == "google_sheets_skladbot_export":
            continue
        visible_events.append(event)
        entity_id = normalize_text(payload.get("entity_id"))
        if entity_id:
            by_entity[entity_id] = by_entity.get(entity_id, 0) + 1
        for order_id in payload.get("order_ids") or []:
            order_id = normalize_text(order_id)
            if order_id:
                by_entity[order_id] = by_entity.get(order_id, 0) + 1
    return by_entity, visible_events


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
        skladbot_return_request_number=normalize_text(order_raw.get("skladbot_return_request_number")),
        skladbot_return_request_id=normalize_text(order_raw.get("skladbot_return_request_id")),
        skladbot_return_status=normalize_text(order_raw.get("skladbot_return_request_status")),
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


def build_totals(rows, pending_events):
    order_ids = {row.order_id for row in rows}
    item_ids = {row.item_id for row in rows}
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
        pending_google_exports=count_pending_google_exports_for_rows(pending_events, order_ids, item_ids),
    )


def count_pending_google_exports_for_rows(pending_events, order_ids, item_ids):
    total = 0
    for event in pending_events:
        payload = event.payload or {}
        entity_id = normalize_text(payload.get("entity_id"))
        event_order_ids = {normalize_text(order_id) for order_id in payload.get("order_ids") or []}
        if entity_id in order_ids or entity_id in item_ids or event_order_ids.intersection(order_ids):
            total += 1
    return total


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
            payload=sanitize_payload(row.payload),
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


def sanitize_payload(value):
    if isinstance(value, dict):
        return {str(key): sanitize_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value
