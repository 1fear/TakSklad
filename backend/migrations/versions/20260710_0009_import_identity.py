"""materialize normalized import identity

Revision ID: 20260710_0009
Revises: 20260710_0008
Create Date: 2026-07-10 09:00:00
"""

import sqlalchemy as sa
from alembic import op


revision = "20260710_0009"
down_revision = "20260710_0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("orders", sa.Column("import_order_key", sa.String(length=120), nullable=True))
    op.add_column("orders", sa.Column("import_source_order_key", sa.String(length=120), nullable=True))
    op.add_column("order_items", sa.Column("import_item_key", sa.String(length=64), nullable=True))
    op.add_column("order_items", sa.Column("source_import_key", sa.String(length=64), nullable=True))
    op.add_column("order_items", sa.Column("source_import_id", sa.Text(), nullable=True))
    op.add_column("order_items", sa.Column("source_batch_key", sa.Text(), nullable=True))

    op.create_index(
        "idx_orders_import_order_key_status",
        "orders",
        ["import_order_key", "status"],
    )
    op.create_index(
        "idx_orders_import_source_order_key_status",
        "orders",
        ["import_source_order_key", "status"],
    )
    op.create_index("idx_order_items_import_item_key", "order_items", ["import_item_key"])
    op.create_index("idx_order_items_source_import_key", "order_items", ["source_import_key"])


def downgrade():
    raise RuntimeError(
        "Import identity expand migration is forward-only. Restore from backup or create a forward repair migration."
    )
