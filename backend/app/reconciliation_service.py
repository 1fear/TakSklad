from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .db import SessionLocal
from .models import AuditLog, Incident, Order, OrderItem, PendingEvent
from .orders_service import COMPLETED_STATUSES, STATUS_ARCHIVED_NO_KIZ, STATUS_CANCELLED
from .redaction import redact_secrets
from .reports_service import report_timezone


RECONCILIATION_SOURCE = "daily_reconciliation"
TELEGRAM_NOTIFICATION_EVENT_TYPE = "telegram_notification"
CRITICAL_INCIDENT_TYPES = {"skladbot_gap"}


class ReconciliationError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def run_daily_reconciliation(*, db: Session | None = None, report_date=None, alert_chat_ids=None, detail_limit=20) -> dict:
    parsed_date = parse_report_date(report_date)
    if db is not None:
        return build_daily_reconciliation(db, parsed_date, alert_chat_ids=alert_chat_ids, detail_limit=detail_limit)
    with SessionLocal() as session:
        return build_daily_reconciliation(session, parsed_date, alert_chat_ids=alert_chat_ids, detail_limit=detail_limit)


def preview_daily_reconciliation(*, db: Session | None = None, report_date=None, detail_limit=20) -> dict:
    parsed_date = parse_report_date(report_date)
    if db is not None:
        return build_daily_reconciliation_preview(db, parsed_date, detail_limit=detail_limit)
    with SessionLocal() as session:
        return build_daily_reconciliation_preview(session, parsed_date, detail_limit=detail_limit)


def build_daily_reconciliation_preview(db: Session, report_date: date, *, detail_limit=20) -> dict:
    evaluation = evaluate_daily_reconciliation(db, report_date, detail_limit=detail_limit)
    return reconciliation_result(
        report_date,
        evaluation,
        incidents=[preview_incident_summary(report_date, spec) for spec in evaluation["incident_specs"]],
        alerts=[],
        mode="preview",
    )


def build_daily_reconciliation(db: Session, report_date: date, *, alert_chat_ids=None, detail_limit=20) -> dict:
    evaluation = evaluate_daily_reconciliation(db, report_date, detail_limit=detail_limit)
    incidents = [upsert_reconciliation_incident(db, report_date, spec) for spec in evaluation["incident_specs"]]
    alerts = queue_reconciliation_alerts(db, report_date, incidents, alert_chat_ids or [])
    db.commit()
    return reconciliation_result(
        report_date,
        evaluation,
        incidents=[incident_to_summary(incident) for incident in incidents],
        alerts=alerts,
        mode="execute",
    )


def evaluate_daily_reconciliation(db: Session, report_date: date, *, detail_limit=20) -> dict:
    detail_limit = max(1, min(int(detail_limit or 20), 100))
    orders = load_orders_for_date(db, report_date)
    items = visible_items(orders)
    skladbot_summary = summarize_skladbot_gaps(orders, detail_limit)
    db_summary = summarize_db_orders(orders, items)
    return {
        "db": db_summary,
        "skladbot": skladbot_summary,
        "incident_specs": build_incident_specs(report_date, skladbot_summary),
        "status": reconciliation_status(skladbot_summary),
    }


def reconciliation_result(report_date, evaluation, *, incidents, alerts, mode):
    return {
        "source": "postgres",
        "mode": mode,
        "status": evaluation["status"],
        "report_date": report_date.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db": evaluation["db"],
        "skladbot": evaluation["skladbot"],
        "incidents": incidents,
        "alerts": alerts,
    }


def preview_incident_summary(report_date, spec):
    return {
        "id": "",
        "source": RECONCILIATION_SOURCE,
        "severity": spec["severity"],
        "status": "candidate",
        "title": spec["title"],
        "external_ref": f"reconciliation:{report_date.isoformat()}:{spec['kind']}",
    }


def load_orders_for_date(db, report_date):
    return db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(Order.order_date == report_date)
        .order_by(Order.created_at.asc(), Order.id.asc())
    ).scalars().all()


def visible_items(orders):
    return [
        item
        for order in orders
        if normalize_text(order.status) not in {STATUS_CANCELLED, STATUS_ARCHIVED_NO_KIZ}
        for item in (order.items or [])
    ]


def summarize_skladbot_gaps(orders, detail_limit):
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
        "details": {"missing_request_orders": missing[:detail_limit], "problem_status_orders": problem[:detail_limit]},
    }


def summarize_db_orders(orders, items):
    active = [order for order in orders if normalize_text(order.status) not in COMPLETED_STATUSES and normalize_text(order.status) not in {STATUS_CANCELLED, STATUS_ARCHIVED_NO_KIZ}]
    completed = [order for order in orders if normalize_text(order.status) in COMPLETED_STATUSES]
    return {
        "orders": len(orders),
        "active_orders": len(active),
        "completed_orders": len(completed),
        "items": len(items),
        "active_items": sum(1 for item in items if normalize_text(item.order.status) not in COMPLETED_STATUSES),
        "planned_blocks": sum(int(item.quantity_blocks or 0) for item in items),
        "scanned_blocks": sum(int(item.scanned_blocks or 0) for item in items),
    }


def build_incident_specs(report_date, skladbot_summary):
    total = int(skladbot_summary.get("missing_request_orders") or 0) + int(skladbot_summary.get("problem_status_orders") or 0)
    if not total:
        return []
    return [{
        "kind": "skladbot_gap",
        "severity": "critical",
        "title": f"Daily reconciliation found SkladBot gaps for {report_date.isoformat()}",
        "message": "Some active orders have no usable SkladBot WH-R/status. Check stock/create incidents before warehouse picking.",
        "raw_payload": {
            "report_date": report_date.isoformat(),
            "skladbot": skladbot_summary,
            "next_action": "Open incidents, verify SkladBot request creation/matching, retry only after stock and data are correct.",
        },
    }]


def upsert_reconciliation_incident(db, report_date, spec):
    external_ref = f"reconciliation:{report_date.isoformat()}:{spec['kind']}"
    incident = db.execute(
        select(Incident).where(Incident.source == RECONCILIATION_SOURCE).where(Incident.external_ref == external_ref).limit(1)
    ).scalar_one_or_none()
    action = "reconciliation_incident_updated"
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
    incident.severity = spec["severity"]
    incident.status = "open"
    incident.title = spec["title"]
    incident.message = spec["message"]
    incident.raw_payload = spec["raw_payload"]
    incident.resolved_at = None
    db.add(AuditLog(action=action, entity_type="incident", entity_id=str(incident.id), payload={"external_ref": external_ref, "report_date": report_date.isoformat()}))
    return incident


def queue_reconciliation_alerts(db, report_date, incidents, chat_ids):
    alerts = []
    admin_routes = [normalize_text(value) for value in chat_ids if normalize_text(value)]
    if len(admin_routes) != 1 or not admin_routes[0].isdigit() or int(admin_routes[0]) <= 0:
        return alerts
    for incident in incidents:
        kind = normalize_text(incident.external_ref).split(":")[-1]
        if incident.severity != "critical" or kind not in CRITICAL_INCIDENT_TYPES:
            continue
        key = f"telegram:notification:v2:reconciliation:{report_date.isoformat()}:{kind}:{incident.id}"
        existing = db.execute(select(PendingEvent).where(PendingEvent.idempotency_key == key)).scalar_one_or_none()
        if existing is None:
            existing = PendingEvent(event_type=TELEGRAM_NOTIFICATION_EVENT_TYPE, status="pending", idempotency_key=key, payload={
                "kind": "daily_reconciliation_alert", "incident_id": str(incident.id),
                "source": RECONCILIATION_SOURCE, "report_date": report_date.isoformat(),
                "text": reconciliation_alert_text(report_date, incident),
            })
            db.add(existing)
            db.flush()
            status = "queued"
        else:
            status = "deduped"
        alerts.append({"incident_id": str(incident.id), "route_role": "admin", "idempotency_key": key, "status": status, "event_id": str(existing.id)})
    return alerts


def reconciliation_alert_text(report_date, incident):
    next_action = normalize_text((incident.raw_payload or {}).get("next_action")) or "Open TakSklad web panel and review the incident."
    return "\n".join(["TakSklad: требуется проверка сверки", "", f"Дата: {format_date_ru(report_date)}", f"Проблема: {incident.title}", "", f"Что сделать: {next_action}"])


def reconciliation_status(skladbot_summary):
    return "action_required" if int(skladbot_summary.get("missing_request_orders") or 0) + int(skladbot_summary.get("problem_status_orders") or 0) else "ok"


def order_to_summary(order):
    raw_payload = order.raw_payload or {}
    return {
        "order_id": str(order.id), "client": normalize_text(order.client), "order_status": normalize_text(order.status),
        "skladbot_request_number": normalize_text(raw_payload.get("skladbot_request_number")),
        "skladbot_request_id": normalize_text(raw_payload.get("skladbot_request_id")),
        "skladbot_status": normalize_text(raw_payload.get("skladbot_status")),
    }


def incident_to_summary(incident):
    return {"id": str(incident.id), "source": incident.source, "severity": incident.severity, "status": incident.status, "title": redact_secrets(incident.title), "external_ref": incident.external_ref or ""}


def parse_report_date(value):
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


def normalize_text(value):
    return str(value or "").strip()


def format_date_ru(value):
    return value.strftime("%d.%m.%Y")
