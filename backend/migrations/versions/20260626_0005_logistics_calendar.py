"""add logistics calendar days

Revision ID: 20260626_0005
Revises: 20260623_0004
Create Date: 2026-06-26 12:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260626_0005"
down_revision = "20260623_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "logistics_calendar_days",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("service_date", sa.Date(), nullable=False),
        sa.Column("is_non_working", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=40), server_default="manual", nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("service_date", name="uq_logistics_calendar_days_service_date"),
    )
    op.create_index("idx_logistics_calendar_days_service_date", "logistics_calendar_days", ["service_date"])


def downgrade():
    raise RuntimeError(
        "Logistics calendar migration is forward-only. Restore from backup or create a forward repair migration."
    )
