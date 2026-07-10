"""SkladBot order synchronization processor.

External HTTP and pure payload/matching contracts live in dedicated modules.
The scheduling loop lives in skladbot_worker_runner.
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import desc, or_, select, text
from sqlalchemy.orm import selectinload

from .audit_identity import AuditActor, set_audit_actor
from .db import SessionLocal
from .google_sheets_pending import queue_google_sheets_export
from .models import AuditLog, Order, OrderItem
from .order_statuses import COMPLETED_STATUSES
from .skladbot_client import (
    SkladBotClient,
    env_float,
    env_int,
    parse_skladbot_api_tokens,
    sanitize_skladbot_error,
    skladbot_response_error_text,
)
from .skladbot_contracts import (
    address_soft_match,
    business_timezone,
    business_today,
    client_matches,
    extract_list_items,
    field_map,
    get_field,
    nearest_request_diagnostics,
    normalize_lookup_text,
    normalize_payment_type,
    normalize_request_payload,
    normalize_smartup_id,
    normalize_text,
    order_group_payload,
    parse_bool,
    parse_date,
    parse_datetime_value,
    parse_int,
    product_matches,
    product_sku_key,
    request_list_value,
    request_match_diagnostics,
    request_matches_order,
    request_smartup_id,
    request_type_matches,
    request_value,
    simplify_tokens,
    smartup_id_from_comment,
    text_tokens_match,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

SKLADBOT_SYNC_LOCK_KEY = 22052631
SKLADBOT_COMPLETED_BACKFILL_STATUSES = ("completed", "done", "closed")

class CandidateRequests(list):
    def __init__(
        self,
        items=None,
        complete=True,
        reason="",
        details_checked=0,
        detail_limit=0,
        errors=None,
        checked_request_ids=None,
        last_checked_request_id=0,
        candidate_count=0,
        rotated_after_request_id=0,
    ):
        super().__init__(items or [])
        self.complete = complete
        self.reason = reason
        self.details_checked = details_checked
        self.detail_limit = detail_limit
        self.errors = errors or []
        self.checked_request_ids = checked_request_ids or []
        self.last_checked_request_id = last_checked_request_id
        self.candidate_count = candidate_count
        self.rotated_after_request_id = rotated_after_request_id

    def meta(self):
        return {
            "complete": self.complete,
            "reason": self.reason,
            "details_checked": self.details_checked,
            "detail_limit": self.detail_limit,
            "errors": self.errors,
            "checked_request_ids": self.checked_request_ids,
            "last_checked_request_id": self.last_checked_request_id,
            "candidate_count": self.candidate_count,
            "rotated_after_request_id": self.rotated_after_request_id,
        }

def date_in_window(value, today=None, lookback_days=1):
    today = today or business_today()
    parsed = parse_date(value)
    if not parsed:
        return False
    return today - timedelta(days=lookback_days) <= parsed <= today

def active_order_unloading_dates(orders=None, today=None):
    today = today or business_today()
    dates = {today + timedelta(days=1)}
    for order in orders or []:
        order_date = getattr(order, "order_date", None)
        if order_date:
            dates.add(order_date)
    return dates

def request_unloading_date_matches_active_orders(request, orders=None, today=None):
    parsed = parse_date(request.get("unloading_date") if isinstance(request, dict) else None)
    if not parsed:
        return False
    return parsed in active_order_unloading_dates(orders=orders, today=today)

def request_created_recently(request, today=None, lookback_days=1):
    dated_values = [
        value
        for value in (request.get("created_at"), request.get("updated_at"))
        if parse_date(value)
    ]
    if not dated_values:
        return False
    return any(date_in_window(value, today=today, lookback_days=lookback_days) for value in dated_values)

def dynamic_skladbot_lookback_days(orders=None, today=None, base_lookback_days=None):
    today = today or business_today()
    base_lookback_days = env_int("SKLADBOT_SYNC_LOOKBACK_DAYS", 1) if base_lookback_days is None else int(base_lookback_days or 0)
    max_lookback_days = max(base_lookback_days, env_int("SKLADBOT_SYNC_MAX_LOOKBACK_DAYS", 7))
    create_lead_days = max(0, env_int("SKLADBOT_ORDER_CREATE_LEAD_DAYS", 3))
    order_dates = [
        order.order_date
        for order in orders or []
        if getattr(order, "order_date", None) is not None
    ]
    if not order_dates:
        return max(0, base_lookback_days)
    oldest_order_date = min(order_dates)
    days_since_oldest_order = (today - oldest_order_date).days
    if days_since_oldest_order < 0:
        return max(0, base_lookback_days)
    required_lookback = days_since_oldest_order + create_lead_days
    return min(max(base_lookback_days, required_lookback), max_lookback_days)

def order_has_skladbot_number(order):
    raw_payload = getattr(order, "raw_payload", None) or {}
    return bool(normalize_text(raw_payload.get("skladbot_request_number")) or normalize_text(raw_payload.get("skladbot_request_id")))

def order_needs_skladbot_backfill(order):
    raw_payload = getattr(order, "raw_payload", None) or {}
    return not (
        normalize_text(raw_payload.get("skladbot_request_number"))
        and normalize_text(raw_payload.get("skladbot_request_id"))
    )

def completed_backfill_days():
    return max(0, env_int("SKLADBOT_COMPLETED_BACKFILL_DAYS", 2))

def completed_backfill_cutoffs(today=None, now=None):
    days = completed_backfill_days()
    now = now or datetime.now(timezone.utc)
    today = today or business_today(now)
    return today - timedelta(days=days), now - timedelta(days=days)

def load_skladbot_sync_orders(db, now=None):
    active_orders = db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .where(~Order.status.in_(COMPLETED_STATUSES))
        .order_by(Order.order_date.asc(), Order.created_at.asc())
    ).scalars().all()

    cutoff_date, cutoff_datetime = completed_backfill_cutoffs(now=now)
    completed_backfill = []
    if completed_backfill_days() > 0:
        completed_backfill = db.execute(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.status.in_(SKLADBOT_COMPLETED_BACKFILL_STATUSES))
            .where(
                or_(
                    Order.updated_at >= cutoff_datetime,
                    Order.order_date >= cutoff_date,
                )
            )
            .order_by(Order.updated_at.desc(), Order.created_at.desc())
        ).scalars().all()
        completed_backfill = [
            order
            for order in completed_backfill
            if order_needs_skladbot_backfill(order)
        ]

    seen = set()
    orders = []
    for order in [*active_orders, *completed_backfill]:
        order_id = str(order.id)
        if order_id in seen:
            continue
        seen.add(order_id)
        orders.append(order)
    return orders, active_orders, completed_backfill

def all_orders_have_candidate_match(orders, requests):
    if not orders:
        return False
    for order in orders:
        if not any(request_matches_order(order, request) for request in requests):
            return False
    return True

def fetch_candidate_requests(today=None, orders=None, client=None, start_after_request_id=0):
    client = client or SkladBotClient()
    if not client.configured:
        logging.info("SkladBot worker disabled: SKLADBOT_API_TOKEN is not configured")
        return CandidateRequests([], complete=True)

    lookback_days = dynamic_skladbot_lookback_days(orders=orders, today=today)
    detail_limit = max(1, env_int("SKLADBOT_DETAIL_LIMIT", 10))
    result = []
    details_checked = 0
    detail_errors = []
    stopped_by_limit = False
    list_items = []
    for item in client.list_requests():
        list_type = normalize_text(request_list_value(item, "type"))
        if list_type and not request_type_matches(list_type):
            continue
        request_id = parse_int(request_list_value(item, "id"))
        if request_id <= 0:
            continue
        list_unloading_date = request_list_value(item, "unloading_date", "unloadingDate")
        list_dates = {
            "created_at": request_list_value(item, "created_at", "createdAt"),
            "updated_at": request_list_value(item, "updated_at", "updatedAt"),
        }
        list_recent = request_created_recently(list_dates, today=today, lookback_days=lookback_days)
        list_unloading_matches = request_unloading_date_matches_active_orders(
            {"unloading_date": list_unloading_date},
            orders=orders,
            today=today,
        )
        has_list_dates = any(parse_date(value) for value in [*list_dates.values(), list_unloading_date])
        if has_list_dates and not list_recent and not list_unloading_matches:
            if parse_date(list_unloading_date) or not orders:
                continue
        freshness_candidates = [
            parse_date(list_dates.get("updated_at")),
            parse_date(list_dates.get("created_at")),
        ]
        freshness_date = max((value for value in freshness_candidates if value), default=None)
        if freshness_date is None:
            freshness_date = parse_date(list_unloading_date)
        priority = 0 if list_unloading_matches else 1 if list_recent else 2
        list_items.append((priority, freshness_date, request_id, item))

    list_items.sort(
        key=lambda value: (
            value[0],
            -(value[1].toordinal() if value[1] else 0),
        )
    )
    list_items = rotate_candidate_list_items(list_items, start_after_request_id)

    candidate_count = len(list_items)
    checked_request_ids = []
    for _priority, _freshness_date, _request_id, item in list_items:
        if details_checked >= detail_limit:
            stopped_by_limit = True
            break
        request_id = parse_int(request_list_value(item, "id"))
        try:
            detail = client.get_request_detail(request_id)
            details_checked += 1
            checked_request_ids.append(request_id)
        except httpx.HTTPStatusError as exc:
            detail_errors.append({"request_id": request_id, "error": f"HTTP {exc.response.status_code if exc.response is not None else 'unknown'}"})
            logging.warning(
                "SkladBot worker: skip request_id=%s after HTTP %s",
                request_id,
                exc.response.status_code if exc.response is not None else "unknown",
            )
            continue
        except Exception as exc:
            detail_errors.append({"request_id": request_id, "error": sanitize_skladbot_error(exc)})
            logging.warning(
                "SkladBot worker: skip request_id=%s after detail fetch error: %s",
                request_id,
                sanitize_skladbot_error(exc),
            )
            continue
        if client.request_delay:
            time.sleep(client.request_delay)
        request = normalize_request_payload(item, detail)
        if not request_type_matches(request.get("type")):
            continue
        if not (
            request_created_recently(request, today=today, lookback_days=lookback_days)
            or request_unloading_date_matches_active_orders(request, orders=orders, today=today)
        ):
            continue
        result.append(request)
        if orders and all_orders_have_candidate_match(orders, result):
            break
    complete = not detail_errors and not (stopped_by_limit and not all_orders_have_candidate_match(orders or [], result))
    reason = ""
    if detail_errors:
        reason = "detail_errors"
    if stopped_by_limit and not all_orders_have_candidate_match(orders or [], result):
        reason = "detail_limit_reached"
    logging.info(
        "SkladBot worker: candidates=%s details_checked=%s lookback_days=%s detail_limit=%s complete=%s reason=%s",
        len(result),
        details_checked,
        lookback_days,
        detail_limit,
        complete,
        reason,
    )
    return CandidateRequests(
        result,
        complete=complete,
        reason=reason,
        details_checked=details_checked,
        detail_limit=detail_limit,
        errors=detail_errors[:20],
        checked_request_ids=checked_request_ids,
        last_checked_request_id=checked_request_ids[-1] if checked_request_ids else 0,
        candidate_count=candidate_count,
        rotated_after_request_id=parse_int(start_after_request_id),
    )

def rotate_candidate_list_items(list_items, start_after_request_id=0):
    cursor = parse_int(start_after_request_id)
    if cursor <= 0 or len(list_items) < 2:
        return list_items
    for index, item in enumerate(list_items):
        if item[2] == cursor:
            return list_items[index + 1:] + list_items[:index + 1]
    return list_items

def update_orders_from_skladbot(audit_actor: AuditActor | None = None):
    checked_at = datetime.now(timezone.utc).isoformat()
    updated = 0
    matched = 0
    not_found = 0
    multiple = 0
    incomplete = 0
    pending = 0

    with SessionLocal() as db:
        if audit_actor is not None:
            set_audit_actor(db, audit_actor)
        if not try_acquire_skladbot_sync_lock(db):
            logging.info("SkladBot worker: another sync is already running, skip")
            return {"requests": 0, "updated": 0, "matched": 0, "not_found": 0, "multiple": 0, "busy": True}
        try:
            orders, active_orders, completed_backfill_orders = load_skladbot_sync_orders(db)
            if not orders:
                logging.info("SkladBot worker: no active or recent completed backend orders, skip SkladBot API")
                return {
                    "requests": 0,
                    "updated": 0,
                    "matched": 0,
                    "not_found": 0,
                    "multiple": 0,
                    "completed_backfill_orders": 0,
                }

            orders_to_check = [order for order in orders if order_needs_skladbot_backfill(order)]
            if not orders_to_check:
                logging.info("SkladBot worker: all active/recent orders already have SkladBot numbers, skip SkladBot API")
                google_sheets_result = export_skladbot_numbers_to_google_sheets(db, active_orders)
                db.add(AuditLog(
                    action="skladbot_google_sheets_export",
                    entity_type="skladbot",
                    entity_id="worker",
                    payload=google_sheets_result,
                ))
                db.commit()
                return {
                    "requests": 0,
                    "updated": 0,
                    "matched": 0,
                    "not_found": 0,
                    "multiple": 0,
                    "already_numbered": len(orders),
                    "active_orders": len(active_orders),
                    "completed_backfill_orders": len(completed_backfill_orders),
                    "google_sheets_export": google_sheets_result,
                }

            requests = fetch_candidate_requests(
                orders=orders_to_check,
                start_after_request_id=load_skladbot_fetch_cursor(db),
            )

            for order in orders_to_check:
                matches = [request for request in requests if request_matches_order(order, request)]
                raw_payload = dict(order.raw_payload or {})
                raw_payload["skladbot_checked_at"] = checked_at
                if len(matches) == 1:
                    request = matches[0]
                    raw_payload["skladbot_request_number"] = request.get("number") or ""
                    raw_payload["skladbot_request_id"] = str(request.get("id") or "")
                    raw_payload["skladbot_status"] = "found"
                    raw_payload["skladbot_raw"] = request.get("raw") or {}
                    matched += 1
                elif len(matches) > 1:
                    raw_payload["skladbot_status"] = "multiple"
                    raw_payload["skladbot_candidates"] = [
                        {"id": request.get("id"), "number": request.get("number")}
                        for request in matches[:10]
                    ]
                    raw_payload["skladbot_nearest"] = nearest_request_diagnostics(order, requests)
                    multiple += 1
                elif not getattr(requests, "complete", True):
                    raw_payload["skladbot_status"] = "pending"
                    raw_payload.pop("skladbot_error", None)
                    raw_payload["skladbot_fetch"] = requests.meta() if hasattr(requests, "meta") else {}
                    raw_payload["skladbot_nearest"] = nearest_request_diagnostics(order, requests)
                    incomplete += 1
                    pending += 1
                else:
                    raw_payload["skladbot_status"] = "not_found"
                    raw_payload["skladbot_nearest"] = nearest_request_diagnostics(order, requests)
                    not_found += 1
                order.raw_payload = raw_payload
                updated += 1

            db.add(AuditLog(
                action="skladbot_worker_sync",
                entity_type="skladbot",
                entity_id="worker",
                payload={
                    "requests": len(requests),
                    "orders_checked": len(orders_to_check),
                    "orders_already_numbered": len(orders) - len(orders_to_check),
                    "active_orders": len(active_orders),
                    "completed_backfill_orders": len(completed_backfill_orders),
                    "updated": updated,
                    "matched": matched,
                    "not_found": not_found,
                    "multiple": multiple,
                    "incomplete": incomplete,
                    "pending": pending,
                    "fetch": requests.meta() if hasattr(requests, "meta") else {},
                },
            ))
            include_archive = bool(completed_backfill_orders)
            google_sheets_result = export_skladbot_numbers_to_google_sheets(
                db,
                orders,
                include_inactive=include_archive,
                include_archive=include_archive,
                force=True,
            )
            db.add(AuditLog(
                action="skladbot_google_sheets_export",
                entity_type="skladbot",
                entity_id="worker",
                payload=google_sheets_result,
            ))
            db.commit()
        finally:
            release_skladbot_sync_lock(db)

    logging.info(
        "SkladBot worker: requests=%s orders=%s matched=%s not_found=%s multiple=%s pending=%s",
        len(requests),
        updated,
        matched,
        not_found,
        multiple,
        pending,
    )
    return {
        "requests": len(requests),
        "updated": updated,
        "matched": matched,
        "not_found": not_found,
        "multiple": multiple,
        "incomplete": incomplete,
        "pending": pending,
        "active_orders": len(active_orders),
        "completed_backfill_orders": len(completed_backfill_orders),
        "fetch": requests.meta() if hasattr(requests, "meta") else {},
        "google_sheets_export": google_sheets_result,
    }

def load_skladbot_fetch_cursor(db):
    event = db.execute(
        select(AuditLog)
        .where(AuditLog.action == "skladbot_worker_sync")
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    payload = event.payload if event is not None else {}
    fetch = payload.get("fetch") if isinstance(payload, dict) else {}
    if not isinstance(fetch, dict):
        return 0
    return parse_int(fetch.get("last_checked_request_id"))

def export_skladbot_numbers_to_google_sheets(db, orders, include_inactive=False, include_archive=False, force=False):
    order_ids = [str(order.id) for order in orders or []]
    if not order_ids:
        return {"status": "skipped", "updated": 0, "error": ""}
    if not force and not include_inactive and not include_archive and recent_skladbot_google_export_exists(db):
        return {
            "status": "skipped",
            "queued": False,
            "updated": 0,
            "error": "",
            "reason": "recent_export_cooldown",
        }
    result = {"status": "queued", "queued": True, "updated": 0, "error": ""}
    event = queue_google_sheets_export(
        db,
        "google_sheets_skladbot_export",
        "skladbot",
        "active_orders",
        result=result,
        payload={
            "order_ids": order_ids,
            "include_inactive": bool(include_inactive),
            "include_archive": bool(include_archive),
        },
    )
    return {**result, "pending_event_id": str(event.id) if event else ""}

def recent_skladbot_google_export_exists(db, min_interval_seconds=None):
    min_interval_seconds = (
        skladbot_google_export_min_interval_seconds()
        if min_interval_seconds is None
        else int(min_interval_seconds or 0)
    )
    if min_interval_seconds <= 0:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=min_interval_seconds)
    recent = db.execute(
        select(AuditLog)
        .where(AuditLog.action == "skladbot_google_sheets_export")
        .where(AuditLog.created_at >= cutoff)
        .order_by(desc(AuditLog.created_at), desc(AuditLog.id))
        .limit(1)
    ).scalar_one_or_none()
    return recent is not None

def skladbot_google_export_min_interval_seconds():
    return max(0, env_int("SKLADBOT_GOOGLE_EXPORT_MIN_INTERVAL_SECONDS", 300))

def try_acquire_skladbot_sync_lock(db):
    if db.bind.dialect.name != "postgresql":
        return True
    return bool(db.execute(
        text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
        {"lock_key": SKLADBOT_SYNC_LOCK_KEY},
    ).scalar())

def release_skladbot_sync_lock(db):
    if db.bind.dialect.name != "postgresql":
        return
    return
