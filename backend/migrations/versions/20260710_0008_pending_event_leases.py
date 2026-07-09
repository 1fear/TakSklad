"""add owner-scoped pending event leases

Revision ID: 20260710_0008
Revises: 20260701_0007
Create Date: 2026-07-10 01:40:00
"""
import sqlalchemy as sa
from alembic import op


revision = "20260710_0008"
down_revision = "20260701_0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "pending_events",
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.add_column("pending_events", sa.Column("lease_owner", sa.String(length=160), nullable=True))
    op.add_column("pending_events", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("pending_events", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "UPDATE pending_events "
        "SET available_at = CASE "
        "WHEN payload ? 'next_attempt_at' "
        "AND pg_input_is_valid(payload ->> 'next_attempt_at', 'timestamp with time zone') "
        "THEN (payload ->> 'next_attempt_at')::timestamptz "
        "ELSE COALESCE(created_at, now()) END, "
        "lease_owner = CASE WHEN status = 'processing' THEN 'legacy-expired' ELSE NULL END, "
        "lease_expires_at = CASE WHEN status = 'processing' THEN now() ELSE NULL END"
    )
    op.create_index(
        "idx_pending_events_claim",
        "pending_events",
        ["event_type", "status", "available_at", "created_at", "id"],
    )
    op.create_index(
        "idx_pending_events_lease_expiry",
        "pending_events",
        ["status", "lease_expires_at", "id"],
    )


def downgrade():
    raise RuntimeError(
        "Pending event lease migration is forward-only. Restore from backup or create a forward repair migration."
    )
