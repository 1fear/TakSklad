"""Точки логистики, добавленные руками во вкладке «Календарь»

Ручная точка существует только для маршрутного листа: заказ на складе она
не создаёт, заявку в СкладБот не рождает, в КИЗ-отчёты и сверку со Smartup
не попадает. Поэтому она живёт своей таблицей и подмешивается ровно в двух
местах: список дня календаря и XLSX логистики
"""

from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from .client_points_service import (
    ClientPointApiError,
    DEFAULT_DELIVERY_FROM,
    DEFAULT_DELIVERY_TO,
    ensure_timeslot_order,
    find_client_point,
    normalize_lookup_text,
    normalize_optional,
    normalize_required,
    normalize_text,
    normalize_timeslot,
    point_key,
)
from .logistics_zone_service import ZONE_CITY, classify_order, parse_coordinates
from .models import AuditLog, ClientPoint, LogisticsManualStop


MANUAL_STOP_SOURCE = "manual_stop"


def list_logistics_manual_stops(db: Session, service_date: date):
    return db.execute(
        select(LogisticsManualStop)
        .where(LogisticsManualStop.service_date == service_date)
        .where(LogisticsManualStop.is_active.is_(True))
        .order_by(LogisticsManualStop.created_at.asc(), LogisticsManualStop.id.asc())
    ).scalars().all()


def manual_stop_rows(
    db: Session, service_date: date, region_index, region_directory_empty=False
) -> list[dict[str, Any]]:
    """Строки ручных точек для карточки дня, зона считается тем же правилом, что у заказов.

    Пустой справочник областных точек уводит в город всю доставку, включая
    ручные точки: иначе карточка дня показала бы область там, где её нет в XLSX
    """
    rows = []
    for stop in list_logistics_manual_stops(db, service_date):
        rows.append({
            "id": str(stop.id),
            "zone": ZONE_CITY if region_directory_empty else classify_order(
                stop.client_name, stop.coordinates, region_index
            ),
            "client": stop.client_name or "",
            "point_name": stop.point_name or "",
            "address": stop.address or "",
            "coordinates": stop.coordinates or "",
            "representative": stop.representative or "",
            "delivery_from": stop.delivery_from or DEFAULT_DELIVERY_FROM,
            "delivery_to": stop.delivery_to or DEFAULT_DELIVERY_TO,
            "blocks": int(stop.blocks or 0),
            "comment": stop.comment or "",
        })
    return rows


def save_logistics_manual_stop(db: Session, payload) -> dict[str, Any]:
    service_date = payload.service_date
    client_name = normalize_required(payload.client_name, "client_name")
    address = normalize_required(payload.address, "address")
    coordinates = normalize_coordinates_required(payload.coordinates)
    point_name = normalize_text(payload.point_name)
    representative = normalize_text(payload.representative)
    comment = normalize_text(payload.comment)
    delivery_from = normalize_timeslot(
        normalize_optional(payload.delivery_from, DEFAULT_DELIVERY_FROM), "delivery_from"
    )
    delivery_to = normalize_timeslot(
        normalize_optional(payload.delivery_to, DEFAULT_DELIVERY_TO), "delivery_to"
    )
    ensure_timeslot_order(delivery_from, delivery_to)
    blocks = normalize_blocks(payload.blocks)
    actor = normalize_text(payload.actor)

    stop = None
    if payload.id:
        stop = db.execute(
            select(LogisticsManualStop).where(LogisticsManualStop.id == payload.id)
        ).scalar_one_or_none()
        if stop is None or not stop.is_active:
            raise ClientPointApiError(404, "Manual stop not found")
    old_state = manual_stop_audit_state(stop) if stop is not None else None
    if stop is None:
        stop = LogisticsManualStop(service_date=service_date)
        db.add(stop)

    stop.service_date = service_date
    stop.client_name = client_name
    stop.point_name = point_name or None
    stop.address = address
    stop.coordinates = coordinates
    stop.representative = representative or None
    stop.delivery_from = delivery_from
    stop.delivery_to = delivery_to
    stop.blocks = blocks
    stop.comment = comment or None
    stop.is_active = True
    stop.actor = actor or None
    stop.raw_payload = {
        **(stop.raw_payload or {}),
        "source": MANUAL_STOP_SOURCE,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    if payload.save_to_directory:
        sync_client_point_from_manual_stop(db, stop)

    db.add(AuditLog(
        action="logistics_manual_stop_updated" if old_state else "logistics_manual_stop_created",
        entity_type="logistics_manual_stop",
        entity_id=str(stop.id),
        payload={
            "date": service_date.isoformat(),
            "old": old_state or {},
            "new": manual_stop_audit_state(stop),
            "actor": actor,
            "save_to_directory": bool(payload.save_to_directory),
        },
    ))
    db.commit()
    db.refresh(stop)
    return manual_stop_read(stop)


def delete_logistics_manual_stop(db: Session, stop_id, actor="") -> dict[str, Any]:
    stop = db.execute(
        select(LogisticsManualStop).where(LogisticsManualStop.id == coerce_stop_id(stop_id))
    ).scalar_one_or_none()
    if stop is None or not stop.is_active:
        raise ClientPointApiError(404, "Manual stop not found")
    old_state = manual_stop_audit_state(stop)
    # Мягкое удаление: строка выходит из карточки дня и из XLSX, но остаётся
    # видна аудиту, иначе «кто и что убрал из маршрута» восстановить нечем
    stop.is_active = False
    stop.actor = normalize_text(actor) or stop.actor
    db.add(AuditLog(
        action="logistics_manual_stop_deleted",
        entity_type="logistics_manual_stop",
        entity_id=str(stop.id),
        payload={
            "date": stop.service_date.isoformat(),
            "old": old_state,
            "actor": normalize_text(actor),
        },
    ))
    db.commit()
    db.refresh(stop)
    return manual_stop_read(stop)


def sync_client_point_from_manual_stop(db: Session, stop) -> ClientPoint | None:
    """Положить точку в справочник, чтобы в следующий раз она нашлась поиском."""
    key = point_key(stop.client_name)
    normalized_address = normalize_lookup_text(stop.address)
    if not key or not normalized_address:
        return None
    with db.no_autoflush:
        point = find_client_point(db, key, normalized_address)
    if point is None:
        point = ClientPoint(
            client_name=stop.client_name,
            point_name=stop.point_name,
            address=stop.address,
            normalized_client=key,
            normalized_address=normalized_address,
            coordinates=stop.coordinates,
            representative=stop.representative,
            delivery_from=stop.delivery_from,
            delivery_to=stop.delivery_to,
            is_active=True,
            raw_payload={"source": MANUAL_STOP_SOURCE},
        )
        db.add(point)
        return point
    # Существующую точку справочника ручная не перетирает: у неё уже может
    # быть выверенное окно доставки и адрес из импорта. Дописываются только
    # пустые поля, остальное остаётся как было
    if not normalize_text(point.coordinates):
        point.coordinates = stop.coordinates
    if not normalize_text(point.representative) and stop.representative:
        point.representative = stop.representative
    if not normalize_text(point.point_name) and stop.point_name:
        point.point_name = stop.point_name
    return point


def manual_stop_read(stop) -> dict[str, Any]:
    return {
        "id": str(stop.id),
        "service_date": stop.service_date,
        "client_name": stop.client_name or "",
        "point_name": stop.point_name or "",
        "address": stop.address or "",
        "coordinates": stop.coordinates or "",
        "representative": stop.representative or "",
        "delivery_from": stop.delivery_from or DEFAULT_DELIVERY_FROM,
        "delivery_to": stop.delivery_to or DEFAULT_DELIVERY_TO,
        "blocks": int(stop.blocks or 0),
        "comment": stop.comment or "",
        "is_active": bool(stop.is_active),
    }


def manual_stop_audit_state(stop) -> dict[str, Any]:
    return {
        "client_name": stop.client_name or "",
        "point_name": stop.point_name or "",
        "address": stop.address or "",
        "coordinates": stop.coordinates or "",
        "representative": stop.representative or "",
        "delivery_from": stop.delivery_from or "",
        "delivery_to": stop.delivery_to or "",
        "blocks": int(stop.blocks or 0),
        "comment": stop.comment or "",
        "is_active": bool(stop.is_active),
    }


def coerce_stop_id(value):
    """UUID приходит и объектом от pydantic, и строкой от вызывающего кода."""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        raise ClientPointApiError(422, "Invalid manual stop id") from None


def normalize_coordinates_required(value) -> str:
    """Координаты обязательны: без них строка ушла бы на лист «Требуют координаты»."""
    text = normalize_required(value, "coordinates")
    point = parse_coordinates(text)
    if point is None:
        raise ClientPointApiError(422, "Invalid coordinates: use «41.311081, 69.240562»")
    latitude, longitude = point
    return f"{latitude}, {longitude}"


def normalize_blocks(value) -> int:
    try:
        blocks = int(value)
    except (TypeError, ValueError):
        raise ClientPointApiError(422, "blocks must be a whole number") from None
    if blocks < 0:
        raise ClientPointApiError(422, "blocks must be zero or greater")
    return blocks
