"""add representative contacts

Revision ID: 20260701_0006
Revises: 20260626_0005
Create Date: 2026-07-01 12:30:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260701_0006"
down_revision = "20260626_0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "representative_contacts",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("work_phone", sa.String(length=80), nullable=True),
        sa.Column("personal_phone", sa.String(length=80), nullable=True),
        sa.Column("work_zone", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("normalized_name", name="uq_representative_contacts_normalized_name"),
    )
    op.create_index(
        "idx_representative_contacts_normalized_name",
        "representative_contacts",
        ["normalized_name"],
    )


def downgrade():
    raise RuntimeError(
        "Representative contacts migration is forward-only. Restore from backup or create a forward repair migration."
    )
