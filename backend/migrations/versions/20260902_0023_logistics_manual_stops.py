"""Add manual logistics stops added by hand from the calendar tab."""

import sqlalchemy as sa
from alembic import op


revision = "20260902_0023"
down_revision = "20260817_0022"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.create_table(
        "logistics_manual_stops",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("service_date", sa.Date(), nullable=False),
        sa.Column("client_name", sa.String(length=255), nullable=False),
        sa.Column("point_name", sa.String(length=255), nullable=True),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("coordinates", sa.Text(), nullable=False),
        sa.Column("representative", sa.String(length=255), nullable=True),
        sa.Column("delivery_from", sa.String(length=5), nullable=False, server_default="10:00"),
        sa.Column("delivery_to", sa.String(length=5), nullable=False, server_default="18:00"),
        sa.Column("blocks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=True),
        sa.Column("raw_payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("blocks >= 0", name="ck_logistics_manual_stops_blocks_nonnegative"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_logistics_manual_stops_service_date",
        "logistics_manual_stops",
        ["service_date", "is_active"],
    )


def downgrade():
    op.drop_index("idx_logistics_manual_stops_service_date", table_name="logistics_manual_stops")
    op.drop_table("logistics_manual_stops")
