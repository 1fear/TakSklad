"""add client delivery points

Revision ID: 20260623_0003
Revises: 20260617_0002
Create Date: 2026-06-23 12:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260623_0003"
down_revision = "20260617_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "client_points",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("client_name", sa.String(length=255), nullable=False),
        sa.Column("point_name", sa.String(length=255), nullable=True),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("normalized_client", sa.String(length=255), nullable=False),
        sa.Column("normalized_address", sa.Text(), nullable=False),
        sa.Column("coordinates", sa.Text(), nullable=True),
        sa.Column("representative", sa.String(length=255), nullable=True),
        sa.Column("delivery_from", sa.String(length=5), server_default="10:00", nullable=False),
        sa.Column("delivery_to", sa.String(length=5), server_default="18:00", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("normalized_client", "normalized_address", name="uq_client_points_normalized"),
    )
    op.create_index("idx_client_points_normalized", "client_points", ["normalized_client", "normalized_address"])
    op.create_index("idx_client_points_timeslot", "client_points", ["delivery_from", "delivery_to"])


def downgrade():
    raise RuntimeError(
        "Client points migration is forward-only. Restore from backup or create a forward repair migration."
    )
