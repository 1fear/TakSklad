from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .google_sheets_pending import queue_google_sheets_export
from .google_sheets_exporter import make_sheet_record
from .kiz_movements_service import MOVEMENT_RESET, record_kiz_movement
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
    return apply_terminal_no_kiz_action(
        db,
        order_id,
        payload,
        target_status=STATUS_ARCHIVED_NO_KIZ,
        audit_action="order_archived_without_kiz",
        google_action="google_sheets_archive_no_kiz_export",
    )


def cancel_order(db: Session, order_id, payload):
    return apply_terminal_no_kiz_action(
        db,
        order_id,
        payload,
        target_status=STATUS_CANCELLED,
        audit_action="order_cancelled",
        google_action="google_sheets_cancel_export",
    )


def complete_orders_without_kiz(db: Session, payload):
    order_ids = unique_order_ids(getattr(payload, "order_ids", []))
    if not order_ids:
        raise ApiError(422, "Order ids are required")
    reason = normalize_text(getattr(payload, "reason", "")) or "Ручное закрытие как выполнено"

    parsed_ids = [parse_uuid(order_id, "order_id") for order_id in order_ids]
    stmt = (
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(Order.id.in_(parsed_ids))
        .with_for_update()
    )
    orders = db.execute(stmt).scalars().all()
    orders_by_id = {str(order.id): order for order in orders}
    errors = []

    for order_id in order_ids:
        order = orders_by_id.get(order_id)
        if order is None:
            errors.append({"order_id": order_id, "message": "Order not found"})
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
    actor = normalize_text(getattr(payload, "actor", "")) or "web"
    idempotency_key = normalize_text(getattr(payload, "idempotency_key", ""))
    export_result = {"status": "queued", "queued": True, "error": ""}
    for order_id in order_ids:
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
            payload={
                "reason": reason,
                "actor": actor,
                "idempotency_key": idempotency_key,
                "items_count": len(order.items),
            },
        ))
        event = queue_google_sheets_export(
            db,
            "google_sheets_archive_export",
            "order",
            str(order.id),
            result=export_result,
        )
        db.add(AuditLog(
            action="google_sheets_archive_export",
            entity_type="order",
            entity_id=str(order.id),
            payload={**export_result, "pending_event_id": str(event.id) if event else ""},
        ))

    db.commit()
    return {
        "requested": len(order_ids),
        "completed": len(order_ids),
        "failed": 0,
        "errors": [],
        "dry_run": False,
    }


def reset_order_for_rescan(db: Session, order_id, payload):
    order = get_order_for_action(db, order_id, with_for_update=True)
    reason = normalize_text(getattr(payload, "reason", "")) or "Сброс заказа на пересканирование"
    ensure_expected_updated_at(order, getattr(payload, "expected_updated_at", ""))
    if order.status == STATUS_RETURNED:
        raise ApiError(409, "Returned orders cannot be reset for rescan")

    if getattr(payload, "dry_run", False):
        return order_to_read(order)

    now = datetime.now(timezone.utc)
    actor = normalize_text(getattr(payload, "actor", "")) or "web"
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
        "web_action_idempotency_key": normalize_text(getattr(payload, "idempotency_key", "")),
        "reset_from_status": order.status,
    })
    order.raw_payload = raw_payload
    order.status = STATUS_NOT_COMPLETED

    db.add(AuditLog(
        action="order_reset_for_rescan",
        entity_type="order",
        entity_id=str(order.id),
        payload={
            "reason": reason,
            "actor": actor,
            "items": reset_counts,
        },
    ))
    db.commit()
    db.refresh(order)
    queue_order_projection_to_google(db, order, action="google_sheets_restore_order_export")
    db.refresh(order)
    return order_to_read(order)


def restore_order(db: Session, order_id, payload):
    order = get_order_for_action(db, order_id, with_for_update=True)
    reason = normalize_text(getattr(payload, "reason", ""))
    if not reason:
        raise ApiError(422, "Reason is required")
    ensure_expected_updated_at(order, getattr(payload, "expected_updated_at", ""))
    if order.status not in (STATUS_ARCHIVED_NO_KIZ, STATUS_CANCELLED):
        raise ApiError(409, "Only cancelled or archive-without-KIZ orders can be restored")

    if getattr(payload, "dry_run", False):
        return order_to_read(order)

    now = datetime.now(timezone.utc)
    actor = normalize_text(getattr(payload, "actor", "")) or "web"
    previous_status = order.status
    order.status = STATUS_NOT_COMPLETED
    raw_payload = dict(order.raw_payload or {})
    raw_payload.update({
        "web_action": "order_restored",
        "web_action_reason": reason,
        "web_action_actor": actor,
        "web_action_at": now.isoformat(),
        "web_action_idempotency_key": normalize_text(getattr(payload, "idempotency_key", "")),
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
        payload={
            "reason": reason,
            "actor": actor,
            "previous_status": previous_status,
            "items_count": len(order.items),
        },
    ))
    db.commit()
    db.refresh(order)
    queue_order_projection_to_google(db, order, action="google_sheets_restore_order_export")
    db.refresh(order)
    return order_to_read(order)


def resync_order_skladbot(db: Session, order_id, payload=None):
    order = get_order_for_action(db, order_id, with_for_update=True)
    reason = normalize_text(getattr(payload, "reason", "")) if payload else ""
    if payload is not None and not reason:
        raise ApiError(422, "Reason is required")
    ensure_expected_updated_at(order, getattr(payload, "expected_updated_at", "") if payload else "")

    raw_payload = dict(order.raw_payload or {})
    raw_payload["skladbot_resync_requested_at"] = datetime.now(timezone.utc).isoformat()
    raw_payload["skladbot_resync_actor"] = normalize_text(getattr(payload, "actor", "")) if payload else "web"
    raw_payload["skladbot_resync_reason"] = reason
    order.raw_payload = raw_payload
    db.add(AuditLog(
        action="order_skladbot_resync_requested",
        entity_type="order",
        entity_id=str(order.id),
        payload=safe_action_payload(payload),
    ))
    db.commit()

    from .skladbot_worker import update_orders_from_skladbot

    update_orders_from_skladbot()
    db.expire_all()
    refreshed_order = get_order_for_action(db, order_id)
    return order_to_read(refreshed_order)


def resync_order_to_google(db: Session, order_id, payload=None):
    order = get_order_for_action(db, order_id)
    db.add(AuditLog(
        action="order_google_resync_requested",
        entity_type="order",
        entity_id=str(order.id),
        payload=safe_action_payload(payload),
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


def apply_terminal_no_kiz_action(db: Session, order_id, payload, target_status, audit_action, google_action):
    order = get_order_for_action(db, order_id, with_for_update=True)
    if order.status == target_status:
        return order_to_read(order)
    if order.status in INACTIVE_ORDER_STATUSES:
        raise ApiError(409, "Order is not active")

    reason = normalize_text(getattr(payload, "reason", ""))
    if not reason:
        raise ApiError(422, "Reason is required")
    ensure_expected_updated_at(order, getattr(payload, "expected_updated_at", ""))
    ensure_order_has_no_scans(order)

    if getattr(payload, "dry_run", False):
        return order_to_read(order)

    now = datetime.now(timezone.utc)
    raw_payload = dict(order.raw_payload or {})
    raw_payload.update({
        "web_action": audit_action,
        "web_action_reason": reason,
        "web_action_actor": normalize_text(getattr(payload, "actor", "")) or "web",
        "web_action_at": now.isoformat(),
        "web_action_idempotency_key": normalize_text(getattr(payload, "idempotency_key", "")),
    })
    order.raw_payload = raw_payload
    order.status = target_status
    for item in order.items:
        item.status = target_status

    db.add(AuditLog(
        action=audit_action,
        entity_type="order",
        entity_id=str(order.id),
        payload={
            "reason": reason,
            "actor": raw_payload["web_action_actor"],
            "idempotency_key": raw_payload["web_action_idempotency_key"],
            "items_count": len(order.items),
        },
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
    partially_scanned_items = [
        item
        for item in order.items
        if (int(item.scanned_blocks or 0) > 0 or len(item.scan_codes or []) > 0)
        and int(item.scanned_blocks or 0) < int(item.quantity_blocks or 0)
    ]
    if partially_scanned_items:
        errors.append({
            "order_id": order_id,
            "message": "Order has partially scanned KIZ codes",
        })
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


def safe_action_payload(payload):
    if payload is None:
        return {}
    return {
        "reason": normalize_text(getattr(payload, "reason", "")),
        "actor": normalize_text(getattr(payload, "actor", "")) or "web",
        "idempotency_key": normalize_text(getattr(payload, "idempotency_key", "")),
    }


def normalize_text(value):
    return str(value or "").strip()
