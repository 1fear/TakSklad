"""Reduce pending-event claim write amplification."""

from alembic import op


revision = "20260711_0016"
down_revision = "20260711_0015"
branch_labels = None
depends_on = None


def upgrade():
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_pending_events_claim_ordered_v2 "
            "ON pending_events (event_type, available_at, created_at, id)"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_pending_events_claim")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_pending_events_claim_ordered")
        op.execute(
            "ALTER INDEX IF EXISTS idx_pending_events_claim_ordered_v2 "
            "RENAME TO idx_pending_events_claim_ordered"
        )


def downgrade():
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_pending_events_claim "
            "ON pending_events (event_type, status, available_at, created_at, id)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_pending_events_claim_ordered_v1 "
            "ON pending_events (event_type, available_at, created_at, id) "
            "INCLUDE (status, lease_expires_at)"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_pending_events_claim_ordered")
        op.execute(
            "ALTER INDEX IF EXISTS idx_pending_events_claim_ordered_v1 "
            "RENAME TO idx_pending_events_claim_ordered"
        )
