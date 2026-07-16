"""Track real worker progress without masking hung operations."""

import sqlalchemy as sa
from alembic import op


revision = "20260716_0018"
down_revision = "20260715_0017"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.add_column(
        "worker_heartbeats",
        sa.Column(
            "last_progress_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.add_column(
        "worker_heartbeats",
        sa.Column("last_progress_phase", sa.String(length=120), nullable=True),
    )
    op.execute(
        "UPDATE worker_heartbeats "
        "SET last_progress_at = last_cycle_started_at"
    )


def downgrade():
    op.drop_column("worker_heartbeats", "last_progress_phase")
    op.drop_column("worker_heartbeats", "last_progress_at")
