"""Add logistics region points directory for city/region report split."""

import sqlalchemy as sa
from alembic import op


revision = "20260804_0021"
down_revision = "20260719_0020"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.create_table(
        "logistics_region_points",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_name", sa.String(length=255), nullable=False),
        sa.Column("normalized_client", sa.String(length=255), nullable=False),
        sa.Column("latitude", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column("longitude", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column("agent", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_logistics_region_points_latitude"),
        sa.CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_logistics_region_points_longitude"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "normalized_client", "latitude", "longitude",
            name="uq_logistics_region_points_client_coordinates",
        ),
    )
    op.create_index(
        "idx_logistics_region_points_client",
        "logistics_region_points",
        ["normalized_client"],
    )


def downgrade():
    op.drop_index("idx_logistics_region_points_client", table_name="logistics_region_points")
    op.drop_table("logistics_region_points")
