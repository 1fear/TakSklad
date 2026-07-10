"""materialize atomic outbox action and aggregate identity

Revision ID: 20260710_0011
Revises: 20260710_0010
Create Date: 2026-07-10 11:00:00
"""

import sqlalchemy as sa
from alembic import op


revision = "20260710_0011"
down_revision = "20260710_0010"
branch_labels = None
depends_on = None


def ensure_concurrent_index():
    bind = op.get_bind()
    row = bind.execute(sa.text(
        "SELECT i.indisvalid,pg_get_indexdef(i.indexrelid) "
        "FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
        "WHERE c.relname='idx_pending_events_action_aggregate_status'"
    )).first()
    if row is not None and not row[0]:
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_pending_events_action_aggregate_status")
        row = None
    if row is not None:
        definition = str(row[1] or "").lower()
        required = ("action", "aggregate_type", "aggregate_id", "status", "created_at", "id")
        if not all(fragment in definition for fragment in required):
            raise RuntimeError("existing atomic outbox index has unexpected definition")
        return
    op.execute(
        "CREATE INDEX CONCURRENTLY idx_pending_events_action_aggregate_status "
        "ON pending_events (action, aggregate_type, aggregate_id, status, created_at, id)"
    )


def add_column_if_missing(name, column):
    bind = op.get_bind()
    exists = bind.execute(sa.text("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name='pending_events' AND column_name=:name
    """), {"name": name}).first()
    if exists is None:
        op.add_column("pending_events", column)


def upgrade():
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    add_column_if_missing("action", sa.Column("action", sa.String(length=80), nullable=True))
    add_column_if_missing("aggregate_type", sa.Column("aggregate_type", sa.String(length=80), nullable=True))
    add_column_if_missing("aggregate_id", sa.Column("aggregate_id", sa.String(length=180), nullable=True))
    op.execute("""
        UPDATE pending_events
        SET action = left(coalesce(nullif(btrim(payload->>'action'), ''), event_type), 80),
            aggregate_type = left(coalesce(
                nullif(btrim(payload->>'entity_type'), ''),
                CASE WHEN nullif(btrim(payload->>'order_id'), '') IS NOT NULL THEN 'order'
                     WHEN nullif(btrim(payload->>'import_id'), '') IS NOT NULL THEN 'import'
                     ELSE 'pending_event' END
            ), 80),
            aggregate_id = left(coalesce(
                nullif(btrim(payload->>'entity_id'), ''),
                nullif(btrim(payload->>'order_id'), ''),
                nullif(btrim(payload->>'import_id'), ''),
                id::text
            ), 180)
        WHERE action IS NULL OR aggregate_type IS NULL OR aggregate_id IS NULL
    """)
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '2s'")
        op.execute("SET statement_timeout = '30s'")
        ensure_concurrent_index()


def downgrade():
    raise RuntimeError(
        "Atomic outbox migration is forward-only. Restore from backup or create a forward repair migration."
    )
