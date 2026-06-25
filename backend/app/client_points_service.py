import hashlib
import re
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import AuditLog, ClientPoint, Order, OrderItem


DEFAULT_DELIVERY_FROM = "10:00"
DEFAULT_DELIVERY_TO = "18:00"


class ClientPointApiError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def list_client_points(db: Session, query="", custom_timeslot=None, limit=1000):
    row_limit = max(1, min(int(limit or 1000), 5000))
    order_meta = build_order_point_meta(db)
    saved_points = db.execute(
        select(ClientPoint).order_by(ClientPoint.client_name.asc(), ClientPoint.address.asc(), ClientPoint.id.asc())
    ).scalars().all()

    rows_by_key = {
        key: derived_point_to_read(key, meta)
        for key, meta in order_meta.items()
    }
    for point in saved_points:
        key = point_key(point.client_name)
        rows_by_key[key] = client_point_to_read(point, order_meta.get(key), source="saved")

    rows = list(rows_by_key.values())
    normalized_query = normalize_search_text(query)
    if normalized_query:
        rows = [
            row for row in rows
            if normalized_query in normalize_search_text(
                " ".join([
                    row["client_name"],
                    row["point_name"],
                    row["address"],
                    row["representative"],
                    row["coordinates"],
                ])
            )
        ]
    if custom_timeslot is not None:
        expected = bool(custom_timeslot)
        rows = [row for row in rows if row["has_custom_timeslot"] == expected]

    rows.sort(key=lambda row: (
        not row["has_custom_timeslot"],
        row["client_name"].casefold(),
        row["address"].casefold(),
    ))
    return rows[:row_limit]


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
    return client_point_to_read(point, build_order_point_meta(db).get(key), source="saved")


def sync_client_point_from_import_row(db: Session, row):
    client_name = normalize_text(row.get("client"))
    address = normalize_text(row.get("address"))
    if not client_name or not address or is_pickup_address(address):
        return None
    key = point_key(client_name)
    normalized_address = normalize_lookup_text(address)
    point = find_client_point(db, key, normalized_address)
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


def build_order_point_meta(db: Session):
    rows = db.execute(
        select(Order)
        .order_by(Order.client.asc(), Order.address.asc(), Order.order_date.asc(), Order.created_at.asc())
    ).scalars().all()
    meta_by_key = {}
    for order in rows:
        client_name = normalize_text(order.client)
        address = normalize_text(order.address)
        if not client_name or not address:
            continue
        raw_payload = order.raw_payload or {}
        key = point_key(client_name)
        meta = meta_by_key.setdefault(key, {
            "client_name": client_name,
            "point_name": "",
            "address": address,
            "coordinates": "",
            "representative": "",
            "orders_count": 0,
            "last_order_date": None,
        })
        meta["orders_count"] += 1
        if order.order_date and (meta["last_order_date"] is None or order.order_date > meta["last_order_date"]):
            meta["last_order_date"] = order.order_date
            meta["client_name"] = client_name
            meta["address"] = address
        if not meta["coordinates"] and normalize_text(raw_payload.get("coordinates")):
            meta["coordinates"] = normalize_text(raw_payload.get("coordinates"))
        if not meta["representative"] and normalize_text(order.representative):
            meta["representative"] = normalize_text(order.representative)
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
    query = (
        select(Order)
        .options(selectinload(Order.items))
        .order_by(Order.order_date.desc(), Order.created_at.desc())
    )
    if date_from:
        query = query.where(Order.order_date >= date_from)
    if date_to:
        query = query.where(Order.order_date <= date_to)

    totals = {
        "orders_count": 0,
        "positions_count": 0,
        "quantity_blocks": 0,
        "quantity_pieces": 0,
    }
    dates_by_key = {}
    display_client_name = requested_client_name

    for order in db.execute(query).scalars().all():
        if point_key(order.client) != normalized_client:
            continue
        display_client_name = normalize_text(order.client) or display_client_name
        date_key = order.order_date.isoformat() if order.order_date else ""
        date_row = dates_by_key.setdefault(date_key, {
            "shipment_date": order.order_date,
            "orders_count": 0,
            "positions_count": 0,
            "quantity_blocks": 0,
            "quantity_pieces": 0,
            "products_by_name": {},
        })
        totals["orders_count"] += 1
        date_row["orders_count"] += 1
        for item in sorted(order.items, key=lambda value: (normalize_text(value.product).casefold(), str(value.id))):
            product_name = normalize_text(item.product) or "Без названия"
            product = date_row["products_by_name"].setdefault(product_name, {
                "product": product_name,
                "positions_count": 0,
                "quantity_blocks": 0,
                "quantity_pieces": 0,
            })
            quantity_blocks = int(item.quantity_blocks or 0)
            quantity_pieces = int(item.quantity_pieces or 0)
            totals["positions_count"] += 1
            totals["quantity_blocks"] += quantity_blocks
            totals["quantity_pieces"] += quantity_pieces
            date_row["positions_count"] += 1
            date_row["quantity_blocks"] += quantity_blocks
            date_row["quantity_pieces"] += quantity_pieces
            product["positions_count"] += 1
            product["quantity_blocks"] += quantity_blocks
            product["quantity_pieces"] += quantity_pieces

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
                "orders_count": int(row.get("orders_count") or 0),
                "positions_count": int(row.get("positions_count") or 0),
                "quantity_blocks": int(row.get("quantity_blocks") or 0),
                "quantity_pieces": int(row.get("quantity_pieces") or 0),
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


def derived_point_to_read(key, meta):
    delivery_from = DEFAULT_DELIVERY_FROM
    delivery_to = DEFAULT_DELIVERY_TO
    return {
        "id": f"derived:{stable_point_id(key)}",
        "client_name": meta.get("client_name") or "",
        "point_name": meta.get("point_name") or "",
        "address": meta.get("address") or "",
        "coordinates": meta.get("coordinates") or "",
        "representative": meta.get("representative") or "",
        "delivery_from": delivery_from,
        "delivery_to": delivery_to,
        "is_active": True,
        "is_saved": False,
        "source": "orders",
        "has_custom_timeslot": False,
        "orders_count": int(meta.get("orders_count") or 0),
        "last_order_date": meta.get("last_order_date"),
        "created_at": None,
        "updated_at": None,
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
