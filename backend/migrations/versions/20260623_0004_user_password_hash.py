"""add web user password hashes

Revision ID: 20260623_0004
Revises: 20260623_0003
Create Date: 2026-06-23 14:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = "20260623_0004"
down_revision = "20260623_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("password_hash", sa.String(length=255), nullable=True))
    op.create_index("idx_users_role_active", "users", ["role", "is_active"])


def downgrade():
    raise RuntimeError(
        "User password migration is forward-only. Restore from backup or create a forward repair migration."
    )
