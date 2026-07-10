"""add authenticated audit subject

Revision ID: 20260710_0013
Revises: 20260710_0012
Create Date: 2026-07-10 16:30:00
"""

import sqlalchemy as sa
from alembic import op


revision = "20260710_0013"
down_revision = "20260710_0012"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.add_column("audit_log", sa.Column("actor_subject", sa.String(length=120), nullable=True))
    op.create_check_constraint(
        "ck_audit_log_single_authenticated_actor",
        "audit_log",
        "actor_user_id IS NULL OR actor_service_principal_id IS NULL",
    )
    op.create_check_constraint(
        "ck_audit_log_actor_subject_nonblank",
        "audit_log",
        "actor_subject IS NULL OR btrim(actor_subject) <> ''",
    )
    op.create_index("idx_audit_log_actor_subject", "audit_log", ["actor_subject"])


def downgrade():
    op.drop_index("idx_audit_log_actor_subject", table_name="audit_log")
    op.drop_constraint("ck_audit_log_actor_subject_nonblank", "audit_log", type_="check")
    op.drop_constraint("ck_audit_log_single_authenticated_actor", "audit_log", type_="check")
    op.drop_column("audit_log", "actor_subject")
