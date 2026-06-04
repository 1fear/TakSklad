import os
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .models import AuditLog, Order, PendingEvent
from .skladbot_worker import normalize_text, product_sku_key


SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE = "skladbot_request_dry_run"
SKLADBOT_CREATE_REQUESTS_MODE_ENV = "SKLADBOT_CREATE_REQUESTS_MODE"
SKLADBOT_CREATE_REQUESTS_DEFAULT_MODE = "dry_run"
SKLADBOT_CUSTOMER_ID = 6211
SKLADBOT_REQUEST_TYPE_ID = 3389

SKU_MAPPING = {
    "red:op": {
        "product_data_id": 2189390,
        "barcode": "4006396053947",
        "is_main_barcode": False,
    },
    "brown:op": {
        "product_data_id": 2189391,
        "barcode": "4006396053978",
        "is_main_barcode": False,
    },
    "gold:ssl": {
        "product_data_id": 2189394,
        "barcode": "4006396054005",
        "is_main_barcode": False,
    },
}


def skladbot_create_requests_mode(environ: dict[str, str] | None = None) -> str:
    environ = environ or os.environ
    mode = normalize_text(environ.get(SKLADBOT_CREATE_REQUESTS_MODE_ENV)).lower()
    if mode in {"dry_run", "enabled", "disabled"}:
        return mode
    return SKLADBOT_CREATE_REQUESTS_DEFAULT_MODE


def create_skladbot_dry_run_for_import(db: Session, import_id: str, rebuild: bool = False) -> dict[str, Any]:
    configured_mode = skladbot_create_requests_mode()
    if configured_mode == "disabled":
        return {
            "status": "disabled",
            "mode": configured_mode,
            "orders": 0,
            "ready": 0,
            "blocked": 0,
            "already_linked": 0,
            "event_id": "",
        }
    mode = "dry_run"

    existing_event = find_skladbot_dry_run_event(db, import_id)
    if existing_event is not None and not rebuild:
        summary = (existing_event.payload or {}).get("summary") or {}
        return {
            **default_summary(mode=mode),
            **summary,
            "status": "deduplicated",
            "event_id": str(existing_event.id),
        }

    orders = list_orders_for_import(db, import_id)
    dry_runs = [
        build_order_dry_run(order, items, import_id, index)
        for index, (order, items) in enumerate(orders, start=1)
    ]
    summary = summarize_dry_runs(dry_runs, mode=mode)
    generated_at = datetime.now(timezone.utc).isoformat()
    event_payload = {
        "version": 1,
        "mode": mode,
        "configured_mode": configured_mode,
        "dry_run": True,
        "would_post": False,
        "import_id": import_id,
        "generated_at": generated_at,
        "summary": summary,
        "dry_runs": dry_runs,
    }

    if existing_event is None:
        event = PendingEvent(
            event_type=SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE,
            status="completed",
            attempts=0,
            payload=event_payload,
            last_error=None,
        )
        db.add(event)
        db.flush()
    else:
        event = existing_event
        event.status = "completed"
        event.attempts = 0
        event.payload = event_payload
        event.last_error = None
        db.add(event)
        db.flush()

    summary = {**summary, "event_id": str(event.id)}
    event.payload = {**event.payload, "summary": summary}
    add_skladbot_dry_run_audit(db, import_id, str(event.id), summary, dry_runs)
    return summary


def list_skladbot_dry_runs(db: Session, import_id: str | None = None) -> list[dict[str, Any]]:
    events = list_skladbot_dry_run_events(db, import_id)
    result = []
    for event in events:
        payload = event.payload or {}
        generated_at = payload.get("generated_at") or None
        for row in payload.get("dry_runs") or []:
            event_id = str(event.id)
            order_id = str(row.get("order_id") or "")
            result.append({
                "id": f"{event_id}:{order_id}" if order_id else event_id,
                "event_id": event_id,
                "import_id": str(row.get("import_id") or payload.get("import_id") or ""),
                "order_id": order_id,
                "client": str(row.get("client") or ""),
                "order_date": row.get("order_date") or None,
                "payment_type": str(row.get("payment_type") or ""),
                "address": str(row.get("address") or ""),
                "blocks": int(row.get("blocks") or 0),
                "status": str(row.get("status") or ""),
                "error": str(row.get("error") or ""),
                "products": row.get("products") or [],
                "payload": row.get("payload") or {},
                "generated_at": generated_at,
            })
    return result


def rebuild_skladbot_dry_run(db: Session, dry_run_id: str) -> list[dict[str, Any]]:
    event_id = normalize_text(dry_run_id).split(":", 1)[0]
    try:
        event_uuid = uuid.UUID(event_id)
    except ValueError as exc:
        raise ValueError("SkladBot dry-run не найден") from exc
    event = db.get(PendingEvent, event_uuid)
    if event is None or event.event_type != SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE:
        raise ValueError("SkladBot dry-run не найден")
    import_id = str((event.payload or {}).get("import_id") or "")
    if not import_id:
        raise ValueError("У dry-run нет import_id")
    create_skladbot_dry_run_for_import(db, import_id, rebuild=True)
    db.commit()
    return list_skladbot_dry_runs(db, import_id)


def list_orders_for_import(db: Session, import_id: str) -> list[tuple[Order, list[Any]]]:
    stmt = select(Order).options(selectinload(Order.items)).order_by(Order.created_at.asc(), Order.client.asc())
    orders = db.execute(stmt).scalars().unique().all()
    matched_orders = []
    for order in orders:
        matched_items = [
            item
            for item in order.items
            if str((item.raw_payload or {}).get("backend_import_id") or "") == import_id
        ]
        if not matched_items:
            continue
        matched_orders.append((order, list(order.items)))
    return matched_orders


def find_skladbot_dry_run_event(db: Session, import_id: str) -> PendingEvent | None:
    events = list_skladbot_dry_run_events(db, import_id)
    return events[0] if events else None


def list_skladbot_dry_run_events(db: Session, import_id: str | None = None) -> list[PendingEvent]:
    stmt = (
        select(PendingEvent)
        .where(PendingEvent.event_type == SKLADBOT_REQUEST_DRY_RUN_EVENT_TYPE)
        .order_by(PendingEvent.created_at.desc(), PendingEvent.id.desc())
    )
    events = db.execute(stmt).scalars().all()
    if not import_id:
        return events
    return [
        event
        for event in events
        if str((event.payload or {}).get("import_id") or "") == import_id
    ]


def build_order_dry_run(order: Order, items: list[Any], import_id: str, index: int) -> dict[str, Any]:
    raw_payload = order.raw_payload or {}
    linked_number = normalize_text(raw_payload.get("skladbot_request_number"))
    linked_id = normalize_text(raw_payload.get("skladbot_request_id"))
    products = [build_product_dry_run(item.product, item.quantity_blocks) for item in items]
    blocks = sum(int(product.get("quantity_blocks") or 0) for product in products)
    status = "ready"
    error = ""

    if linked_number or linked_id:
        status = "already_linked"
        error = "У заказа уже есть номер или ID SkladBot"
    else:
        blocked_errors = [product["error"] for product in products if product.get("status") == "blocked"]
        if blocked_errors:
            status = "blocked"
            error = "; ".join(blocked_errors)

    payload = {}
    if status == "ready":
        payload = build_skladbot_payload(order, products)

    return {
        "id": f"{import_id}:{index}",
        "import_id": import_id,
        "order_id": str(order.id),
        "client": order.client,
        "order_date": order.order_date.isoformat() if order.order_date else None,
        "payment_type": order.payment_type,
        "address": order.address,
        "blocks": blocks,
        "status": status,
        "error": error,
        "products": products,
        "payload": payload,
    }


def build_product_dry_run(product: str, quantity_blocks: int) -> dict[str, Any]:
    sku_key = product_sku_key(product)
    mapping = SKU_MAPPING.get(sku_key)
    blocks = int(quantity_blocks or 0)
    if blocks <= 0:
        return {
            "product": product,
            "quantity_blocks": blocks,
            "product_data_id": None,
            "barcode": "",
            "is_main_barcode": False,
            "status": "blocked",
            "error": f"Некорректное количество блоков для {product}: {blocks}",
        }
    if not mapping:
        return {
            "product": product,
            "quantity_blocks": blocks,
            "product_data_id": None,
            "barcode": "",
            "is_main_barcode": False,
            "status": "blocked",
            "error": f"SKU не найден в mapping: {product}",
        }
    return {
        "product": product,
        "quantity_blocks": blocks,
        "product_data_id": mapping["product_data_id"],
        "barcode": mapping["barcode"],
        "is_main_barcode": mapping["is_main_barcode"],
        "status": "ready",
        "error": "",
    }


def build_skladbot_payload(order: Order, products: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "customer_id": SKLADBOT_CUSTOMER_ID,
        "request_type_id": SKLADBOT_REQUEST_TYPE_ID,
        "fields": {
            "address": {"value": order.address},
            "comment": {"value": order.payment_type},
            "company_name": {"value": order.client},
            "unloading_date": {"value": order.order_date.isoformat() if order.order_date else ""},
        },
        "products": [
            {
                "product_data_id": product["product_data_id"],
                "barcode": product["barcode"],
                "is_main_barcode": product["is_main_barcode"],
                "amount": product["quantity_blocks"],
            }
            for product in products
        ],
    }


def summarize_dry_runs(dry_runs: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    summary = default_summary(mode=mode)
    summary["orders"] = len(dry_runs)
    for item in dry_runs:
        status = str(item.get("status") or "")
        if status in {"ready", "blocked", "already_linked"}:
            summary[status] += 1
    return summary


def default_summary(mode: str) -> dict[str, Any]:
    return {
        "status": "ok",
        "mode": mode,
        "orders": 0,
        "ready": 0,
        "blocked": 0,
        "already_linked": 0,
        "event_id": "",
    }


def add_skladbot_dry_run_audit(
    db: Session,
    import_id: str,
    event_id: str,
    summary: dict[str, Any],
    dry_runs: list[dict[str, Any]],
) -> None:
    db.add(AuditLog(
        action="skladbot_request_dry_run_built",
        entity_type="import",
        entity_id=import_id,
        payload={
            "import_id": import_id,
            "event_id": event_id,
            "summary": summary,
            "orders": [
                {
                    "order_id": item.get("order_id"),
                    "status": item.get("status"),
                    "error": item.get("error"),
                    "payload_preview": item.get("payload") or {},
                }
                for item in dry_runs
            ],
        },
    ))
