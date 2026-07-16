import uuid
from datetime import date, datetime, timezone

from sqlalchemy import BigInteger, String, and_, case, cast, func, or_, select
from sqlalchemy.orm import Session, aliased

from .models import AuditLog, Order, OrderItem, ScanCode
from .pagination import CursorError, decode_cursor, encode_cursor
from .orders_service import (
    COMPLETED_STATUSES,
    INACTIVE_ORDER_STATUSES,
    STATUS_COMPLETED,
    STATUS_ARCHIVED_NO_KIZ,
    STATUS_CANCELLED,
    STATUS_REMOVED_FROM_GOOGLE,
    STATUS_RETURNED,
    parse_int,
)
from .schemas import (
    AdminActivityRead,
    AdminOrderCapabilityRead,
    AdminTableRead,
    AdminTableRow,
    AdminTableTotals,
)
from .redaction import redact_secrets


ADMIN_TABLE_CURSOR_SCOPE = "admin.table.v2"
ADMIN_ACTION_KEYS = (
    "resync",
    "archive",
    "completeWithoutKiz",
    "cancel",
    "deleteActive",
    "resetRescan",
    "restore",
    "resyncSkladBot",
)


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
    cursor="",
):
    row_limit = max(1, int(limit or 500))
    row_offset = max(0, int(offset or 0))
    activity_row_limit = max(0, min(int(30 if activity_limit is None else activity_limit), 100))

    cursor_filters = {
        "status_bucket": status_bucket,
        "shipment_date": shipment_date,
        "search": search,
        "scan_state": scan_state,
        "skladbot_filter": skladbot_filter,
    }
    snapshot = None
    after = None
    stable_page = row_offset == 0
    if cursor:
        if row_offset:
            raise CursorError("invalid_cursor")
        snapshot, after, row_offset = decode_admin_table_cursor(cursor, cursor_filters)
        stable_page = True

    expressions = admin_sql_expressions(db)
    predicates = admin_sql_filter_predicates(
        expressions,
        status_bucket=status_bucket,
        shipment_date=shipment_date,
        search=search,
        scan_state=scan_state,
        skladbot_filter=skladbot_filter,
    )
    if snapshot is not None:
        predicates = (
            *predicates,
            Order.created_at <= snapshot[0],
            OrderItem.created_at <= snapshot[1],
        )
    totals, total_rows = query_admin_totals(db, expressions, predicates)
    raw_rows = query_admin_page(
        db,
        expressions,
        predicates,
        limit=row_limit + 1,
        offset=0 if stable_page else row_offset,
        after=after,
    )
    has_more = len(raw_rows) > row_limit
    visible_rows = raw_rows[:row_limit]
    rows = [admin_sql_row_to_read(row) for row in visible_rows]
    order_capabilities = admin_order_capabilities_from_rows(visible_rows)
    next_cursor = ""
    if has_more and stable_page and visible_rows:
        snapshot = snapshot or (
            visible_rows[-1]["_snapshot_order_created_at"],
            visible_rows[-1]["_snapshot_item_created_at"],
        )
        next_cursor = encode_admin_table_cursor(
            snapshot,
            visible_rows[-1],
            row_offset + len(visible_rows),
            cursor_filters,
        )

    return AdminTableRead(
        generated_at=datetime.now(timezone.utc),
        totals=totals,
        rows=rows,
        recent_activity=list_recent_activity(db, activity_row_limit),
        limit=row_limit,
        offset=row_offset,
        row_count=len(rows),
        total_rows=total_rows,
        has_more=has_more,
        next_cursor=next_cursor,
        order_capabilities=order_capabilities,
    )


def decode_admin_table_cursor(cursor, filters):
    try:
        keys = decode_cursor(cursor, ADMIN_TABLE_CURSOR_SCOPE, filters=filters)
        if len(keys) != 8:
            raise ValueError
        snapshot = (parse_cursor_datetime(keys[0]), parse_cursor_datetime(keys[1]))
        order_date = date.fromisoformat(keys[2]) if keys[2] else None
        after = (
            order_date,
            parse_cursor_datetime(keys[3]),
            uuid.UUID(str(keys[4])),
            parse_cursor_datetime(keys[5]),
            uuid.UUID(str(keys[6])),
        )
        logical_offset = int(keys[7])
        if logical_offset < 0:
            raise ValueError
        return snapshot, after, logical_offset
    except (CursorError, TypeError, ValueError):
        raise CursorError("invalid_cursor") from None


def parse_cursor_datetime(value):
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("cursor datetime must include timezone")
    return parsed


def encode_admin_table_cursor(snapshot, row, logical_offset, filters):
    order_date = row["order_date"]
    return encode_cursor(
        ADMIN_TABLE_CURSOR_SCOPE,
        [
            snapshot[0].isoformat(),
            snapshot[1].isoformat(),
            order_date.isoformat() if order_date else None,
            row["created_at"].isoformat(),
            str(row["order_id"]),
            row["_cursor_item_created_at"].isoformat(),
            str(row["item_id"]),
            int(logical_offset),
        ],
        filters=filters,
    )


def admin_table_after_predicate(after):
    cursor_date, order_created_at, order_id, item_created_at, item_id = after
    item_tail = or_(
        OrderItem.created_at > item_created_at,
        and_(OrderItem.created_at == item_created_at, OrderItem.id > item_id),
    )
    order_tail = or_(
        Order.created_at > order_created_at,
        and_(
            Order.created_at == order_created_at,
            or_(Order.id > order_id, and_(Order.id == order_id, item_tail)),
        ),
    )
    if cursor_date is None:
        return and_(Order.order_date.is_(None), order_tail)
    return or_(
        Order.order_date > cursor_date,
        Order.order_date.is_(None),
        and_(Order.order_date == cursor_date, order_tail),
    )


def admin_order_capabilities_from_rows(rows):
    result = {}
    for row in rows:
        order_id = str(row["order_id"])
        if order_id in result:
            continue
        status = normalize_text(row["order_status"])
        planned_blocks = int(row["_order_planned_blocks"] or 0)
        scanned_blocks = int(row["_order_scanned_blocks"] or 0)
        scan_codes_count = int(row["_order_scan_codes_count"] or 0)
        active = status not in INACTIVE_ORDER_STATUSES
        no_scans = scanned_blocks == 0 and scan_codes_count == 0
        allowed = {
            "resync": True,
            "archive": active and no_scans,
            "completeWithoutKiz": active,
            "cancel": active and no_scans,
            "deleteActive": active and no_scans,
            "resetRescan": status != STATUS_RETURNED,
            "restore": status in (STATUS_ARCHIVED_NO_KIZ, STATUS_CANCELLED),
            "resyncSkladBot": True,
        }
        reasons = {key: "" for key in ADMIN_ACTION_KEYS}
        if not active:
            reasons["archive"] = "Доступно только для активного заказа"
            reasons["completeWithoutKiz"] = "Доступно только для активных заказов"
            reasons["cancel"] = "Доступно только для активного заказа"
            reasons["deleteActive"] = "Доступно только для активного заказа"
        if active and not no_scans:
            reasons["archive"] = "В заказе уже есть отсканированные КИЗы"
            reasons["cancel"] = "В заказе уже есть отсканированные КИЗы"
            reasons["deleteActive"] = "В заказе уже есть отсканированные КИЗы"
        if status == STATUS_RETURNED:
            reasons["resetRescan"] = "Возвраты нельзя сбрасывать на пересканирование"
        if not allowed["restore"]:
            reasons["restore"] = "Доступно только для отмененных заказов или архива без КИЗов"
        result[order_id] = AdminOrderCapabilityRead(
            order_id=order_id,
            items_count=int(row["_order_items_count"] or 0),
            planned_blocks=planned_blocks,
            scanned_blocks=scanned_blocks,
            scan_codes_count=scan_codes_count,
            allowed=allowed,
            disabled_reasons=reasons,
        )
    return result


def admin_sql_expressions(db: Session):
    aggregate_item = aliased(OrderItem)
    aggregate_scan = aliased(ScanCode)
    items_count = (
        select(func.count(aggregate_item.id))
        .where(aggregate_item.order_id == Order.id)
        .correlate(Order)
        .scalar_subquery()
    )
    planned_blocks = (
        select(func.coalesce(func.sum(aggregate_item.quantity_blocks), 0))
        .where(aggregate_item.order_id == Order.id)
        .correlate(Order)
        .scalar_subquery()
    )
    scanned_blocks = (
        select(func.coalesce(func.sum(aggregate_item.scanned_blocks), 0))
        .where(aggregate_item.order_id == Order.id)
        .correlate(Order)
        .scalar_subquery()
    )
    scan_codes_count = (
        select(func.count(aggregate_scan.id))
        .select_from(aggregate_scan)
        .join(aggregate_item, aggregate_item.id == aggregate_scan.order_item_id)
        .where(aggregate_item.order_id == Order.id)
        .correlate(Order)
        .scalar_subquery()
    )
    status_value = case(
        (Order.status == STATUS_RETURNED, "returned"),
        (OrderItem.status == STATUS_REMOVED_FROM_GOOGLE, "cancelled"),
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
    remaining = case(
        (OrderItem.quantity_blocks > OrderItem.scanned_blocks,
         OrderItem.quantity_blocks - OrderItem.scanned_blocks),
        else_=0,
    )
    return {
        "status_bucket": status_value,
        "scan_state": scan_value,
        "request_number": request_number,
        "request_id": request_id,
        "skladbot_status": skladbot_status,
        "remaining_blocks": remaining,
        "block_price": safe_json_int(db, OrderItem.raw_payload, "block_price"),
        "line_total": safe_json_int(db, OrderItem.raw_payload, "line_total"),
        "order_items_count": items_count,
        "order_planned_blocks": planned_blocks,
        "order_scanned_blocks": scanned_blocks,
        "order_scan_codes_count": scan_codes_count,
    }


def admin_sql_filter_predicates(
    expressions,
    *,
    status_bucket="",
    shipment_date="",
    search="",
    scan_state="",
    skladbot_filter="",
):
    status_bucket = normalize_filter_value(status_bucket)
    shipment_date = normalize_filter_value(shipment_date)
    search = normalize_text(search).casefold()
    scan_state = normalize_filter_value(scan_state)
    skladbot_filter = normalize_filter_value(skladbot_filter)
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
    )
    return totals, totals.items


def query_admin_page(db: Session, expressions, predicates, *, limit: int, offset: int, after=None):
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
            json_text(Order.raw_payload, "return_status").label("return_status"),
            json_text(Order.raw_payload, "returned_at").label("returned_at"),
            json_text(Order.raw_payload, "return_reference").label("return_reference"),
            Order.created_at,
            Order.updated_at,
            OrderItem.created_at.label("_cursor_item_created_at"),
            expressions["order_items_count"].label("_order_items_count"),
            expressions["order_planned_blocks"].label("_order_planned_blocks"),
            expressions["order_scanned_blocks"].label("_order_scanned_blocks"),
            expressions["order_scan_codes_count"].label("_order_scan_codes_count"),
            func.max(Order.created_at).over().label("_snapshot_order_created_at"),
            func.max(OrderItem.created_at).over().label("_snapshot_item_created_at"),
        )
        .select_from(Order)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(*predicates)
    )
    if after is not None:
        stmt = stmt.where(admin_table_after_predicate(after))
    stmt = (
        stmt.order_by(
            Order.order_date.asc().nulls_last(),
            Order.created_at.asc(),
            Order.id.asc(),
            OrderItem.created_at.asc(),
            OrderItem.id.asc(),
        )
        .offset(offset)
        .limit(limit)
    )
    return list(db.execute(stmt).mappings().all())


def admin_sql_row_to_read(row):
    values = dict(row)
    for field in (
        "_cursor_item_created_at",
        "_order_items_count",
        "_order_planned_blocks",
        "_order_scanned_blocks",
        "_order_scan_codes_count",
        "_snapshot_order_created_at",
        "_snapshot_item_created_at",
    ):
        values.pop(field, None)
    values["order_id"] = str(values["order_id"])
    values["item_id"] = str(values["item_id"])
    values["coordinates"] = normalize_text(values["coordinates"])
    values["representative"] = values["representative"] or None
    for field in (
        "skladbot_request_number", "skladbot_request_id", "skladbot_status",
        "skladbot_return_request_number", "skladbot_return_request_id", "skladbot_return_status",
        "source_file", "return_status", "returned_at", "return_reference",
    ):
        values[field] = normalize_text(values[field])
    for field in (
        "quantity_pieces", "quantity_blocks", "scanned_blocks", "remaining_blocks",
        "scan_codes_count", "block_price", "line_total",
    ):
        values[field] = int(values[field] or 0)
    return AdminTableRow(**values)


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
):
    status_bucket = normalize_filter_value(status_bucket)
    shipment_date = normalize_filter_value(shipment_date)
    search = normalize_text(search).casefold()
    scan_state = normalize_filter_value(scan_state)
    skladbot_filter = normalize_filter_value(skladbot_filter)

    return [
        row for row in rows
        if admin_row_matches_filters(
            row,
            status_bucket=status_bucket,
            shipment_date=shipment_date,
            search=search,
            scan_state=scan_state,
            skladbot_filter=skladbot_filter,
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
):
    if status_bucket and row.status_bucket != status_bucket:
        return False
    if shipment_date and (row.order_date.isoformat() if row.order_date else "") != shipment_date:
        return False
    if scan_state and admin_row_scan_state(row) != scan_state:
        return False
    if skladbot_filter and not admin_row_matches_skladbot_filter(row, skladbot_filter):
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


def order_item_to_admin_row(order: Order, item: OrderItem, _pending_by_entity=None):
    order_raw = order.raw_payload or {}
    item_raw = item.raw_payload or {}
    blocks = int(item.quantity_blocks or 0)
    scanned = int(item.scanned_blocks or 0)

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
        return "cancelled"
    if order.status == STATUS_ARCHIVED_NO_KIZ:
        return "archive_no_kiz"
    if order.status == STATUS_CANCELLED:
        return "cancelled"
    if order.status in (STATUS_COMPLETED, "done", "closed"):
        return "archive"
    if order.status not in COMPLETED_STATUSES:
        return "active"
    return order.status or "other"


def build_totals(rows, _pending_events=None):
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
