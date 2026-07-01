"""add pending event queue indexes

Revision ID: 20260701_0007
Revises: 20260701_0006
Create Date: 2026-07-01 22:30:00
"""
from alembic import op


revision = "20260701_0007"
down_revision = "20260701_0006"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_events_status_created_at "
        "ON pending_events(status, created_at, id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_events_status_updated_at "
        "ON pending_events(status, updated_at, id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_events_type_status_created_at "
        "ON pending_events(event_type, status, created_at, id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_events_type_status_updated_at "
        "ON pending_events(event_type, status, updated_at, id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_events_updated_created_at "
        "ON pending_events(updated_at, created_at, id)"
    )


def downgrade():
    raise RuntimeError(
        "Pending event queue index migration is forward-only. Restore from backup or create a forward repair migration."
    )
