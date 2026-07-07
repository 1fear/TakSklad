from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .logistics_service import is_logistics_candidate_order, is_returned_order
from .models import AuditLog, LogisticsCalendarDay, Order


DEFAULT_NON_WORKING_WEEKDAYS = (5, 6)
MAX_DELIVERY_SHIFT_DAYS = 31


@dataclass(frozen=True)
class DeliveryDateResolution:
    original_date: date
    effective_date: date
    adjusted: bool
    reason: str
    skipped_dates: tuple[str, ...]


def list_logistics_calendar(db: Session, month: str | None = None) -> dict[str, Any]:
    month_date = parse_calendar_month(month)
    first_day = date(month_date.year, month_date.month, 1)
    last_day = date(month_date.year, month_date.month, monthrange(month_date.year, month_date.month)[1])
    overrides = calendar_overrides(db, first_day, last_day)
    order_summary = calendar_order_summary(db, first_day, last_day)
    days = []
    current = first_day
    while current <= last_day:
        override = overrides.get(current)
        is_weekend = current.weekday() in DEFAULT_NON_WORKING_WEEKDAYS
        is_non_working = bool(override.is_non_working) if override else is_weekend
        summary = order_summary.get(current) or {}
        days.append({
            "date": current,
            "weekday": current.weekday(),
            "is_weekend": is_weekend,
            "is_non_working": is_non_working,
            "is_manual": override is not None,
            "reason": override.reason if override else ("Выходной" if is_weekend else ""),
            "source": override.source if override else ("weekend" if is_weekend else "default"),
            "orders_count": int(summary.get("orders_count") or 0),
            "active_orders": int(summary.get("active_orders") or 0),
            "completed_orders": int(summary.get("completed_orders") or 0),
            "returned_orders": int(summary.get("returned_orders") or 0),
            "planned_blocks": int(summary.get("planned_blocks") or 0),
            "clients": summary.get("clients") or [],
        })
        current += timedelta(days=1)
    return {
        "generated_at": datetime.now(timezone.utc),
        "month": first_day.strftime("%Y-%m"),
        "default_non_working_weekdays": list(DEFAULT_NON_WORKING_WEEKDAYS),
        "days": days,
    }


def set_logistics_calendar_day(db: Session, payload) -> dict[str, Any]:
    service_date = payload.service_date
    existing = db.execute(
        select(LogisticsCalendarDay).where(LogisticsCalendarDay.service_date == service_date)
    ).scalar_one_or_none()
    if existing is None:
        existing = LogisticsCalendarDay(service_date=service_date)
    old_state = {
        "is_non_working": bool(existing.is_non_working),
        "reason": existing.reason or "",
        "source": existing.source or "",
    }
    existing.is_non_working = bool(payload.is_non_working)
    existing.reason = normalize_text(payload.reason)
    existing.source = normalize_text(payload.source) or "manual"
    existing.actor = normalize_text(payload.actor)
    existing.raw_payload = {
        **(existing.raw_payload or {}),
        "updated_from": normalize_text(payload.source) or "web",
    }
    db.add(existing)
    db.add(AuditLog(
        action="logistics_calendar_day_updated",
        entity_type="logistics_calendar_day",
        entity_id=service_date.isoformat(),
        payload={
            "date": service_date.isoformat(),
            "old": old_state,
            "new": {
                "is_non_working": existing.is_non_working,
                "reason": existing.reason or "",
                "source": existing.source,
                "actor": existing.actor or "",
            },
        },
    ))
    db.commit()
    db.refresh(existing)
    return logistics_calendar_day_read(db, service_date)


def logistics_calendar_day_read(db: Session, service_date: date) -> dict[str, Any]:
    month = service_date.strftime("%Y-%m")
    for day in list_logistics_calendar(db, month)["days"]:
        if day["date"] == service_date:
            return day
    raise ValueError(f"Дата {service_date.isoformat()} не найдена в календаре")


def resolve_effective_delivery_date(
    db: Session | None,
    delivery_date: date,
    *,
    default_non_working_weekdays: tuple[int, ...] = DEFAULT_NON_WORKING_WEEKDAYS,
) -> DeliveryDateResolution:
    current = delivery_date
    skipped_dates = []
    for _ in range(MAX_DELIVERY_SHIFT_DAYS + 1):
        if not is_logistics_non_working_day(
            db,
            current,
            default_non_working_weekdays=default_non_working_weekdays,
        ):
            adjusted = current != delivery_date
            return DeliveryDateResolution(
                original_date=delivery_date,
                effective_date=current,
                adjusted=adjusted,
                reason="non_working_logistics_day" if adjusted else "",
                skipped_dates=tuple(skipped_dates),
            )
        skipped_dates.append(current.isoformat())
        current += timedelta(days=1)
    raise ValueError(f"Не найден рабочий день логистики после {delivery_date.isoformat()}")


def is_logistics_non_working_day(
    db: Session | None,
    service_date: date,
    *,
    default_non_working_weekdays: tuple[int, ...] = DEFAULT_NON_WORKING_WEEKDAYS,
) -> bool:
    if db is not None:
        override = db.execute(
            select(LogisticsCalendarDay).where(LogisticsCalendarDay.service_date == service_date)
        ).scalar_one_or_none()
        if override is not None:
            return bool(override.is_non_working)
    return service_date.weekday() in default_non_working_weekdays


def calendar_overrides(db: Session, first_day: date, last_day: date) -> dict[date, LogisticsCalendarDay]:
    rows = db.execute(
        select(LogisticsCalendarDay)
        .where(LogisticsCalendarDay.service_date >= first_day)
        .where(LogisticsCalendarDay.service_date <= last_day)
    ).scalars().all()
    return {row.service_date: row for row in rows}


def calendar_order_summary(db: Session, first_day: date, last_day: date) -> dict[date, dict[str, Any]]:
    orders = db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.order_date >= first_day)
        .where(Order.order_date <= last_day)
        .order_by(Order.order_date.asc(), Order.created_at.asc())
    ).scalars().all()
    summary: dict[date, dict[str, Any]] = {}
    for order in orders:
        returned_order = is_returned_order(order)
        if not returned_order and not is_logistics_candidate_order(order):
            continue
        service_date = order.order_date
        if not isinstance(service_date, date):
            continue
        day = summary.setdefault(service_date, {
            "orders_count": 0,
            "active_orders": 0,
            "completed_orders": 0,
            "returned_orders": 0,
            "planned_blocks": 0,
            "clients": [],
        })
        if returned_order:
            day["returned_orders"] += 1
            if order.client and order.client not in day["clients"]:
                day["clients"].append(order.client)
            continue
        day["orders_count"] += 1
        if order.status == "completed":
            day["completed_orders"] += 1
        else:
            day["active_orders"] += 1
        day["planned_blocks"] += sum(int(item.quantity_blocks or 0) for item in order.items)
        if order.client and order.client not in day["clients"]:
            day["clients"].append(order.client)
    for day in summary.values():
        day["clients"] = day["clients"][:6]
    return summary


def parse_calendar_month(value: str | None) -> date:
    text = normalize_text(value)
    if not text:
        today = date.today()
        return date(today.year, today.month, 1)
    try:
        parsed = datetime.strptime(text[:7], "%Y-%m").date()
    except ValueError as exc:
        raise ValueError(f"Некорректный месяц календаря: {text}") from exc
    return date(parsed.year, parsed.month, 1)


def normalize_text(value: Any) -> str:
    return str(value or "").strip()
