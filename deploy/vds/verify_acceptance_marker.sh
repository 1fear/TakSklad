#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${TAKSKLAD_ENV_FILE:-$SCRIPT_DIR/.env}"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

usage() {
  cat >&2 <<'EOF'
Usage:
  verify_acceptance_marker.sh MARKER [--expect-orders N] [--expect-scans N] [--expect-completed]

Read-only VDS acceptance verifier.

Safety:
  MARKER must contain ACCEPTANCE, WEB_UI_SMOKE, or SMOKE_MVP.
EOF
}

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

MARKER="$1"
shift
EXPECT_ORDERS=""
EXPECT_SCANS=""
EXPECT_COMPLETED="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --expect-orders)
      if [[ $# -lt 2 || ! "$2" =~ ^[0-9]+$ ]]; then
        usage
        exit 2
      fi
      EXPECT_ORDERS="$2"
      shift 2
      ;;
    --expect-scans)
      if [[ $# -lt 2 || ! "$2" =~ ^[0-9]+$ ]]; then
        usage
        exit 2
      fi
      EXPECT_SCANS="$2"
      shift 2
      ;;
    --expect-completed)
      EXPECT_COMPLETED="1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$MARKER" || ${#MARKER} -lt 8 ]]; then
  echo "Marker is too short." >&2
  exit 2
fi

case "$MARKER" in
  *ACCEPTANCE*|*WEB_UI_SMOKE*|*SMOKE_MVP*) ;;
  *)
    echo "Refusing unsafe marker: $MARKER" >&2
    echo "Marker must contain ACCEPTANCE, WEB_UI_SMOKE, or SMOKE_MVP." >&2
    exit 2
    ;;
esac

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Env file not found: $ENV_FILE" >&2
  exit 1
fi

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T \
  -e MARKER="$MARKER" \
  -e EXPECT_ORDERS="$EXPECT_ORDERS" \
  -e EXPECT_SCANS="$EXPECT_SCANS" \
  -e EXPECT_COMPLETED="$EXPECT_COMPLETED" \
  backend-api python - <<'PY'
import json
import os
import sys

from sqlalchemy import String, cast, or_, select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.models import AuditLog, ImportFile, ImportJob, Order, OrderItem, PendingEvent
from app.orders_service import COMPLETED_STATUSES


marker = os.environ["MARKER"]
expect_orders = os.environ.get("EXPECT_ORDERS")
expect_scans = os.environ.get("EXPECT_SCANS")
expect_completed = os.environ.get("EXPECT_COMPLETED") == "1"


def order_condition():
    return or_(
        Order.client == marker,
        Order.external_id == marker,
        cast(Order.raw_payload, String).contains(marker),
    )


def import_condition():
    return cast(ImportJob.raw_payload, String).contains(marker)


def import_file_condition():
    return ImportFile.filename.contains(marker)


def pending_event_condition():
    return cast(PendingEvent.payload, String).contains(marker)


def audit_condition(import_ids):
    condition = cast(AuditLog.payload, String).contains(marker)
    if import_ids:
        condition = or_(condition, AuditLog.entity_id.in_([str(value) for value in import_ids]))
    return condition


def query_count(db, stmt):
    return len(db.execute(stmt).all())


def parse_expected_int(value):
    if value in (None, ""):
        return None
    return int(value)


with SessionLocal() as db:
    orders = db.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.scan_codes))
        .where(order_condition())
        .order_by(Order.created_at.asc())
    ).scalars().all()
    imports = db.execute(select(ImportJob).where(import_condition())).scalars().all()
    import_ids = [row.id for row in imports]

    items = [item for order in orders for item in order.items]
    scan_codes = [scan.code for item in items for scan in item.scan_codes]
    planned_blocks = sum(max(0, item.quantity_blocks or 0) for item in items)
    scanned_blocks = sum(max(0, item.scanned_blocks or 0) for item in items)
    active_orders = [order for order in orders if order.status not in COMPLETED_STATUSES]
    completed_orders = [order for order in orders if order.status in COMPLETED_STATUSES]
    incomplete_items = [
        {
            "order_id": str(item.order_id),
            "product": item.product,
            "planned_blocks": item.quantity_blocks,
            "scanned_blocks": item.scanned_blocks,
            "status": item.status,
        }
        for item in items
        if item.status not in COMPLETED_STATUSES or (item.quantity_blocks or 0) > (item.scanned_blocks or 0)
    ]
    source_files = sorted({
        str((item.raw_payload or {}).get("source_file") or "").strip()
        for item in items
        if str((item.raw_payload or {}).get("source_file") or "").strip()
    })
    order_dates = sorted({
        order.order_date.isoformat()
        for order in orders
        if order.order_date
    })
    missing_coordinates = [
        order.client
        for order in orders
        if not str((order.raw_payload or {}).get("coordinates") or "").strip()
    ]
    import_rows = sum(row.rows_imported or 0 for row in imports)

    summary = {
        "marker": marker,
        "orders": len(orders),
        "active_orders": len(active_orders),
        "completed_orders": len(completed_orders),
        "items": len(items),
        "planned_blocks": planned_blocks,
        "scanned_blocks": scanned_blocks,
        "scan_codes": len(scan_codes),
        "imports": len(imports),
        "import_rows": import_rows,
        "import_statuses": sorted({row.status for row in imports}),
        "import_files": query_count(db, select(ImportFile.id).where(import_file_condition())),
        "pending_events": query_count(db, select(PendingEvent.id).where(pending_event_condition())),
        "audit_log": query_count(db, select(AuditLog.id).where(audit_condition(import_ids))),
        "source_files": source_files,
        "order_dates": order_dates,
        "missing_coordinates": missing_coordinates,
        "incomplete_items": incomplete_items,
    }

    errors = []
    expected_orders = parse_expected_int(expect_orders)
    expected_scans = parse_expected_int(expect_scans)
    if expected_orders is not None and summary["orders"] != expected_orders:
        errors.append(f"orders expected {expected_orders}, got {summary['orders']}")
    if expected_scans is not None and summary["scan_codes"] != expected_scans:
        errors.append(f"scan_codes expected {expected_scans}, got {summary['scan_codes']}")
    if expect_completed:
        if not orders:
            errors.append("completed expected, but marker has no orders")
        if active_orders:
            errors.append(f"active_orders expected 0, got {len(active_orders)}")
        if incomplete_items:
            errors.append(f"incomplete_items expected 0, got {len(incomplete_items)}")
        if planned_blocks != scanned_blocks:
            errors.append(f"planned_blocks/scanned_blocks mismatch: {planned_blocks}/{scanned_blocks}")

    summary["status"] = "failed" if errors else "ok"
    summary["errors"] = errors
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if errors:
        sys.exit(3)
PY
