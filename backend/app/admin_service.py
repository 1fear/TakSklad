from datetime import datetime, timezone

from sqlalchemy import BigInteger, String, and_, case, cast, exists, func, literal, or_, select
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.orm import Session, aliased

from .models import AuditLog, Order, OrderItem, PendingEvent, ScanCode
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
    row_limit = max(1, int(limit or 500))
    row_offset = max(0, int(offset or 0))
    activity_row_limit = max(0, min(int(30 if activity_limit is None else activity_limit), 100))

    expressions = admin_sql_expressions(db)
    predicates = admin_sql_filter_predicates(
        expressions,
        status_bucket=status_bucket,
        shipment_date=shipment_date,
        search=search,
        scan_state=scan_state,
        skladbot_filter=skladbot_filter,
        google_status=google_status,
    )
    totals, total_rows = query_admin_totals(db, expressions, predicates)
    pending_total = query_admin_pending_total(db, predicates)
    totals.pending_google_exports = pending_total
    rows = query_admin_page(db, expressions, predicates, limit=row_limit, offset=row_offset)

    return AdminTableRead(
        generated_at=datetime.now(timezone.utc),
        totals=totals,
        rows=rows,
        recent_activity=list_recent_activity(db, activity_row_limit),
        limit=row_limit,
        offset=row_offset,
        row_count=len(rows),
        total_rows=total_rows,
        has_more=row_offset + len(rows) < total_rows,
    )


def admin_sql_expressions(db: Session):
    pending_count = pending_google_count_for_row(db, Order.id, OrderItem.id)
    status_value = case(
        (Order.status == STATUS_RETURNED, "returned"),
        (OrderItem.status == STATUS_REMOVED_FROM_GOOGLE, "removed_from_google"),
        (Order.status == STATUS_ARCHIVED_NO_KIZ, "archive_no_kiz"),
        (Order.status == STATUS_CANCELLED, "cancelled"),
        (Order.status.in_((STATUS_COMPLETED, "done", "closed")), "archive"),
        (~Order.status.in_(COMPLETED_STATUSES), "active"),
        else_=func.coalesce(Order.status, "other"),
    )
    scan_value = case(
        (OrderItem.quantity_blocks <= 0, "no_plan"),
        (OrderItem.scanned_blocks > OrderItem.quantity_blocks, "over_scanned"),
        (OrderItem.scanned_blocks >= OrderItem.quantity_blocks, "completed"),
        (OrderItem.scanned_blocks > 0, "in_progress"),
        else_="not_started",
    )
    request_number = json_text(Order.raw_payload, "skladbot_request_number")
    request_id = json_text(Order.raw_payload, "skladbot_request_id")
    skladbot_status = json_text(Order.raw_payload, "skladbot_status")
    synced_at = json_text(OrderItem.raw_payload, "google_sheet_synced_at")
    google_value = case(
        (pending_count > 0, "pending"),
        (OrderItem.status == STATUS_REMOVED_FROM_GOOGLE, "removed_from_google"),
        (func.trim(synced_at) != "", "synced"),
        else_="unknown",
    )
    remaining = case(
        (OrderItem.quantity_blocks > OrderItem.scanned_blocks,
         OrderItem.quantity_blocks - OrderItem.scanned_blocks),
        else_=0,
    )
    return {
        "pending_count": pending_count,
        "status_bucket": status_value,
        "scan_state": scan_value,
        "request_number": request_number,
        "request_id": request_id,
        "skladbot_status": skladbot_status,
        "google_status": google_value,
        "remaining_blocks": remaining,
        "block_price": safe_json_int(db, OrderItem.raw_payload, "block_price"),
        "line_total": safe_json_int(db, OrderItem.raw_payload, "line_total"),
    }


def admin_sql_filter_predicates(
    expressions,
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
    predicates = []
    if status_bucket:
        predicates.append(expressions["status_bucket"] == status_bucket)
    if shipment_date:
        predicates.append(func.coalesce(cast(Order.order_date, String), "") == shipment_date)
    if scan_state:
        predicates.append(expressions["scan_state"] == scan_state)
    request_number = func.trim(expressions["request_number"])
    request_id = func.trim(expressions["request_id"])
    has_request = or_(request_number != "", request_id != "")
    if skladbot_filter == "found":
        predicates.append(has_request)
    elif skladbot_filter == "missing":
        predicates.append(~has_request)
    elif skladbot_filter == "problem":
        predicates.append(expressions["skladbot_status"].in_(("not_found", "multiple", "error", "pending")))
    if google_status:
        predicates.append(expressions["google_status"] == google_status)
    if search:
        search_values = (
            Order.client,
            Order.address,
            Order.representative,
            Order.payment_type,
            OrderItem.product,
            json_text(OrderItem.raw_payload, "source_file"),
            expressions["request_number"],
            expressions["request_id"],
        )
        predicates.append(or_(*[
            func.lower(func.coalesce(cast(value, String), "")).contains(search, autoescape=True)
            for value in search_values
        ]))
    return tuple(predicates)


def query_admin_totals(db: Session, expressions, predicates):
    rows = (
        select(
            Order.id.label("order_id"),
            OrderItem.id.label("item_id"),
            expressions["status_bucket"].label("status_bucket"),
            OrderItem.quantity_blocks.label("quantity_blocks"),
            OrderItem.scanned_blocks.label("scanned_blocks"),
            expressions["remaining_blocks"].label("remaining_blocks"),
            expressions["line_total"].label("line_total"),
        )
        .select_from(Order)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(*predicates)
        .subquery()
    )
    values = db.execute(select(
        func.count(func.distinct(rows.c.order_id)),
        func.count(rows.c.item_id),
        func.count(func.distinct(rows.c.order_id)).filter(rows.c.status_bucket == "active"),
        func.count(func.distinct(rows.c.order_id)).filter(rows.c.status_bucket == "archive"),
        func.count(func.distinct(rows.c.order_id)).filter(rows.c.status_bucket == "returned"),
        func.coalesce(func.sum(rows.c.quantity_blocks), 0),
        func.coalesce(func.sum(rows.c.scanned_blocks), 0),
        func.coalesce(func.sum(
            case((rows.c.status_bucket == "active", rows.c.remaining_blocks), else_=0)
        ), 0),
        func.coalesce(func.sum(rows.c.line_total), 0),
    )).one()
    totals = AdminTableTotals(
        orders=int(values[0] or 0),
        items=int(values[1] or 0),
        active_orders=int(values[2] or 0),
        archived_orders=int(values[3] or 0),
        returned_orders=int(values[4] or 0),
        planned_blocks=int(values[5] or 0),
        scanned_blocks=int(values[6] or 0),
        remaining_blocks=int(values[7] or 0),
        total_price=int(values[8] or 0),
        pending_google_exports=0,
    )
    return totals, totals.items


def query_admin_pending_total(db: Session, predicates):
    matching_row = exists(
        select(literal(1))
        .select_from(Order)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(*predicates)
        .where(pending_event_link_predicate(db, PendingEvent, Order.id, OrderItem.id))
    ).correlate(PendingEvent)
    total = db.execute(
        select(func.count(PendingEvent.id))
        .where(*pending_event_base_predicates(PendingEvent))
        .where(matching_row)
    ).scalar_one()
    return int(total or 0)


def query_admin_page(db: Session, expressions, predicates, *, limit: int, offset: int):
    scan_count = (
        select(func.count(ScanCode.id))
        .where(ScanCode.order_item_id == OrderItem.id)
        .correlate(OrderItem)
        .scalar_subquery()
    )
    stmt = (
        select(
            Order.id.label("order_id"),
            OrderItem.id.label("item_id"),
            Order.order_date,
            Order.payment_type,
            Order.client,
            Order.address,
            Order.representative,
            Order.status.label("order_status"),
            OrderItem.status.label("item_status"),
            expressions["status_bucket"].label("status_bucket"),
            OrderItem.product,
            OrderItem.quantity_pieces,
            OrderItem.quantity_blocks,
            OrderItem.scanned_blocks,
            expressions["remaining_blocks"].label("remaining_blocks"),
            scan_count.label("scan_codes_count"),
            expressions["block_price"].label("block_price"),
            expressions["line_total"].label("line_total"),
            json_text(Order.raw_payload, "coordinates").label("coordinates"),
            expressions["request_number"].label("skladbot_request_number"),
            expressions["request_id"].label("skladbot_request_id"),
            expressions["skladbot_status"].label("skladbot_status"),
            json_text(Order.raw_payload, "skladbot_return_request_number").label("skladbot_return_request_number"),
            json_text(Order.raw_payload, "skladbot_return_request_id").label("skladbot_return_request_id"),
            json_text(Order.raw_payload, "skladbot_return_request_status").label("skladbot_return_status"),
            json_text(OrderItem.raw_payload, "source_file").label("source_file"),
            expressions["google_status"].label("google_sheet_status"),
            json_text(OrderItem.raw_payload, "google_sheet_row_number").label("google_sheet_row_number"),
            json_text(OrderItem.raw_payload, "google_sheet_synced_at").label("google_sheet_synced_at"),
            expressions["pending_count"].label("pending_google_exports"),
            json_text(Order.raw_payload, "return_status").label("return_status"),
            json_text(Order.raw_payload, "returned_at").label("returned_at"),
            json_text(Order.raw_payload, "return_reference").label("return_reference"),
            Order.created_at,
            Order.updated_at,
        )
        .select_from(Order)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(*predicates)
        .order_by(
            Order.order_date.asc().nulls_last(),
            Order.created_at.asc(),
            Order.id.asc(),
            OrderItem.created_at.asc(),
            OrderItem.id.asc(),
        )
        .offset(offset)
        .limit(limit)
    )
    return [admin_sql_row_to_read(row) for row in db.execute(stmt).mappings().all()]


def admin_sql_row_to_read(row):
    values = dict(row)
    values["order_id"] = str(values["order_id"])
    values["item_id"] = str(values["item_id"])
    values["coordinates"] = normalize_text(values["coordinates"])
    values["representative"] = values["representative"] or None
    for field in (
        "skladbot_request_number", "skladbot_request_id", "skladbot_status",
        "skladbot_return_request_number", "skladbot_return_request_id", "skladbot_return_status",
        "source_file", "google_sheet_synced_at", "return_status", "returned_at", "return_reference",
    ):
        values[field] = normalize_text(values[field])
    values["google_sheet_row_number"] = parse_optional_int(values["google_sheet_row_number"])
    for field in (
        "quantity_pieces", "quantity_blocks", "scanned_blocks", "remaining_blocks",
        "scan_codes_count", "block_price", "line_total", "pending_google_exports",
    ):
        values[field] = int(values[field] or 0)
    return AdminTableRow(**values)


def pending_google_count_for_row(db: Session, order_id, item_id):
    event = aliased(PendingEvent)
    return (
        select(func.count(event.id))
        .where(*pending_event_base_predicates(event))
        .where(pending_event_link_predicate(db, event, order_id, item_id))
        .correlate(Order, OrderItem)
        .scalar_subquery()
    )


def pending_event_base_predicates(event):
    return (
        event.event_type == GOOGLE_EXPORT_EVENT_TYPE,
        event.status.in_(PENDING_STATUSES),
        func.coalesce(json_text(event.payload, "action"), "") != "google_sheets_skladbot_export",
    )


def pending_event_link_predicate(db: Session, event, order_id, item_id):
    entity_id = func.trim(json_text(event.payload, "entity_id"))
    order_text = cast(order_id, String)
    item_text = cast(item_id, String)
    if db.get_bind().dialect.name == "postgresql":
        direct_match = or_(entity_id == order_text, entity_id == item_text)
        order_ids_contains = func.jsonb_path_exists(
            event.payload,
            cast("$.order_ids[*] ? (@ == $order_id)", JSONPATH),
            func.jsonb_build_object("order_id", order_text),
        )
    else:
        normalized_entity_id = func.replace(entity_id, "-", "")
        direct_match = or_(normalized_entity_id == order_text, normalized_entity_id == item_text)
        order_ids = func.json_each(event.payload, "$.order_ids").table_valued("key", "value").alias()
        order_ids_contains = exists(
            select(literal(1))
            .select_from(order_ids)
            .where(func.replace(cast(order_ids.c.value, String), "-", "") == order_text)
        ).correlate(event, Order)
    return or_(direct_match, order_ids_contains)


def json_text(column, key):
    return func.coalesce(column[key].as_string(), "")


def safe_json_int(db: Session, column, key):
    value = func.trim(json_text(column, key))
    if db.get_bind().dialect.name == "postgresql":
        valid = value.op("~")(r"^[+-]?[0-9]+$")
    else:
        unsigned = and_(value.op("GLOB")("[0-9]*"), ~value.op("GLOB")("*[^0-9]*"))
        negative = and_(value.op("GLOB")("-[0-9]*"), ~func.substr(value, 2).op("GLOB")("*[^0-9]*"))
        positive = and_(value.op("GLOB")("+[0-9]*"), ~func.substr(value, 2).op("GLOB")("*[^0-9]*"))
        valid = or_(unsigned, negative, positive)
    return case((valid, cast(value, BigInteger)), else_=0)


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
            actor_subject=row.actor_subject or "",
            actor_user_id=str(row.actor_user_id or ""),
            actor_service_principal_id=str(row.actor_service_principal_id or ""),
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
