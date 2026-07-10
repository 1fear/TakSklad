import hashlib
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PendingEvent
from .outbox_service import queue_outbox_event
from .redaction import redact_secrets


SMARTUP_DEAL_SAGA_EVENT_TYPE = "smartup_deal_saga"
SMARTUP_DEAL_SAGA_ACTION = "smartup_change_status"
SMARTUP_SAGA_MODES = {"disabled", "shadow", "enforced"}
REMOTE_CONFIRMED_STATES = {"remote_confirmed", "skladbot_pending", "skladbot_queued"}
RECONCILE_STATES = {"remote_write_started", "remote_failed"}


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
) -> list[PendingEvent]:
    mode = normalize_saga_mode(mode)
    events = []
    for item in imports:
        import_id = normalize_text(item.get("import_id"))
        for deal_id in sorted({normalize_text(value) for value in item.get("deal_ids") or [] if normalize_text(value)}):
            key = saga_idempotency_key(
                export_date=export_date,
                slot_label=slot_label,
                target_delivery_date=target_delivery_date,
                deal_id=deal_id,
                target_status=target_status,
            )
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
            remote_status = normalize_text(statuses.get(event.aggregate_id))
            payload["remote_reconciliations"] = int(payload.get("remote_reconciliations") or 0) + 1
            payload["last_reconciled_at"] = now
            payload["last_reconciled_status"] = remote_status
            if remote_status == target_status:
                payload["saga_state"] = "remote_confirmed"
                payload["remote_result"] = {"confirmed": True, "source": "read_reconciliation"}
                event.status = "pending"
            elif remote_status:
                payload["saga_state"] = "intent_persisted"
                payload["remote_result"] = {
                    "confirmed": False,
                    "source": "read_reconciliation",
                    "observed_status": remote_status,
                }
                event.status = "pending"
            else:
                payload["saga_state"] = "remote_failed"
                payload["remote_result"] = {
                    "confirmed": False,
                    "source": "read_reconciliation",
                    "error": "remote status could not be reconciled",
                }
                event.status = "failed"
                event.last_error = "remote status could not be reconciled"
            event.payload = payload
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
    for event in to_change:
        payload = dict(event.payload or {})
        payload["saga_state"] = "remote_write_started"
        payload["remote_attempts"] = int(payload.get("remote_attempts") or 0) + 1
        payload["remote_write_started_at"] = now
        event.payload = payload
        event.status = "processing"
    db.commit()
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
    create_events = db.execute(
        select(PendingEvent).where(PendingEvent.event_type == "skladbot_request_create")
    ).scalars().all()
    by_import = {}
    for create_event in create_events:
        import_id = normalize_text((create_event.payload or {}).get("import_id"))
        if import_id:
            by_import.setdefault(import_id, []).append(create_event)
    now = datetime.now(timezone.utc).isoformat()
    for event in events:
        event = refresh_event(db, event)
        if saga_state(event) not in REMOTE_CONFIRMED_STATES:
            continue
        payload = dict(event.payload or {})
        keys = sorted({item.idempotency_key for item in by_import.get(payload.get("import_id"), []) if item.idempotency_key})
        payload["skladbot_event_keys"] = keys
        payload["skladbot_event_count"] = len(keys)
        payload["skladbot_event_key"] = keys[0] if len(keys) == 1 else ""
        payload["skladbot_recorded_at"] = now
        payload["saga_state"] = "skladbot_queued" if len(keys) == 1 else "skladbot_pending"
        event.payload = payload
        event.status = "completed" if len(keys) == 1 else "pending"
    db.commit()


def load_slot_sagas(
    db: Session,
    *,
    export_date: date,
    slot_label: str,
    target_delivery_date: date | None,
) -> list[PendingEvent]:
    events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == SMARTUP_DEAL_SAGA_EVENT_TYPE)
        .where(PendingEvent.status.in_(("pending", "failed", "processing")))
        .order_by(PendingEvent.created_at, PendingEvent.id)
    ).scalars().all()
    target_text = target_delivery_date.isoformat() if target_delivery_date else ""
    return [
        event for event in events
        if normalize_text((event.payload or {}).get("export_date")) == export_date.isoformat()
        and normalize_text((event.payload or {}).get("slot")) == normalize_text(slot_label)
        and normalize_text((event.payload or {}).get("target_delivery_date")) == target_text
    ]


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
    }


def remote_error_for_deal(response: dict, deal_id: str) -> str:
    for error in response.get("errors") or []:
        if normalize_text(error.get("deal_id") or error.get("code") or error.get("id")) == deal_id:
            return redact_secrets(error.get("message") or error.get("error") or "remote status not confirmed")[:500]
    return "remote status not confirmed"


def normalize_text(value) -> str:
    return str(value or "").strip()
