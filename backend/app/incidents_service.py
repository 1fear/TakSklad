import uuid
from datetime import date, datetime, time, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .models import AuditLog, Incident
from .redaction import redact_secrets


INCIDENT_STATUSES = ("open", "in_progress", "manual_review", "resolved", "ignored", "cancelled")
INCIDENT_SEVERITIES = ("info", "warning", "critical")
INCIDENT_ENTITY_TYPES = ("pending_event", "order", "order_item", "import", "scan_code", "external")
TERMINAL_INCIDENT_STATUSES = ("resolved", "ignored", "cancelled")


class IncidentApiError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def create_incident(db: Session, payload):
    source = normalize_required(payload.source, "source")
    title = normalize_required(payload.title, "title")
    status = validate_choice(payload.status or "open", INCIDENT_STATUSES, "status")
    severity = validate_choice(payload.severity or "warning", INCIDENT_SEVERITIES, "severity")
    entity_type = normalize_text(payload.entity_type)
    if entity_type:
        validate_choice(entity_type, INCIDENT_ENTITY_TYPES, "entity_type")
    incident = Incident(
        source=source,
        severity=severity,
        status=status,
        title=title,
        message=normalize_text(payload.message),
        entity_type=entity_type or None,
        entity_id=normalize_text(payload.entity_id) or None,
        pending_event_id=parse_uuid(payload.pending_event_id),
        order_id=parse_uuid(payload.order_id),
        order_item_id=parse_uuid(payload.order_item_id),
        import_id=parse_uuid(payload.import_id),
        scan_code_id=parse_uuid(payload.scan_code_id),
        external_ref=normalize_text(payload.external_ref) or None,
        raw_payload=payload.raw_payload or {},
        resolved_at=datetime.now(timezone.utc) if status in TERMINAL_INCIDENT_STATUSES else None,
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)
    return incident_to_read(incident)


def list_incidents(
    db: Session,
    *,
    status=None,
    severity=None,
    source=None,
    entity_type=None,
    date_from=None,
    date_to=None,
    limit=100,
):
    limit = max(1, min(int(limit or 100), 500))
    stmt = select(Incident)
    if normalize_text(status):
        stmt = stmt.where(Incident.status == validate_choice(status, INCIDENT_STATUSES, "status"))
    if normalize_text(severity):
        stmt = stmt.where(Incident.severity == validate_choice(severity, INCIDENT_SEVERITIES, "severity"))
    if normalize_text(source):
        stmt = stmt.where(Incident.source == normalize_text(source))
    if normalize_text(entity_type):
        stmt = stmt.where(Incident.entity_type == validate_choice(entity_type, INCIDENT_ENTITY_TYPES, "entity_type"))
    from_dt = parse_datetime_boundary(date_from, end_of_day=False)
    to_dt = parse_datetime_boundary(date_to, end_of_day=True)
    if from_dt is not None:
        stmt = stmt.where(Incident.created_at >= from_dt)
    if to_dt is not None:
        stmt = stmt.where(Incident.created_at <= to_dt)
    incidents = db.execute(
        stmt.order_by(desc(Incident.updated_at), desc(Incident.created_at), desc(Incident.id)).limit(limit)
    ).scalars().all()
    return {
        "items": [incident_to_read(incident) for incident in incidents],
        "summary": build_incident_summary(db),
    }


def get_incident(db: Session, incident_id):
    incident = db.get(Incident, parse_required_uuid(incident_id, "incident_id"))
    if incident is None:
        raise IncidentApiError(404, "Incident not found")
    return incident_to_read(incident)


def update_incident_status(db: Session, incident_id, payload):
    incident = db.get(Incident, parse_required_uuid(incident_id, "incident_id"))
    if incident is None:
        raise IncidentApiError(404, "Incident not found")
    new_status = validate_choice(payload.status, INCIDENT_STATUSES, "status")
    reason = normalize_required(payload.reason, "reason")
    actor = normalize_text(payload.actor) or "web"
    source = normalize_text(payload.source) or actor
    old_status = incident.status
    if old_status != new_status:
        incident.status = new_status
        incident.resolved_at = datetime.now(timezone.utc) if new_status in TERMINAL_INCIDENT_STATUSES else None
    audit = AuditLog(
        action="incident_status_changed",
        entity_type="incident",
        entity_id=str(incident.id),
        payload={
            "old_status": old_status,
            "new_status": new_status,
            "actor": actor,
            "source": source,
            "reason": reason,
            "incident_source": incident.source,
            "severity": incident.severity,
        },
    )
    db.add(audit)
    db.commit()
    db.refresh(incident)
    return incident_to_read(incident)


def build_incident_summary(db: Session):
    rows = db.execute(select(Incident.status, Incident.severity)).all()
    summary = {
        "total": len(rows),
        "by_status": {},
        "by_severity": {},
    }
    for status, severity in rows:
        summary["by_status"][status or ""] = summary["by_status"].get(status or "", 0) + 1
        summary["by_severity"][severity or ""] = summary["by_severity"].get(severity or "", 0) + 1
    return summary


def incident_to_read(incident: Incident):
    return {
        "id": str(incident.id),
        "source": incident.source,
        "severity": incident.severity,
        "status": incident.status,
        "title": redact_secrets(incident.title),
        "message": redact_secrets(incident.message or ""),
        "entity_type": incident.entity_type or "",
        "entity_id": incident.entity_id or "",
        "pending_event_id": str(incident.pending_event_id or ""),
        "order_id": str(incident.order_id or ""),
        "order_item_id": str(incident.order_item_id or ""),
        "import_id": str(incident.import_id or ""),
        "scan_code_id": str(incident.scan_code_id or ""),
        "external_ref": incident.external_ref or "",
        "raw_payload": redact_payload(incident.raw_payload or {}),
        "created_at": incident.created_at,
        "updated_at": incident.updated_at,
        "resolved_at": incident.resolved_at,
    }


def redact_payload(value):
    if isinstance(value, dict):
        return {
            key: "***" if is_secret_key(key) else redact_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value


def is_secret_key(key):
    normalized = str(key or "").casefold()
    return any(marker in normalized for marker in ("token", "password", "secret", "authorization"))


def normalize_text(value):
    return str(value or "").strip()


def normalize_required(value, field):
    text = normalize_text(value)
    if not text:
        raise IncidentApiError(422, f"{field} is required")
    return text


def validate_choice(value, allowed, field):
    text = normalize_required(value, field)
    if text not in allowed:
        raise IncidentApiError(422, f"Invalid {field}: {text}")
    return text


def parse_uuid(value):
    text = normalize_text(value)
    if not text:
        return None
    return parse_required_uuid(text, "uuid")


def parse_required_uuid(value, field):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise IncidentApiError(422, f"Invalid {field}") from exc


def parse_datetime_boundary(value, *, end_of_day):
    text = normalize_text(value)
    if not text:
        return None
    try:
        if len(text) == 10:
            parsed_date = date.fromisoformat(text)
            return datetime.combine(parsed_date, time.max if end_of_day else time.min)
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IncidentApiError(422, "Invalid date range") from exc
