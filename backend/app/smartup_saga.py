import hashlib
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Iterable
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import Order, PendingEvent, SmartupFulfillment, SmartupFulfillmentOrder
from .outbox_service import queue_outbox_event, sanitize_outbox_payload
from .redaction import redact_secrets


SMARTUP_DEAL_SAGA_EVENT_TYPE = "smartup_deal_saga"
SMARTUP_DEAL_SAGA_ACTION = "smartup_change_status"
SMARTUP_SAGA_MODES = {"disabled", "shadow", "enforced"}
REMOTE_CONFIRMED_STATES = {"remote_confirmed", "skladbot_pending", "skladbot_queued"}
RECONCILE_STATES = {"remote_write_started", "remote_failed"}

FULFILLMENT_STATES = {
    "local_ready",
    "smartup_write_started",
    "smartup_confirmed",
    "skladbot_create_queued",
    "skladbot_post_started",
    "skladbot_created",
    "smartup_ambiguous",
    "skladbot_ambiguous",
    "blocked_validation",
    "blocked_stock",
    "payload_mismatch",
    "manual_review",
    "cancelled",
}
FULFILLMENT_TRANSITIONS = {
    "local_ready": {"smartup_write_started", "blocked_validation", "payload_mismatch", "cancelled"},
    "smartup_write_started": {"smartup_confirmed", "smartup_ambiguous", "manual_review"},
    "smartup_ambiguous": {"smartup_write_started", "smartup_confirmed", "manual_review", "cancelled"},
    "smartup_confirmed": {
        "skladbot_create_queued",
        "blocked_validation",
        "blocked_stock",
        "manual_review",
    },
    "skladbot_create_queued": {
        "skladbot_post_started",
        "skladbot_ambiguous",
        "blocked_validation",
        "blocked_stock",
        "manual_review",
    },
    "skladbot_post_started": {"skladbot_create_queued", "skladbot_created", "skladbot_ambiguous", "blocked_stock", "manual_review"},
    "skladbot_ambiguous": {"skladbot_create_queued", "skladbot_post_started", "skladbot_created", "manual_review", "cancelled"},
    "blocked_validation": {"local_ready", "manual_review", "cancelled"},
    "blocked_stock": {"skladbot_create_queued", "manual_review", "cancelled"},
    "payload_mismatch": {"manual_review", "cancelled"},
    "manual_review": {
        "local_ready",
        "smartup_confirmed",
        "skladbot_create_queued",
        "skladbot_created",
        "cancelled",
    },
    "skladbot_created": set(),
    "cancelled": set(),
}
LEGACY_SAGA_STATES = {
    "local_ready": "intent_persisted",
    "smartup_write_started": "remote_write_started",
    "smartup_confirmed": "remote_confirmed",
    "skladbot_create_queued": "skladbot_queued",
    "skladbot_post_started": "skladbot_pending",
    "skladbot_created": "skladbot_queued",
}


class FulfillmentTransitionError(ValueError):
    """Raised when a durable fulfillment state transition is not allowed."""


def fulfillment_workflow_key(
    *,
    source_scope: str,
    deal_id: str,
    request_type: str = "shipment",
    revision: int = 1,
) -> str:
    """Build a stable business key independent from files, slots and imports."""
    normalized_scope = required_fulfillment_identity(source_scope, "source_scope").casefold()
    normalized_deal_id = required_fulfillment_identity(deal_id, "deal_id")
    normalized_request_type = required_fulfillment_identity(request_type, "request_type").casefold()
    if int(revision) <= 0:
        raise ValueError("fulfillment revision must be positive")
    identity = "|".join((normalized_scope, normalized_deal_id, normalized_request_type, str(int(revision))))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"smartup:fulfillment:v1:{digest}"


def fulfillment_payload_hash(payload: dict) -> str:
    """Hash a canonical JSON representation of the immutable request payload."""
    encoded = json.dumps(
        payload or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=canonical_json_value,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def get_or_create_fulfillment(
    db: Session,
    *,
    source_scope: str,
    deal_id: str,
    request_type: str = "shipment",
    revision: int = 1,
    target_status: str,
    payload: dict,
    canonical_import_id: UUID | None = None,
    legacy_event: PendingEvent | None = None,
) -> SmartupFulfillment:
    """Resolve a durable workflow without committing the caller transaction."""
    normalized_scope = required_fulfillment_identity(source_scope, "source_scope").casefold()
    normalized_deal_id = required_fulfillment_identity(deal_id, "deal_id")
    normalized_request_type = required_fulfillment_identity(request_type, "request_type").casefold()
    normalized_target_status = required_fulfillment_identity(target_status, "target_status")
    workflow_key = fulfillment_workflow_key(
        source_scope=normalized_scope,
        deal_id=normalized_deal_id,
        request_type=normalized_request_type,
        revision=revision,
    )
    immutable_payload = {
        "target_status": normalized_target_status,
        "payload": dict(payload or {}),
    }
    payload_digest = fulfillment_payload_hash(immutable_payload)
    fulfillment = None
    dialect_name = db.get_bind().dialect.name
    values = {
        "id": uuid.uuid4(),
        "workflow_key": workflow_key,
        "source_scope": normalized_scope,
        "deal_id": normalized_deal_id,
        "request_type": normalized_request_type,
        "revision": int(revision),
        "target_status": normalized_target_status,
        "payload_hash": payload_digest,
        "state": "local_ready",
        "retry_attempts": 0,
        "reconciliation_attempts": 0,
        "canonical_import_id": canonical_import_id,
        "legacy_saga_event_id": legacy_event.id if legacy_event is not None else None,
        "raw_payload": sanitize_outbox_payload(immutable_payload),
    }
    identity_query = select(SmartupFulfillment).where(
        SmartupFulfillment.source_scope == normalized_scope,
        SmartupFulfillment.deal_id == normalized_deal_id,
        SmartupFulfillment.request_type == normalized_request_type,
        SmartupFulfillment.revision == int(revision),
    )
    if dialect_name in {"postgresql", "sqlite"}:
        insert_factory = postgresql_insert if dialect_name == "postgresql" else sqlite_insert
        statement = (
            insert_factory(SmartupFulfillment)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    SmartupFulfillment.source_scope,
                    SmartupFulfillment.deal_id,
                    SmartupFulfillment.request_type,
                    SmartupFulfillment.revision,
                ]
            )
            .returning(SmartupFulfillment)
        )
        with db.no_autoflush:
            fulfillment = db.execute(statement).scalar_one_or_none()
    else:
        fulfillment = db.execute(identity_query).scalar_one_or_none()
    if fulfillment is None:
        fulfillment = db.execute(identity_query).scalar_one_or_none()
    if fulfillment is None:
        fulfillment = SmartupFulfillment(**values)
        db.add(fulfillment)
        db.flush()
    elif fulfillment.payload_hash != payload_digest:
        previous_state = fulfillment.state
        existing_payload = dict(fulfillment.raw_payload or {})
        existing_payload["payload_mismatch_from_state"] = previous_state
        fulfillment.raw_payload = existing_payload
        fulfillment.state = "payload_mismatch"
        fulfillment.state_changed_at = datetime.now(timezone.utc)
        fulfillment.last_error = "payload hash mismatch for existing fulfillment revision"
    else:
        if fulfillment.canonical_import_id is None and canonical_import_id is not None:
            fulfillment.canonical_import_id = canonical_import_id
        if fulfillment.legacy_saga_event_id is None and legacy_event is not None:
            fulfillment.legacy_saga_event_id = legacy_event.id
    if legacy_event is not None:
        sync_legacy_saga_event(db, fulfillment, legacy_event)
    return fulfillment


def link_fulfillment_orders(
    db: Session,
    fulfillment: SmartupFulfillment,
    order_ids: Iterable[UUID],
) -> list[SmartupFulfillmentOrder]:
    """Idempotently attach one or more canonical TakSklad orders."""
    normalized_order_ids = sorted({UUID(str(order_id)) for order_id in order_ids}, key=str)
    if not normalized_order_ids:
        return []
    dialect_name = db.get_bind().dialect.name
    if dialect_name in {"postgresql", "sqlite"}:
        insert_factory = postgresql_insert if dialect_name == "postgresql" else sqlite_insert
        rows = [
            {
                "id": uuid.uuid4(),
                "fulfillment_id": fulfillment.id,
                "order_id": order_id,
                "state": "pending",
            }
            for order_id in normalized_order_ids
        ]
        db.execute(
            insert_factory(SmartupFulfillmentOrder)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=[
                    SmartupFulfillmentOrder.fulfillment_id,
                    SmartupFulfillmentOrder.order_id,
                ]
            )
        )
    existing = db.execute(
        select(SmartupFulfillmentOrder)
        .where(SmartupFulfillmentOrder.fulfillment_id == fulfillment.id)
        .where(SmartupFulfillmentOrder.order_id.in_(normalized_order_ids))
    ).scalars().all()
    by_order_id = {item.order_id: item for item in existing}
    if len(by_order_id) != len(normalized_order_ids):
        for order_id in normalized_order_ids:
            if order_id not in by_order_id:
                link = SmartupFulfillmentOrder(fulfillment=fulfillment, order_id=order_id, state="pending")
                db.add(link)
                by_order_id[order_id] = link
    db.flush()
    orders = db.execute(select(Order).where(Order.id.in_(normalized_order_ids))).scalars().all()
    for order in orders:
        raw_payload = dict(order.raw_payload or {})
        raw_payload["smartup_fulfillment_key"] = fulfillment.workflow_key
        raw_payload["smartup_fulfillment_revision"] = int(fulfillment.revision or 1)
        order.raw_payload = raw_payload
    return [by_order_id[order_id] for order_id in normalized_order_ids]


def fulfillment_order_ids(db: Session, fulfillment: SmartupFulfillment) -> list[UUID]:
    return list(db.execute(
        select(SmartupFulfillmentOrder.order_id)
        .where(SmartupFulfillmentOrder.fulfillment_id == fulfillment.id)
        .order_by(SmartupFulfillmentOrder.order_id)
    ).scalars().all())


def link_fulfillment_order_event(
    db: Session,
    fulfillment: SmartupFulfillment,
    order_id: UUID,
    *,
    create_event: PendingEvent | None = None,
    remote_request_id: str | None = None,
) -> SmartupFulfillmentOrder:
    """Attach local create intent and/or upstream request to one canonical order."""
    link = db.execute(
        select(SmartupFulfillmentOrder)
        .where(SmartupFulfillmentOrder.fulfillment_id == fulfillment.id)
        .where(SmartupFulfillmentOrder.order_id == UUID(str(order_id)))
    ).scalar_one_or_none()
    if link is None:
        link = link_fulfillment_orders(db, fulfillment, [order_id])[0]
    if create_event is not None:
        link.skladbot_event_id = create_event.id
        if link.state == "pending":
            link.state = "create_queued"
    if remote_request_id is not None:
        link.remote_request_id = required_fulfillment_identity(remote_request_id, "remote_request_id")
    return link


def transition_fulfillment_order(
    db: Session,
    link: SmartupFulfillmentOrder,
    target_state: str,
    *,
    error: str = "",
    remote_request_id: str = "",
) -> SmartupFulfillment:
    """Update one SkladBot request and derive the aggregate workflow state."""
    fulfillment = db.execute(
        select(SmartupFulfillment)
        .where(SmartupFulfillment.id == link.fulfillment_id)
        .with_for_update()
    ).scalar_one_or_none()
    if fulfillment is None:
        raise FulfillmentTransitionError("fulfillment mapping points to a missing workflow")
    db.refresh(link)
    target = normalize_text(target_state).casefold()
    supported = {"pending", "create_queued", "post_started", "created", "ambiguous", "blocked_stock", "manual_review"}
    if target not in supported:
        raise FulfillmentTransitionError(f"unsupported fulfillment order state: {target}")
    link.state = target
    link.last_error = redact_secrets(error or "") or None
    if remote_request_id:
        link.remote_request_id = required_fulfillment_identity(remote_request_id, "remote_request_id")
    db.flush([link])

    states = list(db.execute(
        select(SmartupFulfillmentOrder.state)
        .where(SmartupFulfillmentOrder.fulfillment_id == fulfillment.id)
    ).scalars().all())
    if states and all(state == "created" for state in states):
        aggregate = "skladbot_created"
    elif "manual_review" in states:
        aggregate = "manual_review"
    elif "ambiguous" in states:
        aggregate = "skladbot_ambiguous"
    elif "blocked_stock" in states:
        aggregate = "blocked_stock"
    elif "post_started" in states:
        aggregate = "skladbot_post_started"
    else:
        aggregate = "skladbot_create_queued"
    allowed_current = {
        "smartup_confirmed",
        "skladbot_create_queued",
        "skladbot_post_started",
        "skladbot_created",
        "skladbot_ambiguous",
        "blocked_stock",
        "manual_review",
    }
    if fulfillment.state not in allowed_current:
        raise FulfillmentTransitionError(
            f"cannot derive SkladBot aggregate from fulfillment state {fulfillment.state}"
        )
    fulfillment.state = aggregate
    fulfillment.state_changed_at = datetime.now(timezone.utc)
    fulfillment.last_error = redact_secrets(error or "") or None
    return fulfillment


def transition_fulfillment(
    db: Session | None,
    fulfillment: SmartupFulfillment,
    target_state: str,
    *,
    expected_states: Iterable[str] | None = None,
    error: str | None = None,
    retry_at: datetime | None = None,
    increment_retry: bool = False,
    increment_reconciliation: bool = False,
) -> SmartupFulfillment:
    """Apply an explicit workflow transition without committing."""
    del db  # The session is accepted to keep the service API transaction-oriented.
    normalized_target = normalize_text(target_state).casefold()
    if normalized_target not in FULFILLMENT_STATES:
        raise FulfillmentTransitionError(f"unsupported fulfillment state: {normalized_target}")
    current_state = normalize_text(fulfillment.state).casefold()
    if expected_states is not None:
        normalized_expected = {normalize_text(value).casefold() for value in expected_states}
        if current_state not in normalized_expected:
            raise FulfillmentTransitionError(
                f"fulfillment state {current_state} is not one of expected states {sorted(normalized_expected)}"
            )
    if normalized_target != current_state and normalized_target not in FULFILLMENT_TRANSITIONS.get(current_state, set()):
        raise FulfillmentTransitionError(f"invalid fulfillment transition: {current_state} -> {normalized_target}")
    fulfillment.state = normalized_target
    fulfillment.state_changed_at = datetime.now(timezone.utc)
    if increment_retry:
        fulfillment.retry_attempts = int(fulfillment.retry_attempts or 0) + 1
    if increment_reconciliation:
        fulfillment.reconciliation_attempts = int(fulfillment.reconciliation_attempts or 0) + 1
    if retry_at is not None:
        fulfillment.available_at = retry_at
    fulfillment.last_error = redact_secrets(error or "") or None
    return fulfillment


def fulfillment_retry_at(
    retry_attempts: int,
    *,
    now: datetime | None = None,
    base_seconds: int = 30,
    maximum_seconds: int = 1800,
) -> datetime:
    if retry_attempts < 0 or base_seconds <= 0 or maximum_seconds <= 0:
        raise ValueError("fulfillment backoff values must be positive")
    current_time = now or datetime.now(timezone.utc)
    delay_seconds = min(base_seconds * (2 ** retry_attempts), maximum_seconds)
    return current_time + timedelta(seconds=delay_seconds)


def sync_legacy_saga_event(
    db: Session | None,
    fulfillment: SmartupFulfillment,
    event: PendingEvent,
) -> PendingEvent:
    """Expose durable state through the existing PendingEvent during migration."""
    fulfillment.legacy_saga_event_id = event.id
    payload = dict(event.payload or {})
    payload["fulfillment_id"] = str(fulfillment.id)
    payload["fulfillment_workflow_key"] = fulfillment.workflow_key
    payload["fulfillment_state"] = fulfillment.state
    order_ids = fulfillment_order_ids(db, fulfillment) if db is not None else [item.order_id for item in fulfillment.order_links]
    payload["canonical_order_ids"] = sorted(str(order_id) for order_id in order_ids)
    legacy_state = LEGACY_SAGA_STATES.get(fulfillment.state)
    if legacy_state:
        payload["saga_state"] = legacy_state
    event.payload = payload
    return event


def required_fulfillment_identity(value, field: str) -> str:
    normalized = normalize_text(value)
    if not normalized:
        raise ValueError(f"fulfillment {field} is required")
    return normalized


def canonical_json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"unsupported fulfillment payload value: {type(value).__name__}")


def smartup_saga_fault(_boundary: str, _deal_id: str) -> None:
    """No-op seam used only by synthetic boundary fault tests."""


def normalize_saga_mode(value) -> str:
    mode = str(value or "disabled").strip().lower()
    return mode if mode in SMARTUP_SAGA_MODES else "disabled"


def saga_idempotency_key(
    *,
    export_date: date,
    slot_label: str,
    target_delivery_date: date | None,
    deal_id: str,
    target_status: str,
) -> str:
    identity = "|".join([
        export_date.isoformat(),
        normalize_text(slot_label),
        target_delivery_date.isoformat() if target_delivery_date else "",
        normalize_text(deal_id),
        normalize_text(target_status),
    ])
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"smartup:deal_saga:v1:{digest}"


def prepare_deal_sagas(
    db: Session,
    imports: list[dict],
    *,
    source_batch_key: str,
    target_status: str,
    export_date: date,
    slot_label: str,
    target_delivery_date: date | None,
    mode: str,
    source_scope: str = "smartup:default",
) -> list[PendingEvent]:
    mode = normalize_saga_mode(mode)
    events = []
    for item in imports:
        import_id = normalize_text(item.get("import_id"))
        for deal_id in sorted({normalize_text(value) for value in item.get("deal_ids") or [] if normalize_text(value)}):
            order_ids = sorted({
                UUID(normalize_text(value))
                for value in item.get("resolved_order_ids") or []
                if normalize_text(value)
            }, key=str)
            fulfillment = None
            if mode == "shadow":
                key = saga_idempotency_key(
                    export_date=export_date,
                    slot_label=slot_label,
                    target_delivery_date=target_delivery_date,
                    deal_id=deal_id,
                    target_status=target_status,
                )
            else:
                fulfillment = get_or_create_fulfillment(
                    db,
                    source_scope=source_scope,
                    deal_id=deal_id,
                    request_type="shipment",
                    revision=1,
                    target_status=target_status,
                    payload={
                        "deal_id": deal_id,
                        "target_status": normalize_text(target_status),
                        "delivery_date": normalize_text(item.get("delivery_date")),
                        "order_ids": [str(order_id) for order_id in order_ids],
                    },
                    canonical_import_id=UUID(import_id) if import_id else None,
                )
                if fulfillment.state == "payload_mismatch":
                    db.flush()
                    db.commit()
                    raise FulfillmentTransitionError(
                        f"fulfillment payload mismatch requires a new revision: {fulfillment.workflow_key}"
                    )
                link_fulfillment_orders(db, fulfillment, order_ids)
                key = fulfillment.workflow_key
            event = queue_outbox_event(
                db,
                event_type=SMARTUP_DEAL_SAGA_EVENT_TYPE,
                action=SMARTUP_DEAL_SAGA_ACTION,
                aggregate_type="smartup_deal",
                aggregate_id=deal_id,
                idempotency_key=key,
                payload={
                    "version": 1,
                    "mode": mode,
                    "saga_state": "shadow_intent" if mode == "shadow" else "intent_persisted",
                    "deal_id": deal_id,
                    "import_id": import_id,
                    "target_status": normalize_text(target_status),
                    "source_batch_key_hash": hashlib.sha256(source_batch_key.encode("utf-8")).hexdigest(),
                    "export_date": export_date.isoformat(),
                    "slot": normalize_text(slot_label),
                    "target_delivery_date": target_delivery_date.isoformat() if target_delivery_date else "",
                    "import_snapshot": saga_import_snapshot(item),
                    "remote_attempts": 0,
                    "remote_reconciliations": 0,
                    "remote_result": {},
                    "skladbot_event_key": "",
                },
            )
            if fulfillment is not None:
                sync_legacy_saga_event(db, fulfillment, event)
            events.append(event)
    smartup_saga_fault("import_to_intent", "batch")
    db.commit()
    return events


def execute_status_sagas(
    db: Session,
    client,
    events: list[PendingEvent],
    *,
    target_status: str,
    successful_ids,
    failed_ids,
) -> dict:
    target_status = normalize_text(target_status)
    active_events = [refresh_event(db, event) for event in events]
    reconcile = [event for event in active_events if saga_state(event) in RECONCILE_STATES]
    if reconcile:
        statuses = client.get_deal_statuses([event.aggregate_id for event in reconcile])
        now = datetime.now(timezone.utc).isoformat()
        for event in reconcile:
            payload = dict(event.payload or {})
            fulfillment = fulfillment_for_event(db, event)
            remote_status = normalize_text(statuses.get(event.aggregate_id))
            payload["remote_reconciliations"] = int(payload.get("remote_reconciliations") or 0) + 1
            payload["last_reconciled_at"] = now
            payload["last_reconciled_status"] = remote_status
            if remote_status == target_status:
                payload["saga_state"] = "remote_confirmed"
                payload["remote_result"] = {"confirmed": True, "source": "read_reconciliation"}
                event.status = "pending"
                if fulfillment is not None:
                    transition_fulfillment(
                        db,
                        fulfillment,
                        "smartup_confirmed",
                        expected_states={"smartup_write_started", "smartup_ambiguous", "smartup_confirmed"},
                        increment_reconciliation=True,
                    )
            elif remote_status:
                payload["saga_state"] = "intent_persisted"
                payload["remote_result"] = {
                    "confirmed": False,
                    "source": "read_reconciliation",
                    "observed_status": remote_status,
                }
                event.status = "pending"
                if fulfillment is not None:
                    transition_fulfillment(
                        db,
                        fulfillment,
                        "smartup_ambiguous",
                        expected_states={"smartup_write_started", "smartup_ambiguous"},
                        increment_reconciliation=True,
                    )
            else:
                payload["saga_state"] = "remote_failed"
                payload["remote_result"] = {
                    "confirmed": False,
                    "source": "read_reconciliation",
                    "error": "remote status could not be reconciled",
                }
                event.status = "failed"
                event.last_error = "remote status could not be reconciled"
                if fulfillment is not None:
                    transition_fulfillment(
                        db,
                        fulfillment,
                        "smartup_ambiguous",
                        expected_states={"smartup_write_started", "smartup_ambiguous"},
                        error="remote status could not be reconciled",
                        retry_at=fulfillment_retry_at(int(fulfillment.retry_attempts or 0)),
                        increment_reconciliation=True,
                    )
            event.payload = payload
            if fulfillment is not None:
                sync_legacy_saga_event(db, fulfillment, event)
        db.commit()

    active_events = [refresh_event(db, event) for event in active_events]
    to_change = [event for event in active_events if saga_state(event) == "intent_persisted"]
    if not to_change:
        confirmed = sorted(
            event.aggregate_id for event in active_events if saga_state(event) in REMOTE_CONFIRMED_STATES
        )
        unresolved = sorted(
            event.aggregate_id for event in active_events if saga_state(event) not in REMOTE_CONFIRMED_STATES
        )
        return {
            "successes": [{"deal_id": deal_id} for deal_id in confirmed],
            "errors": [
                {"deal_id": deal_id, "message": "remote status could not be reconciled"}
                for deal_id in unresolved
            ],
            "submitted": 0,
            "deal_ids": sorted(confirmed + unresolved),
            "successful_deal_ids": confirmed,
            "failed_deal_ids": unresolved,
            "status": target_status,
            "reconciled": not unresolved,
        }

    now = datetime.now(timezone.utc).isoformat()
    claimed_events = []
    for event in sorted(to_change, key=lambda item: (item.aggregate_id, str(item.id))):
        fulfillment = fulfillment_for_event(db, event)
        if fulfillment is not None:
            fulfillment = db.execute(
                select(SmartupFulfillment)
                .where(SmartupFulfillment.id == fulfillment.id)
                .with_for_update()
            ).scalar_one()
            db.refresh(event)
            if saga_state(event) != "intent_persisted" or fulfillment.state not in {
                "local_ready", "smartup_ambiguous"
            }:
                continue
        payload = dict(event.payload or {})
        payload["saga_state"] = "remote_write_started"
        payload["remote_attempts"] = int(payload.get("remote_attempts") or 0) + 1
        payload["remote_write_started_at"] = now
        event.payload = payload
        event.status = "processing"
        if fulfillment is not None and fulfillment.state != "smartup_write_started":
            transition_fulfillment(
                db,
                fulfillment,
                "smartup_write_started",
                expected_states={"local_ready", "smartup_ambiguous"},
                increment_retry=fulfillment.state == "smartup_ambiguous",
            )
            sync_legacy_saga_event(db, fulfillment, event)
        claimed_events.append(event)
    db.commit()
    to_change = claimed_events
    if not to_change:
        return execute_status_sagas(
            db,
            client,
            events,
            target_status=target_status,
            successful_ids=successful_ids,
            failed_ids=failed_ids,
        )
    smartup_saga_fault("intent_to_smartup", "batch")

    deal_ids = [event.aggregate_id for event in to_change]
    response = client.change_status(deal_ids, target_status)
    smartup_saga_fault("smartup_to_local", "batch")
    succeeded = set(successful_ids(response, deal_ids))
    failed = set(failed_ids(response))
    now = datetime.now(timezone.utc).isoformat()
    for event in to_change:
        confirmed = event.aggregate_id in succeeded and event.aggregate_id not in failed
        payload = dict(event.payload or {})
        payload["saga_state"] = "remote_confirmed" if confirmed else "remote_failed"
        payload["remote_recorded_at"] = now
        payload["remote_result"] = {
            "confirmed": confirmed,
            "error": "" if confirmed else remote_error_for_deal(response, event.aggregate_id),
        }
        event.payload = payload
        event.status = "pending" if confirmed else "failed"
        event.last_error = "" if confirmed else payload["remote_result"]["error"]
        fulfillment = fulfillment_for_event(db, event)
        if fulfillment is not None:
            transition_fulfillment(
                db,
                fulfillment,
                "smartup_confirmed" if confirmed else "smartup_ambiguous",
                expected_states={"smartup_write_started"},
                error="" if confirmed else payload["remote_result"]["error"],
                retry_at=None if confirmed else fulfillment_retry_at(int(fulfillment.retry_attempts or 0)),
            )
            sync_legacy_saga_event(db, fulfillment, event)
    db.commit()
    return response


def record_shadow_results(db: Session, events: list[PendingEvent], response: dict, successful_ids, failed_ids) -> None:
    deal_ids = [event.aggregate_id for event in events]
    succeeded = set(successful_ids(response, deal_ids))
    failed = set(failed_ids(response))
    for event in events:
        confirmed = event.aggregate_id in succeeded and event.aggregate_id not in failed
        payload = dict(event.payload or {})
        payload["saga_state"] = "shadow_observed"
        payload["remote_result"] = {"confirmed": confirmed, "source": "shadow_observation"}
        event.payload = payload
        event.status = "completed"
    db.commit()


def mark_skladbot_results(db: Session, events: list[PendingEvent]) -> None:
    canonical_order_ids = sorted({
        normalize_text(order_id)
        for event in events
        for order_id in (event.payload or {}).get("canonical_order_ids") or []
        if normalize_text(order_id)
    })
    import_ids = sorted({
        normalize_text((event.payload or {}).get("import_id"))
        for event in events
        if normalize_text((event.payload or {}).get("import_id"))
    })
    if not import_ids and not canonical_order_ids:
        return
    create_query = select(PendingEvent).where(PendingEvent.event_type == "skladbot_request_create")
    filters = []
    if import_ids:
        filters.append(PendingEvent.payload["import_id"].as_string().in_(import_ids))
    if canonical_order_ids:
        filters.append(PendingEvent.aggregate_id.in_(canonical_order_ids))
    if len(filters) == 2:
        create_query = create_query.where(or_(*filters))
    else:
        create_query = create_query.where(filters[0])
    create_events = db.execute(
        create_query.order_by(PendingEvent.created_at, PendingEvent.id)
    ).scalars().all()
    by_import = {}
    by_order = {}
    for create_event in create_events:
        import_id = normalize_text((create_event.payload or {}).get("import_id"))
        if import_id:
            by_import.setdefault(import_id, []).append(create_event)
        order_id = normalize_text(create_event.aggregate_id or (create_event.payload or {}).get("order_id"))
        if order_id:
            by_order.setdefault(order_id, []).append(create_event)
    linked_order_ids = set()
    if canonical_order_ids:
        for order in db.execute(
            select(Order).where(Order.id.in_([UUID(value) for value in canonical_order_ids]))
        ).scalars().all():
            raw_payload = order.raw_payload or {}
            if normalize_text(raw_payload.get("skladbot_request_id") or raw_payload.get("skladbot_request_number")):
                linked_order_ids.add(str(order.id))
    now = datetime.now(timezone.utc).isoformat()
    for event in events:
        event = refresh_event(db, event)
        if saga_state(event) not in REMOTE_CONFIRMED_STATES:
            continue
        payload = dict(event.payload or {})
        expected_order_ids = sorted({
            normalize_text(value)
            for value in payload.get("canonical_order_ids") or []
            if normalize_text(value)
        })
        matched_create_events = [
            create_event
            for order_id in expected_order_ids
            for create_event in by_order.get(order_id, [])
        ]
        if not expected_order_ids:
            matched_create_events = list(by_import.get(payload.get("import_id"), []))
        keys = sorted({item.idempotency_key for item in matched_create_events if item.idempotency_key})
        covered_order_ids = sorted({
            order_id
            for order_id in expected_order_ids
            if order_id in linked_order_ids or bool(by_order.get(order_id))
        })
        coverage_complete = bool(expected_order_ids) and covered_order_ids == expected_order_ids
        if not expected_order_ids:
            coverage_complete = len(keys) == 1
        payload["skladbot_event_keys"] = keys
        payload["skladbot_event_count"] = len(keys)
        payload["skladbot_expected_order_count"] = len(expected_order_ids)
        payload["skladbot_covered_order_count"] = len(covered_order_ids)
        payload["skladbot_covered_order_ids"] = covered_order_ids
        payload["skladbot_event_key"] = keys[0] if len(keys) == 1 else ""
        payload["skladbot_recorded_at"] = now
        payload["saga_state"] = "skladbot_queued" if coverage_complete else "skladbot_pending"
        event.payload = payload
        event.status = "completed" if coverage_complete else "pending"
        fulfillment = fulfillment_for_event(db, event)
        if fulfillment is not None:
            for create_event in matched_create_events:
                order_id = normalize_text(create_event.aggregate_id or (create_event.payload or {}).get("order_id"))
                try:
                    order_uuid = UUID(order_id)
                except ValueError:
                    order_uuid = None
                if order_uuid is not None:
                    link = link_fulfillment_order_event(
                        db,
                        fulfillment,
                        order_uuid,
                        create_event=create_event,
                    )
                    create_status = normalize_text((create_event.payload or {}).get("create_status"))
                    if create_event.status == "completed" and create_status in {
                        "created", "created_recovered", "already_linked"
                    }:
                        link_state = "created"
                    elif create_status == "ambiguous":
                        link_state = "ambiguous"
                    elif create_status == "blocked_stock":
                        link_state = "blocked_stock"
                    elif create_event.status == "failed":
                        link_state = "manual_review"
                    elif normalize_text((create_event.payload or {}).get("post_state")) == "started":
                        link_state = "post_started"
                    else:
                        link_state = "create_queued"
                    transition_fulfillment_order(
                        db,
                        link,
                        link_state,
                        error=normalize_text(create_event.last_error),
                        remote_request_id=normalize_text(
                            (create_event.payload or {}).get("created_request_id")
                        ),
                    )
            for order_id in expected_order_ids:
                if order_id not in linked_order_ids or by_order.get(order_id):
                    continue
                order = db.get(Order, UUID(order_id))
                link = link_fulfillment_order_event(db, fulfillment, UUID(order_id))
                transition_fulfillment_order(
                    db,
                    link,
                    "created",
                    remote_request_id=normalize_text((order.raw_payload or {}).get("skladbot_request_id"))
                    if order is not None else "",
                )
            if coverage_complete and fulfillment.state == "smartup_confirmed":
                transition_fulfillment(db, fulfillment, "skladbot_create_queued")
            sync_legacy_saga_event(db, fulfillment, event)
            if not coverage_complete:
                refreshed_payload = dict(event.payload or {})
                refreshed_payload["saga_state"] = "skladbot_pending"
                event.payload = refreshed_payload
    db.commit()


def load_slot_sagas(
    db: Session,
    *,
    export_date: date,
    slot_label: str,
    target_delivery_date: date | None,
) -> list[PendingEvent]:
    target_text = target_delivery_date.isoformat() if target_delivery_date else ""
    events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == SMARTUP_DEAL_SAGA_EVENT_TYPE)
        .where(PendingEvent.status.in_(("pending", "failed", "processing")))
        .where(PendingEvent.payload["export_date"].as_string() == export_date.isoformat())
        .where(PendingEvent.payload["slot"].as_string() == normalize_text(slot_label))
        .where(PendingEvent.payload["target_delivery_date"].as_string() == target_text)
        .order_by(PendingEvent.created_at, PendingEvent.id)
    ).scalars().all()
    return list(events)


def load_recoverable_fulfillment_events(
    db: Session,
    *,
    source_scope: str,
    now: datetime | None = None,
    limit: int = 50,
) -> list[PendingEvent]:
    """Load durable non-terminal workflows independently from schedule slots."""
    timestamp = now or datetime.now(timezone.utc)
    recoverable_states = (
        "local_ready",
        "smartup_write_started",
        "smartup_ambiguous",
        "smartup_confirmed",
    )
    return list(db.execute(
        select(PendingEvent)
        .join(SmartupFulfillment, SmartupFulfillment.legacy_saga_event_id == PendingEvent.id)
        .where(SmartupFulfillment.source_scope == normalize_text(source_scope).casefold())
        .where(SmartupFulfillment.state.in_(recoverable_states))
        .where(SmartupFulfillment.available_at <= timestamp)
        .order_by(SmartupFulfillment.available_at, SmartupFulfillment.created_at, SmartupFulfillment.id)
        .limit(max(1, min(int(limit or 50), 200)))
    ).scalars().all())


def imports_from_sagas(events: list[PendingEvent]) -> list[dict]:
    imports = {}
    for event in events:
        snapshot = dict((event.payload or {}).get("import_snapshot") or {})
        import_id = normalize_text(snapshot.get("import_id"))
        if import_id:
            imports[import_id] = snapshot
    return list(imports.values())


def saga_report(events: list[PendingEvent], mode: str) -> dict:
    states = {}
    for event in events:
        state = saga_state(event)
        states[state] = states.get(state, 0) + 1
    return {
        "mode": normalize_saga_mode(mode),
        "deals": len(events),
        "states": states,
        "workflow_key_hashes": [
            hashlib.sha256(normalize_text(event.idempotency_key).encode("utf-8")).hexdigest()
            for event in events
        ],
    }


def saga_state(event: PendingEvent) -> str:
    return normalize_text((event.payload or {}).get("saga_state"))


def refresh_event(db: Session, event: PendingEvent) -> PendingEvent:
    return db.get(PendingEvent, event.id) or event


def fulfillment_for_event(db: Session, event: PendingEvent) -> SmartupFulfillment | None:
    fulfillment_id = normalize_text((event.payload or {}).get("fulfillment_id"))
    if not fulfillment_id:
        return None
    try:
        return db.get(SmartupFulfillment, UUID(fulfillment_id))
    except ValueError:
        return None


def saga_import_snapshot(item: dict) -> dict:
    return {
        "delivery_date": normalize_text(item.get("delivery_date")),
        "import_id": normalize_text(item.get("import_id")),
        "status": normalize_text(item.get("status")),
        "rows_total": int(item.get("rows_total") or 0),
        "rows_imported": int(item.get("rows_imported") or 0),
        "orders_created": int(item.get("orders_created") or 0),
        "items_created": int(item.get("items_created") or 0),
        "duplicate_rows": int(item.get("duplicate_rows") or 0),
        "invalid_rows": int(item.get("invalid_rows") or 0),
        "deal_ids": [normalize_text(value) for value in item.get("deal_ids") or [] if normalize_text(value)],
        "resolved_order_ids": [
            normalize_text(value)
            for value in item.get("resolved_order_ids") or []
            if normalize_text(value)
        ],
    }


def remote_error_for_deal(response: dict, deal_id: str) -> str:
    for error in response.get("errors") or []:
        if normalize_text(error.get("deal_id") or error.get("code") or error.get("id")) == deal_id:
            return redact_secrets(error.get("message") or error.get("error") or "remote status not confirmed")[:500]
    return "remote status not confirmed"


def normalize_text(value) -> str:
    return str(value or "").strip()
