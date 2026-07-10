from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Date, Integer, String, TIMESTAMP, and_, case, cast, func, literal, or_, select, union
from sqlalchemy.orm import Session

from .models import ImportJob, Order, OrderItem, ScanCode
from .orders_service import (
    ApiError,
    COMPLETED_STATUSES,
    STATUS_ARCHIVED_NO_KIZ,
    STATUS_CANCELLED,
    STATUS_REMOVED_FROM_GOOGLE,
    STATUS_RETURNED,
)
from .scan_quantities import (
    AGGREGATE_BOX_BLOCK_QUANTITY,
    AGGREGATE_BOX_PRODUCT_PREFIXES,
    scan_block_quantity,
)
from .schemas import (
    DashboardDaySummaryRead,
    DayReportOrder,
    DayReportPaymentGroup,
    DayReportRead,
    DayReportTotals,
)
from .settings import load_settings


def build_day_report(db: Session, report_date: str | None = None):
    parsed_date = parse_report_date(report_date)
    scan_day = scan_business_date_expression(db, ScanCode.raw_payload, ScanCode.scanned_at)
    candidates = union(
        select(Order.id.label("order_id")).where(Order.order_date == parsed_date),
        select(OrderItem.order_id.label("order_id"))
        .join(ScanCode, ScanCode.order_item_id == OrderItem.id)
        .where(scan_day == sql_date_value(db, parsed_date)),
    ).cte("day_report_candidates")
    candidate_ids = select(candidates.c.order_id)

    scan_facts = (
        select(
            ScanCode.order_item_id.label("item_id"),
            func.count(ScanCode.id).label("scan_codes"),
            func.coalesce(func.sum(case(
                (scan_day == sql_date_value(db, parsed_date), scan_block_quantity_expression(db)),
                else_=0,
            )), 0).label("scanned_today"),
        )
        .join(OrderItem, OrderItem.id == ScanCode.order_item_id)
        .where(OrderItem.order_id.in_(candidate_ids))
        .group_by(ScanCode.order_item_id)
        .cte("day_report_scan_facts")
    )
    item_facts = (
        select(
            OrderItem.order_id.label("order_id"),
            func.count(OrderItem.id).label("items"),
            func.coalesce(func.sum(case(
                (OrderItem.status.in_(tuple(COMPLETED_STATUSES)), 1),
                else_=0,
            )), 0).label("completed_items"),
            func.coalesce(func.sum(nonnegative_integer(OrderItem.quantity_blocks)), 0).label("planned_blocks"),
            func.coalesce(func.sum(nonnegative_integer(OrderItem.scanned_blocks)), 0).label("scanned_blocks"),
            func.coalesce(func.sum(func.coalesce(scan_facts.c.scanned_today, 0)), 0).label("scanned_today"),
            func.coalesce(func.sum(func.coalesce(scan_facts.c.scan_codes, 0)), 0).label("scan_codes"),
            func.coalesce(func.sum(json_integer_expression(db, OrderItem.raw_payload, "line_total")), 0).label("total_price"),
        )
        .outerjoin(scan_facts, scan_facts.c.item_id == OrderItem.id)
        .where(OrderItem.order_id.in_(candidate_ids))
        .group_by(OrderItem.order_id)
        .cte("day_report_item_facts")
    )
    rows = db.execute(
        select(
            Order.id,
            Order.order_date,
            Order.payment_type,
            Order.client,
            Order.address,
            Order.representative,
            Order.status,
            Order.raw_payload,
            func.coalesce(item_facts.c["items"], 0).label("items"),
            func.coalesce(item_facts.c.completed_items, 0).label("completed_items"),
            func.coalesce(item_facts.c.planned_blocks, 0).label("planned_blocks"),
            func.coalesce(item_facts.c.scanned_blocks, 0).label("scanned_blocks"),
            func.coalesce(item_facts.c.scanned_today, 0).label("scanned_today"),
            func.coalesce(item_facts.c.scan_codes, 0).label("scan_codes"),
            func.coalesce(item_facts.c.total_price, 0).label("total_price"),
        )
        .join(candidates, candidates.c.order_id == Order.id)
        .outerjoin(item_facts, item_facts.c.order_id == Order.id)
        .order_by(Order.order_date.asc(), Order.created_at.asc())
    ).mappings().all()

    report_orders = []
    payment_totals = {}
    totals = empty_totals()

    for row in rows:
        order_totals = aggregate_row_totals(row)
        raw_payload = row["raw_payload"] or {}
        report_orders.append(DayReportOrder(
            id=str(row["id"]),
            order_date=row["order_date"],
            payment_type=row["payment_type"],
            payment_group=payment_group(row["payment_type"]),
            client=row["client"],
            address=row["address"],
            coordinates=raw_payload.get("coordinates") or "",
            representative=row["representative"],
            status=row["status"],
            skladbot_request_number=raw_payload.get("skladbot_request_number") or "",
            **order_totals,
        ))
        add_totals(totals, order_totals, row["status"])
        add_payment_totals(payment_totals, row["payment_type"], order_totals)

    return DayReportRead(
        report_date=parsed_date,
        source="postgres",
        generated_at=datetime.now(report_timezone()),
        totals=DayReportTotals(**totals),
        payment_groups=[
            DayReportPaymentGroup(
                payment_group=group,
                payment_type=values["payment_type"],
                orders=values["orders"],
                planned_blocks=values["planned_blocks"],
                scanned_blocks=values["scanned_blocks"],
                scanned_today=values["scanned_today"],
                remaining_blocks=values["remaining_blocks"],
                scan_codes=values["scan_codes"],
                total_price=values["total_price"],
            )
            for group, values in sorted(payment_totals.items())
        ],
        orders=report_orders,
    )


def build_dashboard_day_summary(db: Session, report_date: str | None = None):
    parsed_date = parse_report_date(report_date)
    backend_import_id = json_text_expression(OrderItem.raw_payload, "backend_import_id")
    import_join = backend_import_id == cast(ImportJob.id, String)
    if db.get_bind().dialect.name == "sqlite":
        import_join = func.replace(backend_import_id, "-", "") == func.replace(cast(ImportJob.id, String), "-", "")
    loaded_day = timestamp_business_date_expression(db, func.coalesce(ImportJob.created_at, OrderItem.created_at))
    loaded_items = (
        select(
            OrderItem.id.label("item_id"),
            OrderItem.order_id.label("order_id"),
            OrderItem.quantity_blocks.label("quantity_blocks"),
            OrderItem.scanned_blocks.label("scanned_blocks"),
            OrderItem.status.label("item_status"),
            OrderItem.raw_payload.label("item_raw_payload"),
        )
        .outerjoin(ImportJob, import_join)
        .where(
            OrderItem.status != STATUS_REMOVED_FROM_GOOGLE,
            loaded_day == sql_date_value(db, parsed_date),
        )
        .cte("dashboard_loaded_items")
    )
    scan_day = scan_business_date_expression(db, ScanCode.raw_payload, ScanCode.scanned_at)
    scan_facts = (
        select(
            ScanCode.order_item_id.label("item_id"),
            func.count(ScanCode.id).label("scan_codes"),
            func.coalesce(func.sum(case(
                (scan_day == sql_date_value(db, parsed_date), scan_block_quantity_expression(db)),
                else_=0,
            )), 0).label("scanned_today"),
        )
        .join(loaded_items, loaded_items.c.item_id == ScanCode.order_item_id)
        .group_by(ScanCode.order_item_id)
        .cte("dashboard_scan_facts")
    )
    order_facts = (
        select(
            loaded_items.c.order_id,
            func.count(loaded_items.c.item_id).label("items"),
            func.coalesce(func.sum(case(
                (loaded_items.c.item_status.in_(tuple(COMPLETED_STATUSES)), 1),
                else_=0,
            )), 0).label("completed_items"),
            func.coalesce(func.sum(nonnegative_integer(loaded_items.c.quantity_blocks)), 0).label("planned_blocks"),
            func.coalesce(func.sum(nonnegative_integer(loaded_items.c.scanned_blocks)), 0).label("scanned_blocks"),
            func.coalesce(func.sum(func.coalesce(scan_facts.c.scanned_today, 0)), 0).label("scanned_today"),
            func.coalesce(func.sum(func.coalesce(scan_facts.c.scan_codes, 0)), 0).label("scan_codes"),
            func.coalesce(func.sum(json_integer_expression(
                db, loaded_items.c.item_raw_payload, "line_total"
            )), 0).label("total_price"),
        )
        .outerjoin(scan_facts, scan_facts.c.item_id == loaded_items.c.item_id)
        .group_by(loaded_items.c.order_id)
        .cte("dashboard_order_facts")
    )
    rows = db.execute(
        select(
            Order.status,
            Order.raw_payload,
            order_facts.c["items"],
            order_facts.c.completed_items,
            order_facts.c.planned_blocks,
            order_facts.c.scanned_blocks,
            order_facts.c.scanned_today,
            order_facts.c.scan_codes,
            order_facts.c.total_price,
        )
        .join(order_facts, order_facts.c.order_id == Order.id)
        .order_by(Order.created_at.asc(), Order.id.asc())
    ).mappings().all()

    totals = empty_totals()
    for row in rows:
        if is_returned_values(row["status"], row["raw_payload"]):
            continue
        if is_dashboard_excluded_status(row["status"]):
            continue
        add_totals(totals, aggregate_row_totals(row), row["status"])

    raw_return_status = func.trim(func.coalesce(json_text_expression(Order.raw_payload, "return_status"), ""))
    returned_day = raw_datetime_business_date_expression(
        db,
        json_text_expression(Order.raw_payload, "returned_at"),
        Order.updated_at,
        naive_is_utc=True,
    )
    return_rows = db.execute(
        select(Order.status, Order.raw_payload, Order.updated_at)
        .where(
            or_(func.lower(func.trim(Order.status)) == STATUS_RETURNED, raw_return_status != ""),
            returned_day == sql_date_value(db, parsed_date),
        )
    ).mappings().all()
    totals["returned_orders"] = sum(
        1 for row in return_rows
        if is_returned_values(row["status"], row["raw_payload"])
    )

    return DashboardDaySummaryRead(
        report_date=parsed_date,
        source="postgres_loaded_items",
        generated_at=datetime.now(report_timezone()),
        totals=DayReportTotals(**totals),
    )


def empty_totals():
    return {
        "orders": 0,
        "completed_orders": 0,
        "active_orders": 0,
        "returned_orders": 0,
        "items": 0,
        "completed_items": 0,
        "planned_blocks": 0,
        "scanned_blocks": 0,
        "scanned_today": 0,
        "remaining_blocks": 0,
        "scan_codes": 0,
        "total_price": 0,
    }


def aggregate_row_totals(row):
    planned_blocks = int(row["planned_blocks"] or 0)
    scanned_blocks = int(row["scanned_blocks"] or 0)
    return {
        "items": int(row["items"] or 0),
        "completed_items": int(row["completed_items"] or 0),
        "planned_blocks": planned_blocks,
        "scanned_blocks": scanned_blocks,
        "scanned_today": int(row["scanned_today"] or 0),
        "remaining_blocks": max(0, planned_blocks - scanned_blocks),
        "scan_codes": int(row["scan_codes"] or 0),
        "total_price": int(row["total_price"] or 0),
    }


def sql_date_value(db: Session, value: date):
    if db.get_bind().dialect.name == "sqlite":
        return value.isoformat()
    return value


def json_text_expression(column, key):
    return cast(column[key].as_string(), String)


def nonnegative_integer(column):
    return case((column > 0, column), else_=0)


def json_integer_expression(db: Session, column, key):
    raw_value = func.trim(func.coalesce(json_text_expression(column, key), ""))
    if db.get_bind().dialect.name == "postgresql":
        return case(
            (raw_value.op("~")(r"^[+-]?[0-9]+$"), cast(raw_value, Integer)),
            else_=0,
        )
    unsigned_value = case(
        (func.substr(raw_value, 1, 1).in_(("+", "-")), func.substr(raw_value, 2)),
        else_=raw_value,
    )
    valid_integer = and_(
        func.length(unsigned_value) > 0,
        unsigned_value.op("NOT GLOB")("*[^0-9]*"),
    )
    return case((valid_integer, cast(raw_value, Integer)), else_=0)


def scan_block_quantity_expression(db: Session):
    raw_quantity = json_integer_expression(db, ScanCode.raw_payload, "block_quantity")
    aggregate_box = or_(*(
        ScanCode.code.like(f"{prefix}%")
        for prefix in AGGREGATE_BOX_PRODUCT_PREFIXES
    ))
    return case(
        (raw_quantity > 0, raw_quantity),
        (aggregate_box, AGGREGATE_BOX_BLOCK_QUANTITY),
        else_=1,
    )


def timestamp_business_date_expression(db: Session, column):
    timezone_name = report_timezone_name()
    if db.get_bind().dialect.name == "postgresql":
        return cast(func.timezone(timezone_name, column), Date)
    return func.date(func.datetime(column, sqlite_timezone_modifier()))


def scan_business_date_expression(db: Session, raw_payload_column, fallback_column):
    raw_value = func.trim(func.coalesce(json_text_expression(raw_payload_column, "scanned_at"), ""))
    fallback_date = scan_timestamp_date_expression(db, fallback_column)
    if db.get_bind().dialect.name == "postgresql":
        aware_timestamp = raw_timestamp_has_timezone(db, raw_value)
        aware_date = cast(func.timezone(
            report_timezone_name(), cast(raw_value, TIMESTAMP(timezone=True))
        ), Date)
        naive_date = cast(cast(raw_value, TIMESTAMP(timezone=False)), Date)
        parsed_date = case(
            (and_(aware_timestamp, func.pg_input_is_valid(raw_value, literal("timestamp with time zone"))), aware_date),
            (and_(~aware_timestamp, func.pg_input_is_valid(raw_value, literal("timestamp without time zone"))), naive_date),
        )
    else:
        aware_timestamp = raw_timestamp_has_timezone(db, raw_value)
        parsed_date = case(
            (aware_timestamp, func.date(func.datetime(raw_value, sqlite_timezone_modifier()))),
            else_=func.date(raw_value),
        )
    return func.coalesce(parsed_date, fallback_date)


def scan_timestamp_date_expression(db: Session, column):
    if db.get_bind().dialect.name == "postgresql":
        return cast(func.timezone(report_timezone_name(), column), Date)
    # SQLite returns timezone-aware model values as naive datetimes. The legacy
    # Python fallback intentionally used that stored calendar date unchanged.
    return func.date(column)


def raw_datetime_business_date_expression(
    db: Session,
    raw_value,
    fallback_column,
    *,
    naive_is_utc: bool,
):
    raw_value = func.trim(func.coalesce(raw_value, ""))
    fallback_date = timestamp_business_date_expression(db, fallback_column)
    if db.get_bind().dialect.name == "postgresql":
        aware_timestamp = raw_timestamp_has_timezone(db, raw_value)
        aware_date = cast(func.timezone(
            report_timezone_name(), cast(raw_value, TIMESTAMP(timezone=True))
        ), Date)
        naive_timestamp = cast(raw_value, TIMESTAMP(timezone=False))
        if naive_is_utc:
            naive_timestamp = func.timezone("UTC", naive_timestamp)
            naive_date = cast(func.timezone(report_timezone_name(), naive_timestamp), Date)
        else:
            naive_date = cast(naive_timestamp, Date)
        parsed_date = case(
            (and_(aware_timestamp, func.pg_input_is_valid(raw_value, literal("timestamp with time zone"))), aware_date),
            (and_(~aware_timestamp, func.pg_input_is_valid(raw_value, literal("timestamp without time zone"))), naive_date),
        )
    else:
        parsed_date = func.date(func.datetime(raw_value, sqlite_timezone_modifier())) if naive_is_utc else case(
            (raw_timestamp_has_timezone(db, raw_value), func.date(func.datetime(raw_value, sqlite_timezone_modifier()))),
            else_=func.date(raw_value),
        )
    return func.coalesce(parsed_date, fallback_date)


def raw_timestamp_has_timezone(db: Session, raw_value):
    if db.get_bind().dialect.name == "postgresql":
        return or_(
            raw_value.op("~")(r"Z$"),
            raw_value.op("~")(r"[+-][0-9]{2}:?[0-9]{2}$"),
        )
    return or_(
        func.substr(raw_value, -1, 1) == "Z",
        func.substr(raw_value, -6, 1).in_(("+", "-")),
        func.substr(raw_value, -5, 1).in_(("+", "-")),
    )


def sqlite_timezone_modifier():
    offset = datetime.now(report_timezone()).utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    return f"{minutes:+d} minutes"


def report_timezone_name():
    return getattr(report_timezone(), "key", "Asia/Tashkent")


def parse_report_date(value: str | None):
    if not value:
        return datetime.now(report_timezone()).date()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text.split()[0], fmt).date()
        except ValueError:
            pass
    raise ApiError(422, "Invalid report_date. Use YYYY-MM-DD or DD.MM.YYYY")


def business_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(report_timezone()).date()
    if isinstance(value, date):
        return value
    return None


def is_dashboard_excluded_order(order: Order):
    return is_dashboard_excluded_status(order.status)


def is_dashboard_excluded_status(status):
    normalized_status = str(status or "").strip().lower()
    return normalized_status in {STATUS_RETURNED, STATUS_ARCHIVED_NO_KIZ, STATUS_CANCELLED}


def is_returned_order(order: Order):
    return is_returned_values(order.status, order.raw_payload)


def is_returned_values(status, raw_payload):
    raw_payload = raw_payload or {}
    return (
        str(status or "").strip().casefold() == STATUS_RETURNED
        or str(raw_payload.get("return_status") or "").strip().casefold() in {"returned", "return", "возврат"}
    )


def return_business_date(order: Order):
    raw_payload = order.raw_payload or {}
    returned_at = parse_datetime_value(raw_payload.get("returned_at"))
    if returned_at is not None:
        return business_date(returned_at)
    return business_date(order.updated_at)


def dashboard_item_loaded_date(item: OrderItem, import_dates: dict[str, date | None]):
    raw_payload = item.raw_payload or {}
    import_id = str(raw_payload.get("backend_import_id") or "")
    return import_dates.get(import_id) or business_date(item.created_at)


def should_include_order(order: Order, report_date: date):
    if order.order_date == report_date:
        return True
    return any(scan_business_date(scan) == report_date for item in order.items for scan in item.scan_codes)


def summarize_order(order: Order, report_date: date):
    return summarize_items(list(order.items), report_date)


def summarize_items(items: list[OrderItem], report_date: date):
    planned_blocks = sum(max(0, item.quantity_blocks or 0) for item in items)
    scanned_blocks = sum(max(0, item.scanned_blocks or 0) for item in items)
    scanned_today = sum(
        scan_block_quantity(scan)
        for item in items
        for scan in item.scan_codes
        if scan_business_date(scan) == report_date
    )
    completed_items = sum(1 for item in items if item.status in COMPLETED_STATUSES)
    return {
        "items": len(items),
        "completed_items": completed_items,
        "planned_blocks": planned_blocks,
        "scanned_blocks": scanned_blocks,
        "scanned_today": scanned_today,
        "remaining_blocks": max(0, planned_blocks - scanned_blocks),
        "scan_codes": sum(len(item.scan_codes) for item in items),
        "total_price": sum(parse_int((item.raw_payload or {}).get("line_total")) for item in items),
    }


def add_totals(totals, order_totals, order_status):
    totals["orders"] += 1
    if order_status == STATUS_RETURNED:
        totals["returned_orders"] += 1
    if order_status in COMPLETED_STATUSES:
        totals["completed_orders"] += 1
    else:
        totals["active_orders"] += 1
    for key in (
        "items",
        "completed_items",
        "planned_blocks",
        "scanned_blocks",
        "scanned_today",
        "remaining_blocks",
        "scan_codes",
        "total_price",
    ):
        totals[key] += order_totals[key]


def add_payment_totals(payment_totals, payment_type, order_totals):
    group = payment_group(payment_type)
    values = payment_totals.setdefault(group, {
        "payment_type": payment_type or "",
        "orders": 0,
        "planned_blocks": 0,
        "scanned_blocks": 0,
        "scanned_today": 0,
        "remaining_blocks": 0,
        "scan_codes": 0,
        "total_price": 0,
    })
    values["orders"] += 1
    for key in ("planned_blocks", "scanned_blocks", "scanned_today", "remaining_blocks", "scan_codes", "total_price"):
        values[key] += order_totals[key]


def payment_group(value):
    payment = str(value or "").strip().lower().replace("ё", "е")
    if "терминал" in payment or "terminal" in payment:
        return "terminal"
    if "перечис" in payment or "безнал" in payment or "transfer" in payment:
        return "transfer"
    return "unknown"


def scan_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.date()
        return value.astimezone(report_timezone()).date()
    if isinstance(value, date):
        return value
    return None


def scan_business_date(scan):
    raw_payload = scan.raw_payload or {}
    parsed = parse_datetime_value(raw_payload.get("scanned_at"))
    if parsed is not None:
        return scan_date(parsed)
    return scan_date(scan.scanned_at)


def parse_datetime_value(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def report_timezone():
    timezone_name = load_settings().timezone
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Tashkent")


def parse_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
