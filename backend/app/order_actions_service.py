import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, selectinload

from .google_sheets_pending import queue_google_sheets_export
from .google_sheets_exporter import make_sheet_record
from .kiz_movements_service import MOVEMENT_RESET, lock_kiz_code_for_transaction, record_kiz_movement
from .models import AuditLog, Order, OrderItem, PendingEvent
from .orders_service import (
    ApiError,
    INACTIVE_ORDER_STATUSES,
    STATUS_NOT_COMPLETED,
    STATUS_COMPLETED,
    STATUS_RETURNED,
    STATUS_ARCHIVED_NO_KIZ,
    STATUS_CANCELLED,
    order_to_read,
    parse_uuid,
    record_google_sheets_export_result,
)


def archive_order_without_kiz(db: Session, order_id, payload):
    context = admin_action_context("order_archived_without_kiz", [str(order_id)], payload)
    return apply_terminal_no_kiz_action(
        db,
        order_id,
        payload,
        context,
        target_status=STATUS_ARCHIVED_NO_KIZ,
        audit_action="order_archived_without_kiz",
        google_action="google_sheets_archive_no_kiz_export",
    )


def cancel_order(db: Session, order_id, payload):
    context = admin_action_context("order_cancelled", [str(order_id)], payload)
    return apply_terminal_no_kiz_action(
        db,
        order_id,
        payload,
        context,
        target_status=STATUS_CANCELLED,
        audit_action="order_cancelled",
        google_action="google_sheets_cancel_export",
    )


def delete_active_order(db: Session, order_id, payload):
    order_id_text = str(parse_uuid(order_id, "order_id"))
    context = admin_action_context("order_deleted_from_active", [order_id_text], payload)
    existing = find_admin_action_audit(db, "order_deleted_from_active", "order", order_id_text, context["idempotency_key"])
    if existing is not None:
        existing_payload = existing.payload or {}
        return {
            "order_id": order_id_text,
            "deleted": True,
            "dry_run": False,
            "google_delete_event_id": normalize_text(existing_payload.get("google_delete_event_id")),
            "skladbot_request_number": normalize_text(existing_payload.get("skladbot_request_number")),
            "skladbot_request_id": normalize_text(existing_payload.get("skladbot_request_id")),
            "message": "Order delete already processed for this idempotency key",
        }

    order = get_order_for_action(db, order_id, with_for_update=True)
    if order.status in INACTIVE_ORDER_STATUSES:
        raise ApiError(409, "Order is not active")

    ensure_expected_updated_at(order, getattr(payload, "expected_updated_at", ""))
    ensure_order_has_no_scans(order)

    raw_payload = dict(order.raw_payload or {})
    skladbot_request_number = normalize_text(raw_payload.get("skladbot_request_number"))
    skladbot_request_id = normalize_text(raw_payload.get("skladbot_request_id"))
    records = [order_item_to_sheet_record(order, item) for item in order.items]

    if getattr(payload, "dry_run", False):
        return {
            "order_id": order_id_text,
            "deleted": False,
            "dry_run": True,
            "google_delete_event_id": "",
            "skladbot_request_number": skladbot_request_number,
            "skladbot_request_id": skladbot_request_id,
            "message": "Order can be deleted",
        }

    event = queue_google_sheets_export(
        db,
        "google_sheets_delete_import_records_export",
        "order",
        order_id_text,
        result={"status": "queued", "updated": 0, "error": ""},
        payload={
            "records": records,
            "reason": context["reason"],
            "actor": context["actor"],
            "source": context["source"],
            "idempotency_key": child_admin_idempotency_key(context, "google_delete"),
        },
    )
    db.add(AuditLog(
        action="order_deleted_from_active",
        entity_type="order",
        entity_id=order_id_text,
        payload=admin_audit_payload(
            "order_deleted_from_active",
            context,
            order=order,
            extra={
                "items_count": len(order.items),
                "google_records": len(records),
                "google_delete_event_id": str(event.id) if event else "",
                "skladbot_request_number": skladbot_request_number,
                "skladbot_request_id": skladbot_request_id,
                "skladbot_left_manual": bool(skladbot_request_number or skladbot_request_id),
            },
        ),
    ))
    db.delete(order)
    db.commit()
    return {
        "order_id": order_id_text,
        "deleted": True,
        "dry_run": False,
        "google_delete_event_id": str(event.id) if event else "",
        "skladbot_request_number": skladbot_request_number,
        "skladbot_request_id": skladbot_request_id,
        "message": "Order deleted from active backend and queued for Google Sheets deletion",
    }


def complete_orders_without_kiz(db: Session, payload):
    order_ids = unique_order_ids(getattr(payload, "order_ids", []))
    if not order_ids:
        raise ApiError(422, "Order ids are required")
    context = admin_action_context("order_completed_without_kiz", order_ids, payload)
    reason = context["reason"]

    parsed_ids = [parse_uuid(order_id, "order_id") for order_id in order_ids]
    stmt = (
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(Order.id.in_(parsed_ids))
        .with_for_update()
    )
    orders = db.execute(stmt).scalars().all()
    orders_by_id = {str(order.id): order for order in orders}
    duplicate_ids = {
        order_id
        for order_id in order_ids
        if find_admin_action_audit(db, "order_completed_without_kiz", "order", order_id, context["idempotency_key"])
        is not None
    }
    errors = []

    for order_id in order_ids:
        order = orders_by_id.get(order_id)
        if order is None:
            errors.append({"order_id": order_id, "message": "Order not found"})
            continue
        if order_id in duplicate_ids:
            continue
        errors.extend(validate_complete_without_kiz_order(db, order, payload))

    if errors:
        raise ApiError(409, {"message": "Bulk complete without KIZ rejected", "errors": errors})

    if getattr(payload, "dry_run", False):
        return {
            "requested": len(order_ids),
            "completed": 0,
            "failed": 0,
            "errors": [],
            "dry_run": True,
        }

    now = datetime.now(timezone.utc)
    actor = context["actor"]
    idempotency_key = context["idempotency_key"]
    export_result = {"status": "queued", "queued": True, "error": ""}
    completed_count = 0
    for order_id in order_ids:
        if order_id in duplicate_ids:
            continue
        order = orders_by_id[order_id]
        raw_payload = dict(order.raw_payload or {})
        raw_payload.update({
            "web_action": "order_completed_without_kiz",
            "web_action_reason": reason,
            "web_action_actor": actor,
            "web_action_at": now.isoformat(),
            "web_action_idempotency_key": idempotency_key,
            "completed_without_kiz": True,
        })
        order.raw_payload = raw_payload
        order.status = STATUS_COMPLETED
        for item in order.items:
            item.status = STATUS_COMPLETED
            item_raw_payload = dict(item.raw_payload or {})
            item_raw_payload.update({
                "completed_without_kiz": True,
                "completed_without_kiz_at": now.isoformat(),
                "completed_without_kiz_reason": reason,
                "completed_without_kiz_actor": actor,
            })
            item.raw_payload = item_raw_payload

        db.add(AuditLog(
            action="order_completed_without_kiz",
            entity_type="order",
            entity_id=str(order.id),
            payload=admin_audit_payload(
                "order_completed_without_kiz",
                context,
                order=order,
                timestamp=now,
                extra={
                    "items_count": len(order.items),
                },
            ),
        ))
        event = queue_google_sheets_export(
            db,
            "google_sheets_archive_export",
            "order",
            str(order.id),
            result=export_result,
            payload={"idempotency_key": child_admin_idempotency_key(context, f"google_archive:{order.id}")},
        )
        db.add(AuditLog(
            action="google_sheets_archive_export",
            entity_type="order",
            entity_id=str(order.id),
            payload={**export_result, "pending_event_id": str(event.id) if event else ""},
        ))
        completed_count += 1

    db.commit()
    return {
        "requested": len(order_ids),
        "completed": completed_count,
        "failed": 0,
        "errors": [],
        "dry_run": False,
    }


def reset_order_for_rescan(db: Session, order_id, payload):
    order = get_order_for_action(db, order_id, with_for_update=True)
    context = admin_action_context("order_reset_for_rescan", [str(order.id)], payload)
    existing = find_admin_action_audit(db, "order_reset_for_rescan", "order", str(order.id), context["idempotency_key"])
    if existing is not None:
        return order_to_read(order)
    reason = context["reason"]
    ensure_expected_updated_at(order, getattr(payload, "expected_updated_at", ""))
    if order.status == STATUS_RETURNED:
        raise ApiError(409, "Returned orders cannot be reset for rescan")

    if getattr(payload, "dry_run", False):
        return order_to_read(order)

    now = datetime.now(timezone.utc)
    actor = context["actor"]
    reset_counts = []
    for item in order.items:
        scan_codes = list(item.scan_codes or [])
        reset_counts.append({
            "item_id": str(item.id),
            "product": item.product,
            "scanned_blocks": int(item.scanned_blocks or 0),
            "scan_codes": len(scan_codes),
        })
        for scan in scan_codes:
            lock_kiz_code_for_transaction(db, scan.code)
            record_kiz_movement(
                db,
                code=scan.code,
                movement_type=MOVEMENT_RESET,
                order_id=order.id,
                order_item_id=item.id,
                scan_code_id=scan.id,
                source="backend",
                actor=actor,
                occurred_at=now,
                raw_payload={
                    "reason": reason,
                    "reset_from_status": order.status,
                },
            )
        item.scan_codes.clear()
        item.scanned_blocks = 0
        item.status = STATUS_NOT_COMPLETED
        raw_payload = dict(item.raw_payload or {})
        raw_payload["reset_for_rescan_at"] = now.isoformat()
        raw_payload["reset_for_rescan_reason"] = reason
        raw_payload["reset_for_rescan_actor"] = actor
        item.raw_payload = raw_payload

    raw_payload = dict(order.raw_payload or {})
    raw_payload.update({
        "web_action": "order_reset_for_rescan",
        "web_action_reason": reason,
        "web_action_actor": actor,
        "web_action_at": now.isoformat(),
        "web_action_idempotency_key": context["idempotency_key"],
        "reset_from_status": order.status,
    })
    order.raw_payload = raw_payload
    order.status = STATUS_NOT_COMPLETED

    db.add(AuditLog(
        action="order_reset_for_rescan",
        entity_type="order",
        entity_id=str(order.id),
        payload=admin_audit_payload(
            "order_reset_for_rescan",
            context,
            order=order,
            timestamp=now,
            extra={
                "items": reset_counts,
            },
        ),
    ))
    db.commit()
    db.refresh(order)
    queue_order_projection_to_google(db, order, action="google_sheets_restore_order_export")
    db.refresh(order)
    return order_to_read(order)


def restore_order(db: Session, order_id, payload):
    order = get_order_for_action(db, order_id, with_for_update=True)
    context = admin_action_context("order_restored", [str(order.id)], payload)
    existing = find_admin_action_audit(db, "order_restored", "order", str(order.id), context["idempotency_key"])
    if existing is not None:
        return order_to_read(order)
    reason = context["reason"]
    ensure_expected_updated_at(order, getattr(payload, "expected_updated_at", ""))
    if order.status not in (STATUS_ARCHIVED_NO_KIZ, STATUS_CANCELLED):
        raise ApiError(409, "Only cancelled or archive-without-KIZ orders can be restored")

    if getattr(payload, "dry_run", False):
        return order_to_read(order)

    now = datetime.now(timezone.utc)
    actor = context["actor"]
    previous_status = order.status
    order.status = STATUS_NOT_COMPLETED
    raw_payload = dict(order.raw_payload or {})
    raw_payload.update({
        "web_action": "order_restored",
        "web_action_reason": reason,
        "web_action_actor": actor,
        "web_action_at": now.isoformat(),
        "web_action_idempotency_key": context["idempotency_key"],
        "restored_from_status": previous_status,
    })
    order.raw_payload = raw_payload
    for item in order.items:
        if item.status in (STATUS_ARCHIVED_NO_KIZ, STATUS_CANCELLED):
            item.status = STATUS_NOT_COMPLETED

    db.add(AuditLog(
        action="order_restored",
        entity_type="order",
        entity_id=str(order.id),
        payload=admin_audit_payload(
            "order_restored",
            context,
            order=order,
            timestamp=now,
            extra={
                "previous_status": previous_status,
                "items_count": len(order.items),
            },
        ),
    ))
    db.commit()
    db.refresh(order)
    queue_order_projection_to_google(db, order, action="google_sheets_restore_order_export")
    db.refresh(order)
    return order_to_read(order)


def resync_order_skladbot(db: Session, order_id, payload=None):
    order = get_order_for_action(db, order_id, with_for_update=True)
    context = admin_action_context("order_skladbot_resync_requested", [str(order.id)], payload)
    existing = find_admin_action_audit(db, "order_skladbot_resync_requested", "order", str(order.id), context["idempotency_key"])
    if existing is not None:
        return order_to_read(order)
    reason = context["reason"]
    ensure_expected_updated_at(order, getattr(payload, "expected_updated_at", "") if payload else "")

    raw_payload = dict(order.raw_payload or {})
    raw_payload["skladbot_resync_requested_at"] = datetime.now(timezone.utc).isoformat()
    raw_payload["skladbot_resync_actor"] = context["actor"]
    raw_payload["skladbot_resync_reason"] = reason
    raw_payload["skladbot_resync_idempotency_key"] = context["idempotency_key"]
    order.raw_payload = raw_payload
    db.add(AuditLog(
        action="order_skladbot_resync_requested",
        entity_type="order",
        entity_id=str(order.id),
        payload=admin_audit_payload("order_skladbot_resync_requested", context, order=order),
    ))
    db.commit()

    from .skladbot_worker import update_orders_from_skladbot

    update_orders_from_skladbot()
    db.expire_all()
    refreshed_order = get_order_for_action(db, order_id)
    return order_to_read(refreshed_order)


def resync_order_to_google(db: Session, order_id, payload=None):
    order = get_order_for_action(db, order_id)
    context = admin_action_context("order_google_resync_requested", [str(order.id)], payload)
    existing = find_admin_action_audit(db, "order_google_resync_requested", "order", str(order.id), context["idempotency_key"])
    if existing is not None:
        return order_to_read(order)
    db.add(AuditLog(
        action="order_google_resync_requested",
        entity_type="order",
        entity_id=str(order.id),
        payload=admin_audit_payload("order_google_resync_requested", context, order=order),
    ))
    db.commit()

    if order.status not in INACTIVE_ORDER_STATUSES:
        for item in order.items:
            record_google_sheets_export_result(
                db,
                action="google_sheets_scan_export",
                entity_type="order_item",
                entity_id=str(item.id),
            )
    elif order.status == STATUS_ARCHIVED_NO_KIZ:
        record_google_sheets_export_result(
            db,
            action="google_sheets_archive_no_kiz_export",
            entity_type="order",
            entity_id=str(order.id),
        )
    elif order.status == STATUS_CANCELLED:
        record_google_sheets_export_result(
            db,
            action="google_sheets_cancel_export",
            entity_type="order",
            entity_id=str(order.id),
        )
    elif order.status == STATUS_RETURNED:
        record_google_sheets_export_result(
            db,
            action="google_sheets_return_export",
            entity_type="order",
            entity_id=str(order.id),
        )
    else:
        record_google_sheets_export_result(
            db,
            action="google_sheets_archive_export",
            entity_type="order",
            entity_id=str(order.id),
        )

    db.refresh(order)
    return order_to_read(order)


def queue_order_projection_to_google(db: Session, order, action="google_sheets_restore_order_export"):
    records = [order_item_to_sheet_record(order, item) for item in order.items]
    result = {"status": "queued", "queued": True, "error": "", "imported": 0, "duplicates": 0, "updated": 0}
    event = queue_google_sheets_export(
        db,
        action,
        "order",
        str(order.id),
        result=result,
        payload={"records": records},
    )
    db.add(AuditLog(
        action=action,
        entity_type="order",
        entity_id=str(order.id),
        payload={**result, "pending_event_id": str(event.id) if event else ""},
    ))
    db.commit()


def order_item_to_sheet_record(order, item):
    row = {
        "order_date": order.order_date,
        "payment_type": order.payment_type,
        "client": order.client,
        "address": order.address,
        "representative": order.representative or "",
        "product": item.product,
        "quantity_pieces": item.quantity_pieces,
        "quantity_blocks": item.quantity_blocks,
        "status": item.status,
        "source_order_id": (item.raw_payload or {}).get("source_order_id") or order.external_id or str(order.id),
        "source_import_id": (item.raw_payload or {}).get("source_import_id") or str(item.id),
        "source_file": (item.raw_payload or {}).get("source_file") or "",
        "source_row": (item.raw_payload or {}).get("source_row") or "",
        "skladbot_request_number": (order.raw_payload or {}).get("skladbot_request_number") or "",
        "skladbot_request_id": (order.raw_payload or {}).get("skladbot_request_id") or "",
    }
    return make_sheet_record(row, item_key=(item.raw_payload or {}).get("item_key") or str(item.id))


def apply_terminal_no_kiz_action(db: Session, order_id, payload, context, target_status, audit_action, google_action):
    order = get_order_for_action(db, order_id, with_for_update=True)
    existing = find_admin_action_audit(db, audit_action, "order", str(order.id), context["idempotency_key"])
    if existing is not None:
        return order_to_read(order)
    if order.status == target_status:
        return order_to_read(order)
    if order.status in INACTIVE_ORDER_STATUSES:
        raise ApiError(409, "Order is not active")

    reason = context["reason"]
    ensure_expected_updated_at(order, getattr(payload, "expected_updated_at", ""))
    ensure_order_has_no_scans(order)

    if getattr(payload, "dry_run", False):
        return order_to_read(order)

    now = datetime.now(timezone.utc)
    raw_payload = dict(order.raw_payload or {})
    raw_payload.update({
        "web_action": audit_action,
        "web_action_reason": reason,
        "web_action_actor": context["actor"],
        "web_action_at": now.isoformat(),
        "web_action_idempotency_key": context["idempotency_key"],
    })
    order.raw_payload = raw_payload
    order.status = target_status
    for item in order.items:
        item.status = target_status

    db.add(AuditLog(
        action=audit_action,
        entity_type="order",
        entity_id=str(order.id),
        payload=admin_audit_payload(
            audit_action,
            context,
            order=order,
            timestamp=now,
            extra={
                "items_count": len(order.items),
            },
        ),
    ))
    db.commit()
    db.refresh(order)

    record_google_sheets_export_result(
        db,
        action=google_action,
        entity_type="order",
        entity_id=str(order.id),
    )
    db.refresh(order)
    return order_to_read(order)


def get_order_for_action(db: Session, order_id, with_for_update=False):
    parsed_order_id = parse_uuid(order_id, "order_id")
    stmt = (
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(Order.id == parsed_order_id)
    )
    if with_for_update:
        stmt = stmt.with_for_update()
    order = db.execute(stmt).scalar_one_or_none()
    if order is None:
        raise ApiError(404, "Order not found")
    return order


def ensure_order_has_no_scans(order):
    scanned_items = [
        item
        for item in order.items
        if int(item.scanned_blocks or 0) > 0 or len(item.scan_codes or []) > 0
    ]
    if scanned_items:
        raise ApiError(409, {
            "message": "Order already has scanned KIZ codes",
            "items": [
                {
                    "id": str(item.id),
                    "product": item.product,
                    "scanned_blocks": item.scanned_blocks,
                    "scan_codes": len(item.scan_codes or []),
                }
                for item in scanned_items
            ],
        })


def validate_complete_without_kiz_order(db: Session, order, payload):
    errors = []
    order_id = str(order.id)
    if order.status in INACTIVE_ORDER_STATUSES:
        errors.append({"order_id": order_id, "message": "Order is not active"})
    expected_by_order = getattr(payload, "expected_updated_at_by_order", {}) or {}
    try:
        ensure_expected_updated_at(order, expected_by_order.get(order_id, ""))
    except ApiError as exc:
        errors.append({"order_id": order_id, "message": str(exc.detail)})
    if pending_google_export_exists(db, order):
        errors.append({"order_id": order_id, "message": "Order has pending Google export"})
    return errors


def pending_google_export_exists(db: Session, order):
    entity_ids = {str(order.id), *(str(item.id) for item in order.items)}
    events = db.execute(
        select(PendingEvent)
        .where(PendingEvent.event_type == "google_sheets_export")
        .where(PendingEvent.status.in_(("pending", "failed")))
    ).scalars().all()
    for event in events:
        payload = event.payload or {}
        if payload.get("action") == "google_sheets_skladbot_export":
            continue
        if str(payload.get("entity_id") or "") in entity_ids:
            return True
        order_ids = payload.get("order_ids") or []
        if str(order.id) in {str(value) for value in order_ids}:
            return True
    return False


def unique_order_ids(order_ids):
    result = []
    seen = set()
    for value in order_ids or []:
        order_id = normalize_text(value)
        if not order_id or order_id in seen:
            continue
        seen.add(order_id)
        result.append(order_id)
    return result


def ensure_expected_updated_at(order, expected_updated_at):
    expected = normalize_text(expected_updated_at)
    if not expected:
        return
    actual = order.updated_at.isoformat() if order.updated_at else ""
    if actual and actual != expected:
        raise ApiError(409, {
            "message": "Order changed after web table was loaded",
            "expected_updated_at": expected,
            "actual_updated_at": actual,
        })


def admin_action_context(action, order_ids, payload):
    reason = normalize_text(getattr(payload, "reason", "") if payload is not None else "")
    if not reason:
        raise ApiError(422, "Reason is required")
    actor = normalize_text(getattr(payload, "actor", "") if payload is not None else "") or "web"
    source = normalize_text(getattr(payload, "source", "") if payload is not None else "") or actor
    affected_order_ids = [normalize_text(order_id) for order_id in (order_ids or []) if normalize_text(order_id)]
    explicit_key = normalize_text(getattr(payload, "idempotency_key", "") if payload is not None else "")
    return {
        "action": action,
        "reason": reason,
        "actor": actor,
        "source": source,
        "idempotency_key": normalize_admin_idempotency_key(action, explicit_key),
        "affected_order_ids": affected_order_ids,
    }


def normalize_admin_idempotency_key(action, key):
    key = normalize_text(key)
    if key and len(key) <= 120:
        return key
    seed = key or uuid.uuid4().hex
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    return f"admin:{normalize_text(action)[:48]}:{digest}"[:180]


def child_admin_idempotency_key(context, suffix):
    parent = normalize_text((context or {}).get("idempotency_key"))
    suffix = normalize_text(suffix)
    key = f"{parent}:{suffix}" if suffix else parent
    if len(key) <= 180:
        return key
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return f"{parent[:120]}:{digest}"[:180]


def find_admin_action_audit(db: Session, action, entity_type, entity_id, idempotency_key):
    key = normalize_text(idempotency_key)
    if not key:
        return None
    rows = db.execute(
        select(AuditLog)
        .where(AuditLog.action == action)
        .where(AuditLog.entity_type == entity_type)
        .where(AuditLog.entity_id == entity_id)
        .order_by(desc(AuditLog.created_at), desc(AuditLog.id))
        .limit(50)
    ).scalars().all()
    for row in rows:
        if normalize_text((row.payload or {}).get("idempotency_key")) == key:
            return row
    return None


def admin_audit_payload(action, context, order=None, timestamp=None, extra=None):
    timestamp = timestamp or datetime.now(timezone.utc)
    affected_order_ids = list((context or {}).get("affected_order_ids") or [])
    affected_item_ids = []
    raw_context = {}
    if order is not None:
        order_id = str(order.id)
        if order_id not in affected_order_ids:
            affected_order_ids.append(order_id)
        affected_item_ids = [str(item.id) for item in (order.items or [])]
        order_raw = order.raw_payload or {}
        raw_context = {
            "order_status": order.status,
            "items_count": len(order.items or []),
            "skladbot_request_number": normalize_text(order_raw.get("skladbot_request_number")),
            "skladbot_request_id": normalize_text(order_raw.get("skladbot_request_id")),
        }
    payload = {
        "action": action,
        "actor": normalize_text((context or {}).get("actor")) or "web",
        "source": normalize_text((context or {}).get("source")) or normalize_text((context or {}).get("actor")) or "web",
        "reason": normalize_text((context or {}).get("reason")),
        "idempotency_key": normalize_text((context or {}).get("idempotency_key")),
        "affected_order_ids": affected_order_ids,
        "affected_item_ids": affected_item_ids,
        "timestamp": timestamp.isoformat(),
        "raw_context": raw_context,
    }
    payload.update(extra or {})
    return payload


def safe_action_payload(payload):
    if payload is None:
        return {}
    return {
        "reason": normalize_text(getattr(payload, "reason", "")),
        "actor": normalize_text(getattr(payload, "actor", "")) or "web",
        "source": normalize_text(getattr(payload, "source", "")) or normalize_text(getattr(payload, "actor", "")) or "web",
        "idempotency_key": normalize_text(getattr(payload, "idempotency_key", "")),
    }


def normalize_text(value):
    return str(value or "").strip()
