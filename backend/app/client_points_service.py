import hashlib
import re
from datetime import date, datetime, timezone

from sqlalchemy import String, case, func, literal, or_, select, union
from sqlalchemy.orm import Session

from .models import AuditLog, ClientPoint, Order, OrderItem
from .orders_service import STATUS_RETURNED


DEFAULT_DELIVERY_FROM = "10:00"
DEFAULT_DELIVERY_TO = "18:00"


class ClientPointApiError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def register_sqlite_normalizers(db: Session) -> None:
    if db.bind is None or db.bind.dialect.name != "sqlite":
        return
    connection = db.connection().connection
    driver_connection = getattr(connection, "driver_connection", connection)
    driver_connection.create_function("taksklad_point_key", 1, point_key, deterministic=True)
    driver_connection.create_function("taksklad_search_text", 1, normalize_search_text, deterministic=True)


def sql_point_key(db: Session, column):
    if db.bind is not None and db.bind.dialect.name == "sqlite":
        register_sqlite_normalizers(db)
        return func.taksklad_point_key(column, type_=String())
    lowered = func.lower(func.replace(func.coalesce(column, ""), "ё", "е"))
    return func.regexp_replace(lowered, r"[^0-9a-zа-я]+", "", "g", type_=String())


def sql_search_text(db: Session, value):
    if db.bind is not None and db.bind.dialect.name == "sqlite":
        register_sqlite_normalizers(db)
        return func.taksklad_search_text(value, type_=String())
    return func.lower(func.replace(func.coalesce(value, ""), "ё", "е"), type_=String())


def sql_returned_order_predicate():
    return_status = func.lower(func.coalesce(Order.raw_payload["return_status"].as_string(), ""))
    return or_(
        func.lower(func.coalesce(Order.status, "")) == STATUS_RETURNED,
        return_status.in_(("returned", "return", "возврат")),
    )


def list_client_points(db: Session, query="", custom_timeslot=None, limit=None, offset=0):
    row_limit = None if limit is None else max(1, int(limit))
    row_offset = max(0, int(offset or 0))
    order_aggregate, order_display, order_coordinates, order_representative = order_point_ctes(db)
    saved_key = sql_point_key(db, ClientPoint.client_name)
    saved_ranked = select(
        ClientPoint.id.label("id"),
        saved_key.label("point_key"),
        ClientPoint.client_name.label("client_name"),
        ClientPoint.point_name.label("point_name"),
        ClientPoint.address.label("address"),
        ClientPoint.coordinates.label("coordinates"),
        ClientPoint.representative.label("representative"),
        ClientPoint.delivery_from.label("delivery_from"),
        ClientPoint.delivery_to.label("delivery_to"),
        ClientPoint.is_active.label("is_active"),
        ClientPoint.created_at.label("created_at"),
        ClientPoint.updated_at.label("updated_at"),
        func.row_number().over(
            partition_by=saved_key,
            order_by=(
                ClientPoint.client_name.desc(),
                ClientPoint.address.desc(),
                ClientPoint.id.desc(),
            ),
        ).label("saved_rank"),
    ).cte("client_point_saved_ranked")
    saved = select(saved_ranked).where(saved_ranked.c.saved_rank == 1).cte("client_point_saved")
    point_keys = union(
        select(order_aggregate.c.point_key),
        select(saved.c.point_key),
    ).cte("client_point_keys")

    is_saved = saved.c.id.is_not(None)
    client_name = func.coalesce(saved.c.client_name, order_display.c.client_name, "")
    point_name = func.coalesce(saved.c.point_name, "")
    address = func.coalesce(saved.c.address, order_display.c.address, "")
    coordinates = func.coalesce(
        func.nullif(saved.c.coordinates, ""),
        order_coordinates.c.coordinates,
        "",
    )
    representative = func.coalesce(
        func.nullif(saved.c.representative, ""),
        order_representative.c.representative,
        "",
    )
    delivery_from = func.coalesce(func.nullif(saved.c.delivery_from, ""), DEFAULT_DELIVERY_FROM)
    delivery_to = func.coalesce(func.nullif(saved.c.delivery_to, ""), DEFAULT_DELIVERY_TO)
    has_custom_timeslot = or_(
        delivery_from != DEFAULT_DELIVERY_FROM,
        delivery_to != DEFAULT_DELIVERY_TO,
    )
    searchable = (
        client_name + literal(" ") + point_name + literal(" ") + address
        + literal(" ") + representative + literal(" ") + coordinates
    )
    statement = (
        select(
            point_keys.c.point_key,
            saved.c.id,
            client_name.label("client_name"),
            point_name.label("point_name"),
            address.label("address"),
            coordinates.label("coordinates"),
            representative.label("representative"),
            delivery_from.label("delivery_from"),
            delivery_to.label("delivery_to"),
            func.coalesce(saved.c.is_active, True).label("is_active"),
            is_saved.label("is_saved"),
            has_custom_timeslot.label("has_custom_timeslot"),
            func.coalesce(order_aggregate.c.orders_count, 0).label("orders_count"),
            func.coalesce(order_aggregate.c.returned_orders_count, 0).label("returned_orders_count"),
            order_aggregate.c.last_order_date,
            saved.c.created_at,
            saved.c.updated_at,
        )
        .select_from(point_keys)
        .outerjoin(order_aggregate, order_aggregate.c.point_key == point_keys.c.point_key)
        .outerjoin(order_display, order_display.c.point_key == point_keys.c.point_key)
        .outerjoin(order_coordinates, order_coordinates.c.point_key == point_keys.c.point_key)
        .outerjoin(order_representative, order_representative.c.point_key == point_keys.c.point_key)
        .outerjoin(saved, saved.c.point_key == point_keys.c.point_key)
    )
    normalized_query = normalize_search_text(query)
    if normalized_query:
        statement = statement.where(sql_search_text(db, searchable).contains(normalized_query, autoescape=True))
    if custom_timeslot is not None:
        statement = statement.where(has_custom_timeslot.is_(bool(custom_timeslot)))
    statement = statement.order_by(
        case((has_custom_timeslot, 0), else_=1),
        sql_search_text(db, client_name).asc(),
        sql_search_text(db, address).asc(),
    )
    if row_limit is not None:
        statement = statement.limit(row_limit)
    if row_offset:
        statement = statement.offset(row_offset)

    rows = []
    for row in db.execute(statement).mappings():
        saved_point = bool(row["is_saved"])
        rows.append({
            "id": str(row["id"]) if saved_point else f"derived:{stable_point_id(row['point_key'])}",
            "client_name": row["client_name"] or "",
            "point_name": row["point_name"] or "",
            "address": row["address"] or "",
            "coordinates": row["coordinates"] or "",
            "representative": row["representative"] or "",
            "delivery_from": row["delivery_from"] or DEFAULT_DELIVERY_FROM,
            "delivery_to": row["delivery_to"] or DEFAULT_DELIVERY_TO,
            "is_active": bool(row["is_active"]),
            "is_saved": saved_point,
            "source": "saved" if saved_point else "orders",
            "has_custom_timeslot": bool(row["has_custom_timeslot"]),
            "orders_count": int(row["orders_count"] or 0),
            "returned_orders_count": int(row["returned_orders_count"] or 0),
            "last_order_date": row["last_order_date"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
    return rows


def update_client_point_timeslot(db: Session, payload):
    client_name = normalize_required(payload.client_name, "client_name")
    address = normalize_required(payload.address, "address")
    delivery_from = normalize_timeslot(payload.delivery_from, "delivery_from")
    delivery_to = normalize_timeslot(payload.delivery_to, "delivery_to")
    ensure_timeslot_order(delivery_from, delivery_to)
    key = point_key(client_name)
    normalized_address = normalize_lookup_text(address)

    point = find_client_point(db, key, normalized_address)
    old_delivery_from = point.delivery_from if point else DEFAULT_DELIVERY_FROM
    old_delivery_to = point.delivery_to if point else DEFAULT_DELIVERY_TO
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if point is None:
        point = ClientPoint(
            client_name=client_name,
            point_name=None,
            address=address,
            normalized_client=key,
            normalized_address=normalized_address,
            coordinates=normalize_text(payload.coordinates) or None,
            representative=normalize_text(payload.representative) or None,
            delivery_from=delivery_from,
            delivery_to=delivery_to,
            is_active=bool(payload.is_active),
            raw_payload={"created_by": normalize_text(payload.actor) or "web", "created_reason": normalize_text(payload.reason)},
        )
        db.add(point)
        db.flush()
    else:
        point.client_name = client_name
        point.normalized_client = key
        point.address = address
        point.normalized_address = normalized_address
        point.coordinates = normalize_optional(payload.coordinates, point.coordinates)
        point.representative = normalize_optional(payload.representative, point.representative)
        point.delivery_from = delivery_from
        point.delivery_to = delivery_to
        point.is_active = bool(payload.is_active)
        point.raw_payload = {
            **(point.raw_payload or {}),
            "updated_by": normalize_text(payload.actor) or "web",
            "updated_reason": normalize_text(payload.reason),
            "updated_at": now,
        }

    db.add(AuditLog(
        action="client_point_timeslot_updated",
        entity_type="client_point",
        entity_id=str(point.id),
        payload={
            "client_name": client_name,
            "address": address,
            "old_delivery_from": old_delivery_from,
            "old_delivery_to": old_delivery_to,
            "delivery_from": delivery_from,
            "delivery_to": delivery_to,
            "actor": normalize_text(payload.actor) or "web",
            "source": "web",
            "reason": normalize_text(payload.reason),
        },
    ))
    db.commit()
    db.refresh(point)
    return client_point_to_read(point, build_order_point_meta(db, key).get(key), source="saved")


def sync_client_point_from_import_row(db: Session, row):
    return sync_client_point_from_import_row_cached(db, row)


def prefetch_client_points_for_import(db: Session, rows):
    normalized_clients = {
        point_key(row.get("client"))
        for row in rows
        if normalize_text(row.get("client"))
        and normalize_text(row.get("address"))
        and not is_pickup_address(row.get("address"))
    }
    cache = {key: None for key in normalized_clients}
    if not normalized_clients:
        return cache

    statement = (
        select(ClientPoint)
        .where(ClientPoint.normalized_client.in_(normalized_clients))
        .order_by(
            ClientPoint.updated_at.desc(),
            ClientPoint.created_at.desc(),
            ClientPoint.id.asc(),
        )
    )
    with db.no_autoflush:
        points = db.execute(statement).scalars()
        for point in points:
            key = point_key(point.client_name)
            if key in cache and cache[key] is None:
                cache[key] = point
    return cache


def sync_client_point_from_import_row_cached(db: Session, row, cache=None):
    client_name = normalize_text(row.get("client"))
    address = normalize_text(row.get("address"))
    if not client_name or not address or is_pickup_address(address):
        return None
    key = point_key(client_name)
    normalized_address = normalize_lookup_text(address)
    if cache is None:
        point = find_client_point(db, key, normalized_address)
    elif key in cache:
        point = cache[key]
    else:
        with db.no_autoflush:
            point = find_client_point(db, key, normalized_address)
        cache[key] = point
    if point is None:
        point = ClientPoint(
            client_name=client_name,
            point_name=None,
            address=address,
            normalized_client=key,
            normalized_address=normalized_address,
            coordinates=normalize_text(row.get("coordinates")) or None,
            representative=normalize_text(row.get("representative")) or None,
            delivery_from=DEFAULT_DELIVERY_FROM,
            delivery_to=DEFAULT_DELIVERY_TO,
            is_active=True,
            raw_payload={"source": "import"},
        )
        db.add(point)
        if cache is not None:
            cache[key] = point
        return point
    point.client_name = client_name
    point.normalized_client = key
    point.address = address
    point.normalized_address = normalized_address
    coordinates = normalize_text(row.get("coordinates"))
    representative = normalize_text(row.get("representative"))
    if coordinates:
        point.coordinates = coordinates
    if representative:
        point.representative = representative
    point.raw_payload = {
        **(point.raw_payload or {}),
        "source": "import",
        "last_import_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if cache is not None:
        cache[key] = point
    return point


def client_point_delivery_slot_map(db: Session, orders):
    points = db.execute(select(ClientPoint).where(ClientPoint.is_active.is_(True))).scalars().all()
    slot_map = {}
    for point in points:
        if not point.delivery_from or not point.delivery_to:
            continue
        slot = (point.delivery_from, point.delivery_to)
        slot_map[point_key(point.client_name)] = slot
    return slot_map


def delivery_slot_for_order(order, slot_map):
    return slot_map.get(point_key(order.client), (DEFAULT_DELIVERY_FROM, DEFAULT_DELIVERY_TO))


def order_point_ctes(db: Session):
    order_key = sql_point_key(db, Order.client)
    coordinates = func.coalesce(Order.raw_payload["coordinates"].as_string(), "")
    representative = func.coalesce(Order.representative, "")
    returned = sql_returned_order_predicate()
    legacy_order = (
        Order.client.asc(),
        Order.address.asc(),
        case((Order.order_date.is_(None), 1), else_=0),
        Order.order_date.asc(),
        Order.created_at.asc(),
        Order.id.asc(),
    )
    ranked = select(
        order_key.label("point_key"),
        Order.client.label("client_name"),
        Order.address.label("address"),
        coordinates.label("coordinates"),
        representative.label("representative"),
        Order.order_date.label("order_date"),
        returned.label("is_returned"),
        func.row_number().over(
            partition_by=order_key,
            order_by=(
                case((Order.order_date.is_(None), 1), else_=0),
                Order.order_date.desc(),
                Order.client.asc(),
                Order.address.asc(),
                Order.created_at.asc(),
                Order.id.asc(),
            ),
        ).label("display_rank"),
        func.row_number().over(
            partition_by=order_key,
            order_by=(case((func.trim(coordinates) == "", 1), else_=0), *legacy_order),
        ).label("coordinates_rank"),
        func.row_number().over(
            partition_by=order_key,
            order_by=(case((func.trim(representative) == "", 1), else_=0), *legacy_order),
        ).label("representative_rank"),
    ).where(order_key != "").cte("client_point_order_ranked")
    aggregate = (
        select(
            ranked.c.point_key,
            func.sum(case((ranked.c.is_returned, 0), else_=1)).label("orders_count"),
            func.sum(case((ranked.c.is_returned, 1), else_=0)).label("returned_orders_count"),
            func.max(ranked.c.order_date).label("last_order_date"),
        )
        .group_by(ranked.c.point_key)
        .cte("client_point_order_aggregate")
    )
    display = select(
        ranked.c.point_key,
        ranked.c.client_name,
        ranked.c.address,
    ).where(ranked.c.display_rank == 1).cte("client_point_order_display")
    first_coordinates = select(
        ranked.c.point_key,
        ranked.c.coordinates,
    ).where(
        ranked.c.coordinates_rank == 1,
        func.trim(ranked.c.coordinates) != "",
    ).cte("client_point_order_coordinates")
    first_representative = select(
        ranked.c.point_key,
        ranked.c.representative,
    ).where(
        ranked.c.representative_rank == 1,
        func.trim(ranked.c.representative) != "",
    ).cte("client_point_order_representative")
    return aggregate, display, first_coordinates, first_representative


def build_order_point_meta(db: Session, normalized_client=None):
    aggregate, display, coordinates, representative = order_point_ctes(db)
    statement = (
        select(
            aggregate.c.point_key,
            display.c.client_name,
            display.c.address,
            coordinates.c.coordinates,
            representative.c.representative,
            aggregate.c.orders_count,
            aggregate.c.returned_orders_count,
            aggregate.c.last_order_date,
        )
        .join(display, display.c.point_key == aggregate.c.point_key)
        .outerjoin(coordinates, coordinates.c.point_key == aggregate.c.point_key)
        .outerjoin(representative, representative.c.point_key == aggregate.c.point_key)
    )
    if normalized_client is not None:
        statement = statement.where(aggregate.c.point_key == point_key(normalized_client))
    meta_by_key = {}
    for row in db.execute(statement).mappings():
        key = row["point_key"]
        meta_by_key[key] = {
            "client_name": row["client_name"] or "",
            "point_name": "",
            "address": row["address"] or "",
            "coordinates": row["coordinates"] or "",
            "representative": row["representative"] or "",
            "orders_count": int(row["orders_count"] or 0),
            "returned_orders_count": int(row["returned_orders_count"] or 0),
            "last_order_date": row["last_order_date"],
        }
    return meta_by_key


def get_client_point_order_summary(
    db: Session,
    client_name,
    date_from: date | None = None,
    date_to: date | None = None,
):
    requested_client_name = normalize_required(client_name, "client_name")
    if date_from and date_to and date_from > date_to:
        raise ClientPointApiError(422, "date_from must be earlier than date_to")

    normalized_client = point_key(requested_client_name)
    point_filter = sql_point_key(db, Order.client) == normalized_client
    order_filters = [point_filter]
    if date_from:
        order_filters.append(Order.order_date >= date_from)
    if date_to:
        order_filters.append(Order.order_date <= date_to)

    returned = sql_returned_order_predicate()
    order_rows = db.execute(
        select(
            Order.order_date.label("shipment_date"),
            Order.payment_type.label("payment_type"),
            returned.label("is_returned"),
            func.count(Order.id).label("orders_count"),
        )
        .where(*order_filters)
        .group_by(Order.order_date, Order.payment_type, returned)
    ).mappings()
    order_reference_rows = db.execute(
        select(
            Order.id.label("order_id"),
            Order.order_date.label("shipment_date"),
            func.coalesce(Order.raw_payload["skladbot_request_number"].as_string(), "").label(
                "skladbot_request_number"
            ),
            func.coalesce(Order.raw_payload["skladbot_request_id"].as_string(), "").label(
                "skladbot_request_id"
            ),
            returned.label("is_returned"),
        )
        .where(*order_filters)
        .order_by(returned.asc(), Order.created_at.asc(), Order.id.asc())
    ).mappings()
    display_client_name = db.execute(
        select(Order.client)
        .where(*order_filters)
        .order_by(Order.order_date.asc(), Order.created_at.asc())
        .limit(1)
    ).scalar_one_or_none() or requested_client_name
    product_name = func.coalesce(func.nullif(func.trim(OrderItem.product), ""), "Без названия")
    item_rows = db.execute(
        select(
            Order.order_date.label("shipment_date"),
            product_name.label("product"),
            func.count(OrderItem.id).label("positions_count"),
            func.coalesce(func.sum(OrderItem.quantity_blocks), 0).label("quantity_blocks"),
            func.coalesce(func.sum(OrderItem.quantity_pieces), 0).label("quantity_pieces"),
        )
        .select_from(Order)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(*order_filters)
        .group_by(Order.order_date, product_name)
    ).mappings()

    totals = {
        "orders_count": 0,
        "returned_orders_count": 0,
        "positions_count": 0,
        "quantity_blocks": 0,
        "quantity_pieces": 0,
    }
    dates_by_key = {}
    for row in order_rows:
        shipment_date = row["shipment_date"]
        date_key = shipment_date.isoformat() if shipment_date else ""
        date_row = dates_by_key.setdefault(date_key, {
            "shipment_date": shipment_date,
            "payment_types": set(),
            "orders_count": 0,
            "returned_orders_count": 0,
            "positions_count": 0,
            "quantity_blocks": 0,
            "quantity_pieces": 0,
            "order_references": [],
            "products_by_name": {},
        })
        payment_type = normalize_text(row["payment_type"])
        if payment_type:
            date_row["payment_types"].add(payment_type)
        order_count = int(row["orders_count"] or 0)
        if row["is_returned"]:
            totals["returned_orders_count"] += order_count
            date_row["returned_orders_count"] += order_count
        else:
            totals["orders_count"] += order_count
            date_row["orders_count"] += order_count

    for row in order_reference_rows:
        shipment_date = row["shipment_date"]
        date_key = shipment_date.isoformat() if shipment_date else ""
        dates_by_key[date_key]["order_references"].append({
            "order_id": str(row["order_id"]),
            "skladbot_request_number": normalize_text(row["skladbot_request_number"]),
            "skladbot_request_id": normalize_text(row["skladbot_request_id"]),
            "is_returned": bool(row["is_returned"]),
        })

    for row in item_rows:
        shipment_date = row["shipment_date"]
        date_key = shipment_date.isoformat() if shipment_date else ""
        date_row = dates_by_key[date_key]
        positions_count = int(row["positions_count"] or 0)
        quantity_blocks = int(row["quantity_blocks"] or 0)
        quantity_pieces = int(row["quantity_pieces"] or 0)
        totals["positions_count"] += positions_count
        totals["quantity_blocks"] += quantity_blocks
        totals["quantity_pieces"] += quantity_pieces
        date_row["positions_count"] += positions_count
        date_row["quantity_blocks"] += quantity_blocks
        date_row["quantity_pieces"] += quantity_pieces
        date_row["products_by_name"][row["product"]] = {
            "product": row["product"],
            "positions_count": positions_count,
            "quantity_blocks": quantity_blocks,
            "quantity_pieces": quantity_pieces,
        }

    dates = sorted(
        dates_by_key.values(),
        key=lambda row: (row.get("shipment_date") is not None, row.get("shipment_date")),
        reverse=True,
    )
    return {
        "client_name": display_client_name,
        "normalized_client": normalized_client,
        "totals": totals,
        "dates": [
            {
                "shipment_date": row.get("shipment_date"),
                "payment_type": ", ".join(sorted(row.get("payment_types") or [], key=str.casefold)),
                "orders_count": int(row.get("orders_count") or 0),
                "returned_orders_count": int(row.get("returned_orders_count") or 0),
                "positions_count": int(row.get("positions_count") or 0),
                "quantity_blocks": int(row.get("quantity_blocks") or 0),
                "quantity_pieces": int(row.get("quantity_pieces") or 0),
                "order_references": list(row.get("order_references") or []),
                "products": sorted(
                    [
                        {
                            "product": product.get("product") or "",
                            "positions_count": int(product.get("positions_count") or 0),
                            "quantity_blocks": int(product.get("quantity_blocks") or 0),
                            "quantity_pieces": int(product.get("quantity_pieces") or 0),
                        }
                        for product in (row.get("products_by_name") or {}).values()
                    ],
                    key=lambda product: product["product"].casefold(),
                ),
            }
            for row in dates
        ],
    }


def client_point_to_read(point: ClientPoint, meta=None, source="saved"):
    meta = meta or {}
    delivery_from = point.delivery_from or DEFAULT_DELIVERY_FROM
    delivery_to = point.delivery_to or DEFAULT_DELIVERY_TO
    return {
        "id": str(point.id),
        "client_name": point.client_name,
        "point_name": point.point_name or "",
        "address": point.address,
        "coordinates": point.coordinates or meta.get("coordinates") or "",
        "representative": point.representative or meta.get("representative") or "",
        "delivery_from": delivery_from,
        "delivery_to": delivery_to,
        "is_active": bool(point.is_active),
        "is_saved": True,
        "source": source,
        "has_custom_timeslot": delivery_from != DEFAULT_DELIVERY_FROM or delivery_to != DEFAULT_DELIVERY_TO,
        "orders_count": int(meta.get("orders_count") or 0),
        "returned_orders_count": int(meta.get("returned_orders_count") or 0),
        "last_order_date": meta.get("last_order_date"),
        "created_at": point.created_at,
        "updated_at": point.updated_at,
    }


def point_key(client_name):
    return normalize_lookup_text(client_name)


def find_client_point(db: Session, normalized_client, normalized_address):
    point = db.execute(
        select(ClientPoint)
        .where(ClientPoint.normalized_client == normalized_client)
        .order_by(ClientPoint.updated_at.desc(), ClientPoint.created_at.desc(), ClientPoint.id.asc())
    ).scalars().first()
    if point is not None:
        return point
    return db.execute(
        select(ClientPoint)
        .where(ClientPoint.normalized_client == normalized_client)
        .where(ClientPoint.normalized_address == normalized_address)
    ).scalars().first()


def stable_point_id(key):
    return hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:16]


def normalize_lookup_text(value):
    text = normalize_text(value).casefold().replace("ё", "е")
    return re.sub(r"[^0-9a-zа-я]+", "", text)


def normalize_search_text(value):
    return normalize_text(value).casefold().replace("ё", "е")


def normalize_text(value):
    return str(value or "").strip()


def normalize_required(value, field):
    text = normalize_text(value)
    if not text:
        raise ClientPointApiError(422, f"{field} is required")
    return text


def normalize_optional(value, fallback):
    text = normalize_text(value)
    return text if text else fallback


def normalize_timeslot(value, field):
    text = normalize_required(value, field)
    match = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?", text)
    if not match:
        raise ClientPointApiError(422, f"Invalid {field}: use HH:MM")
    hours = int(match.group(1))
    minutes = int(match.group(2) or 0)
    if hours > 23 or minutes > 59:
        raise ClientPointApiError(422, f"Invalid {field}: use HH:MM")
    return f"{hours:02d}:{minutes:02d}"


def ensure_timeslot_order(delivery_from, delivery_to):
    if delivery_from >= delivery_to:
        raise ClientPointApiError(422, "delivery_from must be earlier than delivery_to")


def is_pickup_address(value):
    text = normalize_lookup_text(value)
    return text.startswith("самовывоз")
