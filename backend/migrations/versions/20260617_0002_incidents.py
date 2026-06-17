"""add incident tracking

Revision ID: 20260617_0002
Revises: 20260616_0001
Create Date: 2026-06-17 12:45:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260617_0002"
down_revision = "20260616_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "incidents",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("severity", sa.String(length=40), server_default="warning", nullable=False),
        sa.Column("status", sa.String(length=40), server_default="open", nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("entity_type", sa.String(length=80), nullable=True),
        sa.Column("entity_id", sa.String(length=120), nullable=True),
        sa.Column("pending_event_id", sa.Uuid(), nullable=True),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("order_item_id", sa.Uuid(), nullable=True),
        sa.Column("import_id", sa.Uuid(), nullable=True),
        sa.Column("scan_code_id", sa.Uuid(), nullable=True),
        sa.Column("external_ref", sa.String(length=180), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["pending_event_id"], ["pending_events.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["import_id"], ["imports.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["scan_code_id"], ["scan_codes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_incidents_status_severity", "incidents", ["status", "severity"])
    op.create_index("idx_incidents_source", "incidents", ["source"])
    op.create_index("idx_incidents_entity_type", "incidents", ["entity_type"])
    op.create_index("idx_incidents_created_at", "incidents", ["created_at"])
    op.create_index("idx_incidents_pending_event_id", "incidents", ["pending_event_id"])
    op.create_index("idx_incidents_order_id", "incidents", ["order_id"])
    op.create_index("idx_incidents_order_item_id", "incidents", ["order_item_id"])
    op.create_index("idx_incidents_import_id", "incidents", ["import_id"])
    op.create_index("idx_incidents_scan_code_id", "incidents", ["scan_code_id"])


def downgrade():
    raise RuntimeError(
        "Incident migration is forward-only. Restore from backup or create a forward repair migration."
    )
