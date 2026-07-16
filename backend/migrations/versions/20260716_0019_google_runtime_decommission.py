"""Retire legacy Google Sheets runtime events without deleting audit history."""

import uuid
from collections.abc import Mapping
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op


revision = "20260716_0019"
down_revision = "20260716_0018"
branch_labels = None
depends_on = None


LEGACY_EVENT_TYPE = "google_sheets_export"
ACTIVE_STATUSES = (
    "pending",
    "failed",
    "error",
    "processing",
    "blocked",
    "active",
    "waiting_shipment_date",
    "waiting_date_choice",
)
MARKER = "google_runtime_decommission"


def _tables():
    metadata = sa.MetaData()
    pending_events = sa.Table(
        "pending_events",
        metadata,
        sa.Column("id", sa.Uuid()),
        sa.Column("event_type", sa.String()),
        sa.Column("status", sa.String()),
        sa.Column("payload", sa.JSON()),
        sa.Column("last_error", sa.Text()),
        sa.Column("lease_owner", sa.String()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    audit_log = sa.Table(
        "audit_log",
        metadata,
        sa.Column("id", sa.Uuid()),
        sa.Column("actor_subject", sa.String()),
        sa.Column("action", sa.String()),
        sa.Column("entity_type", sa.String()),
        sa.Column("entity_id", sa.String()),
        sa.Column("payload", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    return pending_events, audit_log


def upgrade():
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.alter_column("orders", "source", server_default="backend", existing_type=sa.String(length=40), existing_nullable=False)
    pending_events, audit_log = _tables()
    now = datetime.now(timezone.utc)
    rows = bind.execute(
        sa.select(pending_events.c.id, pending_events.c.status, pending_events.c.payload)
        .where(pending_events.c.event_type == LEGACY_EVENT_TYPE)
        .where(pending_events.c.status.in_(ACTIVE_STATUSES))
    ).mappings().all()
    for row in rows:
        legacy_payload = row["payload"]
        if isinstance(legacy_payload, Mapping):
            payload = dict(legacy_payload)
        else:
            payload = {
                "legacy_payload": legacy_payload,
                "legacy_payload_type": type(legacy_payload).__name__,
            }
        payload[MARKER] = {
            "at": now.isoformat(),
            "reason": "postgres_is_the_only_runtime_source_of_truth",
            "previous_status": row["status"],
            "migration_revision": revision,
        }
        bind.execute(
            pending_events.update().where(pending_events.c.id == row["id"]).values(
                status="cancelled",
                payload=payload,
                last_error="Cancelled by PostgreSQL-only runtime cutover",
                lease_owner=None,
                lease_expires_at=None,
                completed_at=now,
                updated_at=now,
            )
        )
        bind.execute(audit_log.insert().values(
            id=uuid.uuid4(),
            actor_subject="migration:20260716_0019",
            action="google_runtime_event_cancelled",
            entity_type="pending_event",
            entity_id=str(row["id"]),
            payload={
                "event_type": LEGACY_EVENT_TYPE,
                "previous_status": row["status"],
                "new_status": "cancelled",
                "reason": "postgres_is_the_only_runtime_source_of_truth",
            },
            created_at=now,
        ))


def downgrade():
    raise RuntimeError(
        "20260716_0019 is forward-only: downgrading would reactivate the retired Google Sheets runtime"
    )
