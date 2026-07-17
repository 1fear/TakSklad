import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import bindparam, case, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import PendingEvent
from .observability_context import payload_with_correlation
from .redaction import redact_secrets


MAX_OUTBOX_PAYLOAD_BYTES = 2 * 1024 * 1024
SECRET_KEY_MARKERS = ("token", "password", "secret", "authorization", "credential")


class OutboxIdentityConflict(ValueError):
    def __init__(self, existing_event_id: object, reason: str):
        self.existing_event_id = str(existing_event_id or "")
        self.reason = str(reason or "event_identity_conflict")
        super().__init__(f"outbox idempotency identity conflict: {self.reason}")


def outbox_event_identity_conflict_reason(
    event: PendingEvent,
    *,
    event_type: str,
    action: str,
    aggregate_type: str,
    aggregate_id: str,
    idempotency_key: str,
) -> str:
    expected = {
        "event_type": event_type,
        "action": action,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "idempotency_key": idempotency_key,
    }
    actual = {
        "event_type": event.event_type,
        "action": event.action,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "idempotency_key": event.idempotency_key,
    }
    for field, expected_value in expected.items():
        if str(actual.get(field) or "").strip() != str(expected_value or "").strip():
            return f"{field}_mismatch"

    payload = event.payload if isinstance(event.payload, dict) else {}
    payload_expected = {
        "action": action,
        "entity_type": aggregate_type,
        "entity_id": aggregate_id,
        "idempotency_key": idempotency_key,
    }
    for field, expected_value in payload_expected.items():
        if str(payload.get(field) or "").strip() != str(expected_value or "").strip():
            return f"payload_{field}_mismatch"
    return ""


def require_exact_outbox_event_identity(event: PendingEvent, values: dict) -> PendingEvent:
    reason = outbox_event_identity_conflict_reason(
        event,
        event_type=values["event_type"],
        action=values["action"],
        aggregate_type=values["aggregate_type"],
        aggregate_id=values["aggregate_id"],
        idempotency_key=values["idempotency_key"],
    )
    if reason:
        raise OutboxIdentityConflict(event.id, reason)
    return event


def queue_outbox_event(
    db: Session,
    *,
    event_type: str,
    action: str,
    aggregate_type: str,
    aggregate_id: str,
    idempotency_key: str,
    payload: dict | None = None,
    last_error: str | None = None,
    strict_identity: bool = False,
) -> PendingEvent:
    values = outbox_values(
        event_type=event_type,
        action=action,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        idempotency_key=idempotency_key,
        payload=payload,
        last_error=last_error,
    )
    event_type = values["event_type"]
    action = values["action"]
    aggregate_type = values["aggregate_type"]
    aggregate_id = values["aggregate_id"]
    idempotency_key = values["idempotency_key"]

    dialect_name = db.get_bind().dialect.name
    if dialect_name in {"postgresql", "sqlite"}:
        insert_factory = postgresql_insert if dialect_name == "postgresql" else sqlite_insert
        statement = (
            insert_factory(PendingEvent)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[PendingEvent.idempotency_key])
            .returning(PendingEvent)
        )
        with db.no_autoflush:
            inserted = db.execute(statement).scalar_one_or_none()
        if inserted is not None:
            return inserted
        existing = db.execute(
            select(PendingEvent).where(PendingEvent.idempotency_key == idempotency_key)
        ).scalar_one()
        if strict_identity:
            return require_exact_outbox_event_identity(existing, values)
        existing.action = existing.action or action
        existing.aggregate_type = existing.aggregate_type or aggregate_type
        existing.aggregate_id = existing.aggregate_id or aggregate_id
        return existing

    existing = db.execute(
        select(PendingEvent).where(PendingEvent.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing is not None:
        if strict_identity:
            return require_exact_outbox_event_identity(existing, values)
        existing.action = existing.action or action
        existing.aggregate_type = existing.aggregate_type or aggregate_type
        existing.aggregate_id = existing.aggregate_id or aggregate_id
        return existing
    event = PendingEvent(**values)
    db.add(event)
    db.flush()
    return event


def outbox_values(
    *, event_type, action, aggregate_type, aggregate_id, idempotency_key, payload=None, last_error=None,
):
    event_type = required_identity(event_type, "event_type", 80)
    action = required_identity(action, "action", 80)
    aggregate_type = required_identity(aggregate_type, "aggregate_type", 80)
    aggregate_id = required_identity(aggregate_id, "aggregate_id", 180)
    idempotency_key = required_identity(idempotency_key, "idempotency_key", 180)
    event_payload = sanitize_outbox_payload(payload_with_correlation({
        **(payload or {}),
        "action": action,
        "entity_type": aggregate_type,
        "entity_id": aggregate_id,
        "idempotency_key": idempotency_key,
    }))
    ensure_payload_size(event_payload)

    return {
        "id": uuid.uuid4(),
        "event_type": event_type,
        "action": action,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "idempotency_key": idempotency_key,
        "status": "pending",
        "attempts": 0,
        "payload": event_payload,
        "last_error": redact_secrets(last_error or ""),
    }


def queue_coalesced_postgres_outbox_event(
    db: Session,
    *,
    event_type: str,
    action: str,
    aggregate_type: str,
    aggregate_id: str,
    idempotency_key: str,
    payload: dict | None = None,
    last_error: str | None = None,
) -> tuple[PendingEvent, bool]:
    if db.get_bind().dialect.name != "postgresql":
        raise ValueError("coalesced PostgreSQL outbox path requires PostgreSQL")
    values = outbox_values(
        event_type=event_type,
        action=action,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        idempotency_key=idempotency_key,
        payload=payload,
        last_error=last_error,
    )
    candidate_filter = or_(
        PendingEvent.idempotency_key == values["idempotency_key"],
        (PendingEvent.event_type == values["event_type"])
        & (PendingEvent.action == values["action"])
        & (PendingEvent.aggregate_type == values["aggregate_type"])
        & (PendingEvent.aggregate_id == values["aggregate_id"])
        & PendingEvent.status.in_(("pending", "failed")),
    )
    candidate_exists = select(PendingEvent.id).where(candidate_filter).limit(1).exists()
    table = PendingEvent.__table__
    columns = tuple(values)
    source = select(*(
        bindparam(f"coalesced_{name}", value, type_=table.c[name].type)
        for name, value in values.items()
    )).where(~candidate_exists)
    statement = (
        postgresql_insert(PendingEvent)
        .from_select(columns, source)
        .on_conflict_do_nothing(index_elements=[PendingEvent.idempotency_key])
        .returning(PendingEvent)
    )
    with db.no_autoflush:
        inserted = db.execute(statement).scalar_one_or_none()
    if inserted is not None:
        return inserted, True
    existing = db.execute(
        select(PendingEvent)
        .where(candidate_filter)
        .order_by(
            case((PendingEvent.idempotency_key == values["idempotency_key"], 0), else_=1),
            PendingEvent.created_at,
            PendingEvent.id,
        )
        .limit(1)
    ).scalar_one()
    existing.action = existing.action or values["action"]
    existing.aggregate_type = existing.aggregate_type or values["aggregate_type"]
    existing.aggregate_id = existing.aggregate_id or values["aggregate_id"]
    return existing, False


def find_active_outbox_event(
    db: Session,
    *,
    event_type: str,
    action: str,
    aggregate_type: str,
    aggregate_id: str,
) -> PendingEvent | None:
    return db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == event_type)
        .where(PendingEvent.action == action)
        .where(PendingEvent.aggregate_type == aggregate_type)
        .where(PendingEvent.aggregate_id == aggregate_id)
        .where(PendingEvent.status.in_(("pending", "failed")))
        .order_by(PendingEvent.created_at, PendingEvent.id)
        .limit(1)
    ).scalar_one_or_none()


def reactivate_outbox_event(event: PendingEvent, payload: dict, last_error: str | None = None) -> PendingEvent:
    event.payload = sanitize_outbox_payload(payload_with_correlation({**(event.payload or {}), **payload}))
    ensure_payload_size(event.payload)
    event.action = event.action or str(event.payload.get("action") or "") or None
    event.aggregate_type = event.aggregate_type or str(event.payload.get("entity_type") or "") or None
    event.aggregate_id = event.aggregate_id or str(event.payload.get("entity_id") or "") or None
    event.status = "pending"
    event.available_at = datetime.now(timezone.utc)
    event.lease_owner = None
    event.lease_expires_at = None
    event.completed_at = None
    event.last_error = redact_secrets(last_error or "")
    return event


def outbox_fault(_stage: str, _producer: str) -> None:
    """No-op injection seam used by synthetic transaction fault tests."""


def sanitize_outbox_payload(value):
    if isinstance(value, dict):
        return {
            str(key): "***" if is_secret_key(key) else sanitize_outbox_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_outbox_payload(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_outbox_payload(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value


def is_secret_key(key) -> bool:
    normalized = str(key or "").casefold()
    return any(marker in normalized for marker in SECRET_KEY_MARKERS)


def ensure_payload_size(payload) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    size_bytes = len(encoded.encode("utf-8"))
    if size_bytes > MAX_OUTBOX_PAYLOAD_BYTES:
        raise ValueError(f"outbox payload exceeds {MAX_OUTBOX_PAYLOAD_BYTES} bytes")


def required_identity(value, field: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"outbox {field} is required")
    if len(normalized) > maximum:
        raise ValueError(f"outbox {field} exceeds {maximum} characters")
    return normalized
