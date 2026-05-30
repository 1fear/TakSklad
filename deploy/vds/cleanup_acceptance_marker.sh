#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${TAKSKLAD_ENV_FILE:-$SCRIPT_DIR/.env}"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

usage() {
  cat >&2 <<'EOF'
Usage:
  cleanup_acceptance_marker.sh MARKER [--apply]

Default mode is dry-run. Pass --apply to delete matching acceptance data.

Safety:
  MARKER must contain ACCEPTANCE, WEB_UI_SMOKE, or SMOKE_MVP.
EOF
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage
  exit 2
fi

MARKER="$1"
MODE="${2:-}"

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

APPLY="0"
if [[ "$MODE" == "--apply" ]]; then
  APPLY="1"
elif [[ -n "$MODE" ]]; then
  usage
  exit 2
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Env file not found: $ENV_FILE" >&2
  exit 1
fi

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T \
  -e MARKER="$MARKER" \
  -e APPLY="$APPLY" \
  backend-api python - <<'PY'
import json
import os

from sqlalchemy import String, cast, delete, or_, select

from app.db import SessionLocal
from app.models import AuditLog, ImportFile, ImportJob, Order, PendingEvent


marker = os.environ["MARKER"]
apply = os.environ.get("APPLY") == "1"


def query_ids(db, stmt):
    return [row[0] for row in db.execute(stmt).all()]


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


with SessionLocal() as db:
    order_ids = query_ids(db, select(Order.id).where(order_condition()))
    import_ids = query_ids(db, select(ImportJob.id).where(import_condition()))
    import_file_ids = query_ids(db, select(ImportFile.id).where(import_file_condition()))
    pending_event_ids = query_ids(db, select(PendingEvent.id).where(pending_event_condition()))
    audit_ids = query_ids(db, select(AuditLog.id).where(audit_condition(import_ids)))

    before = {
        "orders": len(order_ids),
        "imports": len(import_ids),
        "import_files": len(import_file_ids),
        "pending_events": len(pending_event_ids),
        "audit_log": len(audit_ids),
    }
    deleted = {key: 0 for key in before}

    if apply:
        if order_ids:
            deleted["orders"] = db.execute(delete(Order).where(Order.id.in_(order_ids))).rowcount or 0
        if import_ids:
            deleted["import_files"] += db.execute(delete(ImportFile).where(ImportFile.import_id.in_(import_ids))).rowcount or 0
            deleted["imports"] = db.execute(delete(ImportJob).where(ImportJob.id.in_(import_ids))).rowcount or 0
        if import_file_ids:
            deleted["import_files"] += db.execute(delete(ImportFile).where(ImportFile.id.in_(import_file_ids))).rowcount or 0
        if audit_ids:
            deleted["audit_log"] = db.execute(delete(AuditLog).where(AuditLog.id.in_(audit_ids))).rowcount or 0
        if pending_event_ids:
            deleted["pending_events"] = db.execute(delete(PendingEvent).where(PendingEvent.id.in_(pending_event_ids))).rowcount or 0
        db.commit()

    remaining_import_ids = query_ids(db, select(ImportJob.id).where(import_condition()))
    remaining = {
        "orders": len(query_ids(db, select(Order.id).where(order_condition()))),
        "imports": len(remaining_import_ids),
        "import_files": len(query_ids(db, select(ImportFile.id).where(import_file_condition()))),
        "pending_events": len(query_ids(db, select(PendingEvent.id).where(pending_event_condition()))),
        "audit_log": len(query_ids(db, select(AuditLog.id).where(audit_condition(remaining_import_ids)))),
    }

    print(json.dumps({
        "marker": marker,
        "mode": "apply" if apply else "dry-run",
        "before": before,
        "deleted": deleted,
        "remaining": remaining,
    }, ensure_ascii=False, sort_keys=True))
PY
