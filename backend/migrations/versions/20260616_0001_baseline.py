"""baseline current schema

Revision ID: 20260616_0001
Revises:
Create Date: 2026-06-16 16:25:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260616_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("username", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=40), server_default="operator", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source", sa.String(length=40), server_default="google_sheets", nullable=False),
        sa.Column("external_id", sa.String(length=120), nullable=True),
        sa.Column("order_date", sa.Date(), nullable=True),
        sa.Column("payment_type", sa.String(length=120), nullable=False),
        sa.Column("client", sa.String(length=255), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("representative", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=40), server_default="not_completed", nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "imports",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source", sa.String(length=40), server_default="excel", nullable=False),
        sa.Column("status", sa.String(length=40), server_default="created", nullable=False),
        sa.Column("rows_total", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rows_imported", sa.Integer(), server_default="0", nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "order_items",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("product", sa.String(length=255), nullable=False),
        sa.Column("quantity_pieces", sa.Integer(), server_default="0", nullable=False),
        sa.Column("quantity_blocks", sa.Integer(), server_default="0", nullable=False),
        sa.Column("pieces_per_block", sa.Integer(), nullable=True),
        sa.Column("scanned_blocks", sa.Integer(), server_default="0", nullable=False),
        sa.Column("requires_kiz", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("status", sa.String(length=40), server_default="not_completed", nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "import_files",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("import_id", sa.Uuid(), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["import_id"], ["imports.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sha256", name="uq_import_files_sha256"),
    )
    op.create_table(
        "scan_codes",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("order_item_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=40), server_default="desktop", nullable=False),
        sa.Column("workstation_id", sa.String(length=120), nullable=True),
        sa.Column("scanned_by", sa.String(length=120), nullable=True),
        sa.Column("scanned_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "kiz_codes",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_kiz_codes_code"),
    )
    op.create_table(
        "pending_events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=180), nullable=True),
        sa.Column("status", sa.String(length=40), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "kiz_movements",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("kiz_id", sa.Uuid(), nullable=False),
        sa.Column("movement_type", sa.String(length=40), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("order_item_id", sa.Uuid(), nullable=True),
        sa.Column("scan_code_id", sa.Uuid(), nullable=True),
        sa.Column("return_reference", sa.String(length=120), nullable=True),
        sa.Column("source", sa.String(length=40), server_default="backend", nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=True),
        sa.Column("workstation_id", sa.String(length=120), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.ForeignKeyConstraint(["kiz_id"], ["kiz_codes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["scan_code_id"], ["scan_codes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=True),
        sa.Column("entity_id", sa.String(length=120), nullable=True),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index("idx_orders_status_date", "orders", ["status", "order_date"])
    op.create_index("idx_order_items_order_id", "order_items", ["order_id"])
    op.create_index("idx_scan_codes_order_item_id", "scan_codes", ["order_item_id"])
    op.create_index("idx_scan_codes_code", "scan_codes", ["code"])
    op.create_index("idx_scan_codes_code_order_item_id", "scan_codes", ["code", "order_item_id"])
    op.create_index("idx_kiz_movements_kiz_id_occurred_at", "kiz_movements", ["kiz_id", "occurred_at"])
    op.create_index("idx_kiz_movements_order_id", "kiz_movements", ["order_id"])
    op.create_index("idx_kiz_movements_order_item_id", "kiz_movements", ["order_item_id"])
    op.create_index("idx_kiz_movements_scan_code_id", "kiz_movements", ["scan_code_id"])
    op.create_index("idx_import_files_sha256", "import_files", ["sha256"])
    op.create_index("idx_pending_events_status", "pending_events", ["status"])
    op.create_index("uq_pending_events_idempotency_key", "pending_events", ["idempotency_key"], unique=True)
    op.create_index("idx_audit_log_created_at", "audit_log", ["created_at"])


def downgrade():
    raise RuntimeError(
        "Baseline migration is irreversible. Restore from backup or create a forward repair migration."
    )
