"""Add indexes justified by bounded hot-query access paths."""

from alembic import op
import sqlalchemy as sa


revision = "20260710_0014"
down_revision = "20260710_0013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "idx_orders_active_page",
        "orders",
        ["order_date", "created_at", "id"],
        postgresql_where=sa.text(
            "status NOT IN ('completed','done','closed','returned','archived_no_kiz','cancelled')"
        ),
    )
    op.create_index(
        "idx_pending_events_claim_ordered",
        "pending_events",
        ["event_type", "available_at", "created_at", "id"],
        postgresql_include=["status", "lease_expires_at"],
    )


def downgrade():
    op.drop_index("idx_pending_events_claim_ordered", table_name="pending_events")
    op.drop_index("idx_orders_active_page", table_name="orders")
