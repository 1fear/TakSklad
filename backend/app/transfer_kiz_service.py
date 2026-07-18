"""DB-only completion checks for Telegram transfer-payment KIZ deliveries."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import ImportJob, OrderItem, PendingEvent, ScanCode


TRANSFER_KIZ_COMPLETION_CHECK_EVENT_TYPE = "transfer_kiz_completion_check"
TRANSFER_KIZ_CLIENT_DELIVERY_EVENT_TYPE = "transfer_kiz_client_delivery"
TELEGRAM_NOTIFICATION_EVENT_TYPE = "telegram_notification"
TRANSFER_KIZ_CONTRACT_VERSION = "v1"


def queue_transfer_kiz_completion_check(db: Session, *, scan: ScanCode, item: OrderItem) -> PendingEvent | None:
    """Queue one post-scan DB check only for an item from a Telegram import."""
    import_id = _text((item.raw_payload or {}).get("backend_import_id"))
    if not import_id:
        return None
    import_job = db.get(ImportJob, _uuid(import_id))
    if import_job is None or import_job.source != "telegram":
        return None
    key = f"transfer-kiz:completion-check:{TRANSFER_KIZ_CONTRACT_VERSION}:scan:{scan.id}"
    existing = db.execute(select(PendingEvent).where(PendingEvent.idempotency_key == key)).scalar_one_or_none()
    if existing is not None:
        return existing
    event = PendingEvent(
        event_type=TRANSFER_KIZ_COMPLETION_CHECK_EVENT_TYPE,
        action="check_completion",
        aggregate_type="scan_code",
        aggregate_id=str(scan.id),
        idempotency_key=key,
        status="pending",
        payload={"scan_id": str(scan.id)},
    )
    db.add(event)
    db.flush()
    return event


def claim_transfer_kiz_completion_check(db: Session) -> PendingEvent | None:
    """Claim the next DB-only check; the caller owns its transaction/commit."""
    event = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == TRANSFER_KIZ_COMPLETION_CHECK_EVENT_TYPE)
        .where(PendingEvent.status.in_(("pending", "failed")))
        .order_by(PendingEvent.created_at, PendingEvent.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    ).scalar_one_or_none()
    if event is None:
        return None
    event.status = "processing"
    event.attempts += 1
    return event


def process_transfer_kiz_completion_check(db: Session, event: PendingEvent) -> dict:
    """Recheck the exact imported file in the DB and queue a delivery when ready."""
    if event.event_type != TRANSFER_KIZ_COMPLETION_CHECK_EVENT_TYPE:
        raise ValueError("unexpected_transfer_kiz_completion_check_event")

    scan_id = _text((event.payload or {}).get("scan_id"))
    scan = db.get(ScanCode, _uuid(scan_id)) if scan_id else None
    if scan is None:
        return _finish_check(event, "scan_not_found")
    item = db.execute(
        select(OrderItem).options(selectinload(OrderItem.order)).where(OrderItem.id == scan.order_item_id)
    ).scalar_one_or_none()
    if item is None:
        return _finish_check(event, "item_not_found")

    import_id = _text((item.raw_payload or {}).get("backend_import_id"))
    source_file = _text((item.raw_payload or {}).get("source_file"))
    import_job = db.get(ImportJob, _uuid(import_id)) if import_id else None
    if import_job is None or import_job.source != "telegram" or not source_file:
        return _finish_check(event, "not_telegram_import_item")

    source_key = transfer_kiz_source_key(import_id, source_file)
    readiness = transfer_kiz_delivery_readiness(db, source_key)
    blockers = readiness["blockers"]
    if blockers:
        return _finish_check(event, blockers[0], source_key=source_key, blockers=blockers)

    delivery = queue_transfer_kiz_client_delivery(db, source_key=source_key, import_id=import_id, source_file=source_file)
    event.status = "completed"
    event.last_error = ""
    event.completed_at = datetime.now(timezone.utc)
    event.payload = {**(event.payload or {}), "source_key": source_key, "delivery_event_id": str(delivery.id)}
    return {"status": "ready", "source_key": source_key, "delivery_event_id": str(delivery.id), "blockers": []}


def queue_transfer_kiz_client_delivery(
    db: Session, *, source_key: str, import_id: str, source_file: str,
) -> PendingEvent:
    key = _source_idempotency_key("transfer-kiz:client-delivery", source_key)
    existing = db.execute(select(PendingEvent).where(PendingEvent.idempotency_key == key)).scalar_one_or_none()
    if existing is not None:
        return existing
    event = PendingEvent(
        event_type=TRANSFER_KIZ_CLIENT_DELIVERY_EVENT_TYPE,
        action="deliver_transfer_kiz",
        aggregate_type="import_file",
        aggregate_id=source_key,
        idempotency_key=key,
        status="pending",
        payload={
            "source_key": source_key,
            "import_id": import_id,
            "source_file": source_file,
            "contract_version": TRANSFER_KIZ_CONTRACT_VERSION,
        },
    )
    db.add(event)
    db.flush()
    return event


def queue_transfer_kiz_undo_alert(db: Session, *, item: OrderItem) -> PendingEvent | None:
    """Create one admin alert when an already delivered transfer KIZ is undone."""
    import_id = _text((item.raw_payload or {}).get("backend_import_id"))
    source_file = _text((item.raw_payload or {}).get("source_file"))
    if not import_id or not source_file:
        return None
    source_key = transfer_kiz_source_key(import_id, source_file)
    delivery = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == TRANSFER_KIZ_CLIENT_DELIVERY_EVENT_TYPE)
        .where(PendingEvent.aggregate_id == source_key)
        .where(PendingEvent.status == "completed")
    ).scalar_one_or_none()
    if delivery is None:
        return None
    key = _source_idempotency_key("telegram:notification:transfer-kiz-undo", source_key)
    existing = db.execute(select(PendingEvent).where(PendingEvent.idempotency_key == key)).scalar_one_or_none()
    if existing is not None:
        return existing
    alert = PendingEvent(
        event_type=TELEGRAM_NOTIFICATION_EVENT_TYPE,
        status="pending",
        idempotency_key=key,
        payload={
            "kind": "daily_reconciliation_alert",
            "text": "TakSklad: отменён КИЗ после подготовки передачи. Нужна ручная проверка.",
        },
    )
    db.add(alert)
    db.flush()
    return alert


def transfer_kiz_source_key(import_id: str, source_file: str) -> str:
    return f"import:{import_id}:file:{source_file}"


def transfer_kiz_delivery_readiness(db: Session, source_key: str) -> dict:
    """Return DB-only exact-file delivery readiness without any Telegram side effect."""
    import_id, source_file = _parse_source_key(source_key)
    import_job = db.get(ImportJob, _uuid(import_id)) if import_id else None
    if import_job is None or import_job.source != "telegram":
        return {"source_key": source_key, "import_job": None, "items": [], "source_file": source_file, "blockers": ["not_telegram_import"]}
    items = _exact_import_file_items(db, import_id, source_file)
    if not items:
        return {"source_key": source_key, "import_job": import_job, "items": [], "source_file": source_file, "blockers": ["exact_import_file_items_missing"]}
    orders = {item.order for item in items if item.order is not None}
    return {
        "source_key": source_key,
        "import_job": import_job,
        "items": items,
        "source_file": source_file,
        "blockers": _completion_blockers(import_job, items, orders),
    }


def _exact_import_file_items(db: Session, import_id: str, source_file: str) -> list[OrderItem]:
    candidates = db.execute(
        select(OrderItem).options(selectinload(OrderItem.order), selectinload(OrderItem.scan_codes))
    ).scalars().all()
    return [
        item for item in candidates
        if _text((item.raw_payload or {}).get("backend_import_id")) == import_id
        and _text((item.raw_payload or {}).get("source_file")) == source_file
    ]


def _completion_blockers(import_job: ImportJob, items: list[OrderItem], orders: set) -> list[str]:
    from .reports_service import payment_group
    from .kiz_reports_service import incomplete_kiz_items

    raw_payload = import_job.raw_payload or {}
    blockers = []
    if _count(raw_payload.get("skipped_rows_count")) or _count(raw_payload.get("invalid_rows")):
        blockers.append("import_has_skipped_or_invalid_rows")
    source_rows_count = _count(raw_payload.get("source_rows_count"))
    if source_rows_count and source_rows_count != int(import_job.rows_total or 0) + _count(raw_payload.get("skipped_rows_count")):
        blockers.append("source_rows_count_mismatch")
    if any(payment_group(order.payment_type) != "transfer" for order in orders):
        blockers.append("non_transfer_payment_group")
    if not any(item.scan_codes for item in items if item.requires_kiz):
        blockers.append("no_scans")
    if any(not _text((order.raw_payload or {}).get("skladbot_request_number")) for order in orders):
        blockers.append("missing_skladbot_request_number")
    if incomplete_kiz_items(items):
        blockers.append("incomplete_kiz_items")
    return blockers


def _finish_check(event: PendingEvent, reason: str, *, source_key: str = "", blockers: list[str] | None = None) -> dict:
    event.status = "completed"
    event.last_error = reason
    event.completed_at = datetime.now(timezone.utc)
    event.payload = {
        **(event.payload or {}),
        "source_key": source_key,
        "completion_reason": reason,
        "blockers": list(blockers or [reason]),
    }
    return {"status": "not_ready", "source_key": source_key, "blockers": list(blockers or [reason])}


def _count(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _text(value: object) -> str:
    return str(value or "").strip()


def _uuid(value: object):
    try:
        return uuid.UUID(_text(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _parse_source_key(source_key: str) -> tuple[str, str]:
    source_key = _text(source_key)
    if not source_key.startswith("import:"):
        return "", ""
    import_and_file = source_key[len("import:"):]
    import_id, marker, source_file = import_and_file.partition(":file:")
    if not marker or not _uuid(import_id) or not _text(source_file):
        return "", ""
    return import_id, source_file


def _source_idempotency_key(prefix: str, source_key: str) -> str:
    digest = hashlib.sha256(source_key.encode("utf-8")).hexdigest()
    return f"{prefix}:{TRANSFER_KIZ_CONTRACT_VERSION}:{digest}"
