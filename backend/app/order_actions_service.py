from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .google_sheets_exporter import (
    archive_backend_order_to_google_sheets,
    archive_backend_order_without_kiz_to_google_sheets,
    cancel_backend_order_in_google_sheets,
    mark_backend_order_returned_in_google_sheets,
    sync_backend_order_item_to_google_sheets,
)
from .models import AuditLog, Order, OrderItem
from .orders_service import (
    ApiError,
    INACTIVE_ORDER_STATUSES,
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
        google_exporter=archive_backend_order_without_kiz_to_google_sheets,
    )


def cancel_order(db: Session, order_id, payload):
    return apply_terminal_no_kiz_action(
        db,
        order_id,
        payload,
        target_status=STATUS_CANCELLED,
        audit_action="order_cancelled",
        google_action="google_sheets_cancel_export",
        google_exporter=cancel_backend_order_in_google_sheets,
    )


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
                callback=lambda item=item: sync_backend_order_item_to_google_sheets(item),
            )
    elif order.status == STATUS_ARCHIVED_NO_KIZ:
        record_google_sheets_export_result(
            db,
            action="google_sheets_archive_no_kiz_export",
            entity_type="order",
            entity_id=str(order.id),
            callback=lambda: archive_backend_order_without_kiz_to_google_sheets(order),
        )
    elif order.status == STATUS_CANCELLED:
        record_google_sheets_export_result(
            db,
            action="google_sheets_cancel_export",
            entity_type="order",
            entity_id=str(order.id),
            callback=lambda: cancel_backend_order_in_google_sheets(order),
        )
    elif order.status == STATUS_RETURNED:
        record_google_sheets_export_result(
            db,
            action="google_sheets_return_export",
            entity_type="order",
            entity_id=str(order.id),
            callback=lambda: mark_backend_order_returned_in_google_sheets(order),
        )
    else:
        record_google_sheets_export_result(
            db,
            action="google_sheets_archive_export",
            entity_type="order",
            entity_id=str(order.id),
            callback=lambda: archive_backend_order_to_google_sheets(order),
        )

    db.refresh(order)
    return order_to_read(order)


def apply_terminal_no_kiz_action(db: Session, order_id, payload, target_status, audit_action, google_action, google_exporter):
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
        callback=lambda: google_exporter(order),
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
