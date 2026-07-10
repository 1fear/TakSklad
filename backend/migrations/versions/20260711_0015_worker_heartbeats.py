"""Persist bounded worker main-loop heartbeats."""

from alembic import op
import sqlalchemy as sa


revision = "20260711_0015"
down_revision = "20260710_0014"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_name", sa.String(length=80), primary_key=True),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("grace_seconds", sa.Integer(), server_default="15", nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("correlation_id", sa.String(length=36), nullable=False),
        sa.Column("last_cycle_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_class", sa.String(length=80), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("interval_seconds > 0", name="ck_worker_heartbeats_interval_positive"),
        sa.CheckConstraint("grace_seconds >= 0", name="ck_worker_heartbeats_grace_nonnegative"),
        sa.CheckConstraint(
            "status IN ('running','success','failed')",
            name="ck_worker_heartbeats_supported_status",
        ),
    )
    op.create_index("idx_worker_heartbeats_updated_at", "worker_heartbeats", ["updated_at"])


def downgrade():
    op.drop_index("idx_worker_heartbeats_updated_at", table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")
