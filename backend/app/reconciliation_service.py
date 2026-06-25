from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .db import SessionLocal
from .google_backend_sync_diagnostic import item_source_key, record_source_key
from .google_sheets_exporter import STATUS_COMPLETED as SHEET_STATUS_COMPLETED, backend_item_sheet_status, format_skladbot_status, normalize_text
from .google_sheets_sync_worker import load_google_sheet_records
from .models import AuditLog, Incident, Order, OrderItem, PendingEvent
from .orders_service import COMPLETED_STATUSES, STATUS_ARCHIVED_NO_KIZ, STATUS_CANCELLED, STATUS_REMOVED_FROM_GOOGLE
from .redaction import redact_secrets
from .reports_service import report_timezone


RECONCILIATION_SOURCE = "daily_reconciliation"
TELEGRAM_NOTIFICATION_EVENT_TYPE = "telegram_notification"
CRITICAL_INCIDENT_TYPES = {"google_mirror_mismatch", "skladbot_gap"}


class ReconciliationError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def run_daily_reconciliation(
    *,
    db: Session | None = None,
    report_date=None,
    google_records=None,
    google_error: str = "",
    alert_chat_ids=None,
    detail_limit: int = 20,
) -> dict:
    parsed_date = parse_report_date(report_date)
    if db is not None:
        return build_daily_reconciliation(
            db,
            parsed_date,
            google_records=google_records,
            google_error=google_error,
            alert_chat_ids=alert_chat_ids,
            detail_limit=detail_limit,
        )
    with SessionLocal() as session:
        return build_daily_reconciliation(
            session,
            parsed_date,
            google_records=google_records,
            google_error=google_error,
            alert_chat_ids=alert_chat_ids,
            detail_limit=detail_limit,
        )


def build_daily_reconciliation(
    db: Session,
    report_date: date,
    *,
    google_records=None,
    google_error: str = "",
    alert_chat_ids=None,
    detail_limit: int = 20,
) -> dict:
    detail_limit = max(1, min(int(detail_limit or 20), 100))
    orders = load_orders_for_date(db, report_date)
    google_records, google_status = resolve_google_records(google_records, google_error, report_date)
    db_items = visible_items(orders)
    google_items = records_for_date(google_records, report_date)
    google_summary = (
        compare_google_mirror(db_items, google_items, detail_limit)
        if google_status["status"] == "ok"
        else empty_google_summary()
    )
    google_summary["status"] = google_status["status"]
    google_summary["error"] = google_status["error"]
    skladbot_summary = summarize_skladbot_gaps(orders, detail_limit)
    db_summary = summarize_db_orders(orders, db_items)

    incident_specs = build_incident_specs(report_date, google_summary, skladbot_summary)
    incidents = [
        upsert_reconciliation_incident(db, report_date, spec)
        for spec in incident_specs
    ]
    alerts = queue_reconciliation_alerts(db, report_date, incidents, alert_chat_ids or [])
    status = reconciliation_status(google_summary, skladbot_summary)
    db.commit()

    return {
        "source": "postgres",
        "status": status,
        "report_date": report_date.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": db_summary,
        "google": google_summary,
        "skladbot": skladbot_summary,
        "incidents": [incident_to_summary(incident) for incident in incidents],
        "alerts": alerts,
    }


def load_orders_for_date(db: Session, report_date: date) -> list[Order]:
    return db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(Order.order_date == report_date)
        .order_by(Order.created_at.asc(), Order.id.asc())
    ).scalars().all()


def resolve_google_records(google_records, google_error: str, report_date: date) -> tuple[list[dict], dict]:
    if google_records is not None:
        return list(google_records or []), {"status": "ok", "error": ""}
    if normalize_text(google_error):
        return [], {"status": "error", "error": redact_secrets(google_error)}
    try:
        return load_google_sheet_records(), {"status": "ok", "error": ""}
    except Exception as exc:
        return [], {
            "status": "error",
            "error": redact_secrets(normalize_text(exc) or exc.__class__.__name__),
            "report_date": report_date.isoformat(),
        }


def visible_items(orders: list[Order]) -> list[OrderItem]:
    items = []
    for order in orders:
        if normalize_text(order.status) in {STATUS_CANCELLED, STATUS_ARCHIVED_NO_KIZ}:
            continue
        for item in order.items or []:
            if normalize_text(item.status) != STATUS_REMOVED_FROM_GOOGLE:
                items.append(item)
    return items


def records_for_date(records: list[dict], report_date: date) -> list[dict]:
    return [
        record
        for record in records or []
        if normalize_record_date(record.get("order_date")) == report_date.isoformat()
    ]


def compare_google_mirror(db_items: list[OrderItem], records: list[dict], detail_limit: int) -> dict:
    db_index = build_unique_index(db_items, item_source_key)
    google_index = build_unique_index(records, record_source_key)
    db_active_index = {
        key: item
        for key, item in db_index["items"].items()
        if normalize_text(item.order.status) not in COMPLETED_STATUSES
    }
    google_only = [
        record_to_summary(record)
        for key, record in google_index["items"].items()
        if key not in db_index["items"]
    ]
    db_only_active = [
        item_to_summary(item)
        for key, item in db_active_index.items()
        if key not in google_index["items"]
    ]
    status_mismatches = []
    wh_r_mismatches = []
    for key in sorted(set(db_index["items"]) & set(google_index["items"])):
        item = db_index["items"][key]
        record = google_index["items"][key]
        status_mismatch = compare_status(item, record)
        if status_mismatch:
            status_mismatches.append(status_mismatch)
        wh_r_mismatches.extend(compare_skladbot_fields(item, record))

    return {
        "rows": len(records),
        "matched_items": len(set(db_index["items"]) & set(google_index["items"])),
        "google_only_rows": len(google_only),
        "db_only_active_items": len(db_only_active),
        "status_mismatches": len(status_mismatches),
        "wh_r_mismatches": len(wh_r_mismatches),
        "duplicate_google_keys": len(google_index["duplicates"]),
        "duplicate_db_keys": len(db_index["duplicates"]),
        "details": {
            "google_only": google_only[:detail_limit],
            "db_only_active": db_only_active[:detail_limit],
            "status_mismatches": status_mismatches[:detail_limit],
            "wh_r_mismatches": wh_r_mismatches[:detail_limit],
            "duplicate_google_keys": google_index["duplicates"][:detail_limit],
            "duplicate_db_keys": db_index["duplicates"][:detail_limit],
        },
    }


def empty_google_summary() -> dict:
    return {
        "rows": 0,
        "matched_items": 0,
        "google_only_rows": 0,
        "db_only_active_items": 0,
        "status_mismatches": 0,
        "wh_r_mismatches": 0,
        "duplicate_google_keys": 0,
        "duplicate_db_keys": 0,
        "details": {
            "google_only": [],
            "db_only_active": [],
            "status_mismatches": [],
            "wh_r_mismatches": [],
            "duplicate_google_keys": [],
            "duplicate_db_keys": [],
        },
    }


def summarize_skladbot_gaps(orders: list[Order], detail_limit: int) -> dict:
    missing = []
    problem = []
    for order in orders:
        if normalize_text(order.status) in COMPLETED_STATUSES or normalize_text(order.status) in {STATUS_CANCELLED, STATUS_ARCHIVED_NO_KIZ}:
            continue
        raw_payload = order.raw_payload or {}
        has_request = bool(normalize_text(raw_payload.get("skladbot_request_number")) or normalize_text(raw_payload.get("skladbot_request_id")))
        status = normalize_text(raw_payload.get("skladbot_status"))
        if not has_request:
            missing.append(order_to_summary(order))
        elif status in {"not_found", "multiple", "error", "create_failed", "blocked", "pending"}:
            problem.append({**order_to_summary(order), "skladbot_status": status})
    return {
        "missing_request_orders": len(missing),
        "problem_status_orders": len(problem),
        "details": {
            "missing_request_orders": missing[:detail_limit],
            "problem_status_orders": problem[:detail_limit],
        },
    }


def summarize_db_orders(orders: list[Order], items: list[OrderItem]) -> dict:
    active_orders = [
        order for order in orders
        if normalize_text(order.status) not in COMPLETED_STATUSES
        and normalize_text(order.status) not in {STATUS_CANCELLED, STATUS_ARCHIVED_NO_KIZ}
    ]
    completed_orders = [order for order in orders if normalize_text(order.status) in COMPLETED_STATUSES]
    return {
        "orders": len(orders),
        "active_orders": len(active_orders),
        "completed_orders": len(completed_orders),
        "items": len(items),
        "active_items": len([item for item in items if normalize_text(item.order.status) not in COMPLETED_STATUSES]),
        "planned_blocks": sum(int(item.quantity_blocks or 0) for item in items),
        "scanned_blocks": sum(int(item.scanned_blocks or 0) for item in items),
    }


def build_incident_specs(report_date: date, google_summary: dict, skladbot_summary: dict) -> list[dict]:
    specs = []
    google_mismatch_total = sum(int(google_summary.get(key) or 0) for key in (
        "google_only_rows",
        "db_only_active_items",
        "status_mismatches",
        "wh_r_mismatches",
        "duplicate_google_keys",
        "duplicate_db_keys",
    ))
    if google_mismatch_total:
        specs.append({
            "kind": "google_mirror_mismatch",
            "severity": "critical",
            "title": f"Daily reconciliation found Google mirror drift for {report_date.isoformat()}",
            "message": "DB is source of truth. Check web panel incidents, then resync Google mirror for affected orders.",
            "raw_payload": {
                "report_date": report_date.isoformat(),
                "google": google_summary,
                "next_action": "Open TakSklad web panel, review daily_reconciliation incident, run Google resync for affected orders.",
            },
        })
    if google_summary.get("status") == "error":
        specs.append({
            "kind": "google_mirror_unavailable",
            "severity": "warning",
            "title": f"Daily reconciliation could not read Google mirror for {report_date.isoformat()}",
            "message": "DB workflow is not failed. Google mirror should be checked and retried separately.",
            "raw_payload": {
                "report_date": report_date.isoformat(),
                "google": {
                    "status": google_summary.get("status"),
                    "error": google_summary.get("error"),
                },
                "next_action": "Check Google credentials/quota and retry mirror sync. Do not stop warehouse scanning.",
            },
        })
    skladbot_gap_total = int(skladbot_summary.get("missing_request_orders") or 0) + int(skladbot_summary.get("problem_status_orders") or 0)
    if skladbot_gap_total:
        specs.append({
            "kind": "skladbot_gap",
            "severity": "critical",
            "title": f"Daily reconciliation found SkladBot gaps for {report_date.isoformat()}",
            "message": "Some active orders have no usable SkladBot WH-R/status. Check stock/create incidents before warehouse picking.",
            "raw_payload": {
                "report_date": report_date.isoformat(),
                "skladbot": skladbot_summary,
                "next_action": "Open incidents, verify SkladBot request creation/matching, retry only after stock and data are correct.",
            },
        })
    return specs


def upsert_reconciliation_incident(db: Session, report_date: date, spec: dict) -> Incident:
    external_ref = f"reconciliation:{report_date.isoformat()}:{spec['kind']}"
    incident = db.execute(
        select(Incident)
        .where(Incident.source == RECONCILIATION_SOURCE)
        .where(Incident.external_ref == external_ref)
        .order_by(Incident.created_at.asc(), Incident.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if incident is None:
        incident = Incident(
            source=RECONCILIATION_SOURCE,
            severity=spec["severity"],
            status="open",
            title=spec["title"],
            message=spec["message"],
            entity_type="external",
            external_ref=external_ref,
            raw_payload=spec["raw_payload"],
        )
        db.add(incident)
        db.flush()
        action = "reconciliation_incident_created"
    else:
        incident.severity = spec["severity"]
        incident.status = "open"
        incident.title = spec["title"]
        incident.message = spec["message"]
        incident.raw_payload = spec["raw_payload"]
        incident.resolved_at = None
        action = "reconciliation_incident_updated"
    db.add(AuditLog(
        action=action,
        entity_type="incident",
        entity_id=str(incident.id),
        payload={
            "source": RECONCILIATION_SOURCE,
            "external_ref": external_ref,
            "report_date": report_date.isoformat(),
            "severity": spec["severity"],
            "updated_at": now.isoformat(),
        },
    ))
    return incident


def queue_reconciliation_alerts(db: Session, report_date: date, incidents: list[Incident], chat_ids) -> list[dict]:
    alerts = []
    for incident in incidents:
        kind = normalize_text(incident.external_ref).split(":")[-1]
        if incident.severity != "critical" or kind not in CRITICAL_INCIDENT_TYPES:
            continue
        for chat_id in [normalize_text(value) for value in chat_ids if normalize_text(value)]:
            idempotency_key = f"telegram:notification:v1:reconciliation:{report_date.isoformat()}:{kind}:{incident.id}:{chat_id}"
            existing = db.execute(select(PendingEvent).where(PendingEvent.idempotency_key == idempotency_key)).scalar_one_or_none()
            if existing is not None:
                alerts.append({
                    "incident_id": str(incident.id),
                    "chat_id": chat_id,
                    "idempotency_key": idempotency_key,
                    "status": "deduped",
                    "event_id": str(existing.id),
                })
                continue
            event = PendingEvent(
                event_type=TELEGRAM_NOTIFICATION_EVENT_TYPE,
                status="pending",
                idempotency_key=idempotency_key,
                payload={
                    "kind": "daily_reconciliation_alert",
                    "chat_id": chat_id,
                    "incident_id": str(incident.id),
                    "source": RECONCILIATION_SOURCE,
                    "report_date": report_date.isoformat(),
                    "text": reconciliation_alert_text(report_date, incident),
                },
            )
            db.add(event)
            db.flush()
            alerts.append({
                "incident_id": str(incident.id),
                "chat_id": chat_id,
                "idempotency_key": idempotency_key,
                "status": "queued",
                "event_id": str(event.id),
            })
    return alerts


def reconciliation_alert_text(report_date: date, incident: Incident) -> str:
    payload = incident.raw_payload or {}
    next_action = normalize_text(payload.get("next_action")) or "Open TakSklad web panel and review daily_reconciliation incident."
    lines = [
        "TakSklad: требуется проверка сверки",
        "",
        f"Дата: {format_date_ru(report_date)}",
        f"Источник: {RECONCILIATION_SOURCE}",
        f"Проблема: {incident.title}",
        f"Инцидент: {incident.external_ref}",
        "",
        f"Что сделать: {next_action}",
    ]
    return "\n".join(lines)


def reconciliation_status(google_summary: dict, skladbot_summary: dict) -> str:
    critical_total = sum(int(google_summary.get(key) or 0) for key in (
        "google_only_rows",
        "db_only_active_items",
        "status_mismatches",
        "wh_r_mismatches",
        "duplicate_google_keys",
        "duplicate_db_keys",
    ))
    critical_total += int(skladbot_summary.get("missing_request_orders") or 0)
    critical_total += int(skladbot_summary.get("problem_status_orders") or 0)
    if critical_total:
        return "action_required"
    if google_summary.get("status") == "error":
        return "mirror_issue"
    return "ok"


def build_unique_index(values, key_func):
    items = {}
    duplicates = []
    for value in values:
        key = normalize_text(key_func(value))
        if not key:
            continue
        if key in items:
            duplicates.append(key)
            continue
        items[key] = value
    return {"items": items, "duplicates": sorted(set(duplicates))}


def compare_status(item: OrderItem, record: dict) -> dict | None:
    if normalize_text(item.order.status).casefold() == "returned":
        backend_status = SHEET_STATUS_COMPLETED
    else:
        backend_status = backend_item_sheet_status(item)
    google_status = normalize_text(record.get("status"))
    if google_status and normalize_text(backend_status).casefold() != google_status.casefold():
        return {
            **item_to_summary(item),
            "row_number": record.get("row_number"),
            "field": "status",
            "backend": backend_status,
            "google": google_status,
        }
    return None


def compare_skladbot_fields(item: OrderItem, record: dict) -> list[dict]:
    order_raw = item.order.raw_payload or {}
    comparisons = [
        ("skladbot_request_number", order_raw.get("skladbot_request_number"), record.get("skladbot_request_number")),
        ("skladbot_request_id", order_raw.get("skladbot_request_id"), record.get("skladbot_request_id")),
        ("skladbot_status", format_skladbot_status(order_raw.get("skladbot_status")), format_skladbot_status(record.get("skladbot_status"))),
    ]
    mismatches = []
    for field, backend_value, google_value in comparisons:
        backend_text = normalize_text(backend_value)
        google_text = normalize_text(google_value)
        if backend_text.casefold() != google_text.casefold():
            mismatches.append({
                **item_to_summary(item),
                "row_number": record.get("row_number"),
                "field": field,
                "backend": backend_text,
                "google": google_text,
            })
    return mismatches


def item_to_summary(item: OrderItem) -> dict:
    order = item.order
    return {
        "order_id": str(order.id),
        "item_id": str(item.id),
        "source_key": item_source_key(item),
        "client": normalize_text(order.client),
        "product": normalize_text(item.product),
        "order_status": normalize_text(order.status),
        "item_status": normalize_text(item.status),
    }


def order_to_summary(order: Order) -> dict:
    raw_payload = order.raw_payload or {}
    return {
        "order_id": str(order.id),
        "client": normalize_text(order.client),
        "order_status": normalize_text(order.status),
        "skladbot_request_number": normalize_text(raw_payload.get("skladbot_request_number")),
        "skladbot_request_id": normalize_text(raw_payload.get("skladbot_request_id")),
        "skladbot_status": normalize_text(raw_payload.get("skladbot_status")),
    }


def record_to_summary(record: dict) -> dict:
    return {
        "row_number": record.get("row_number"),
        "source_key": record_source_key(record),
        "client": normalize_text(record.get("client")),
        "product": normalize_text(record.get("product")),
        "status": normalize_text(record.get("status")),
        "source_sheet": normalize_text(record.get("source_sheet")),
    }


def incident_to_summary(incident: Incident) -> dict:
    return {
        "id": str(incident.id),
        "source": incident.source,
        "severity": incident.severity,
        "status": incident.status,
        "title": redact_secrets(incident.title),
        "external_ref": incident.external_ref or "",
    }


def parse_report_date(value) -> date:
    if value is None or value == "":
        return datetime.now(report_timezone()).date()
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = normalize_text(value)
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ReconciliationError(422, "Invalid reconciliation report_date")


def normalize_record_date(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = normalize_text(value)
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def format_date_ru(value: date) -> str:
    return value.strftime("%d.%m.%Y")
