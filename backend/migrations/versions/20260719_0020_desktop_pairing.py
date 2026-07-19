"""Add one-time desktop pairing state and persistent abuse limits."""

import sqlalchemy as sa
from alembic import op


revision = "20260719_0020"
down_revision = "20260716_0019"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.create_table(
        "desktop_pairings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("setup_code_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("device_label", sa.String(length=80), nullable=True),
        sa.Column("desktop_version", sa.String(length=40), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("principal_id", sa.Uuid(), nullable=True),
        sa.Column("token_id", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ack_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','redeemed_unacked','acked','expired','revoked')",
            name="ck_desktop_pairings_supported_status",
        ),
        sa.CheckConstraint("length(setup_code_digest) = 64", name="ck_desktop_pairings_setup_digest_length"),
        sa.CheckConstraint("expires_at > created_at", name="ck_desktop_pairings_expiry_after_creation"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["principal_id"], ["service_principals.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["token_id"], ["service_principal_tokens.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("setup_code_digest", name="uq_desktop_pairings_setup_code_digest"),
        sa.UniqueConstraint("principal_id", name="uq_desktop_pairings_principal_id"),
        sa.UniqueConstraint("token_id", name="uq_desktop_pairings_token_id"),
    )
    op.create_index("idx_desktop_pairings_status_expires", "desktop_pairings", ["status", "expires_at"])
    op.create_index("idx_desktop_pairings_creator_status", "desktop_pairings", ["created_by_user_id", "status"])
    op.create_table(
        "desktop_pairing_rate_limits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bucket_digest", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(bucket_digest) = 64", name="ck_desktop_pairing_rate_bucket_digest_length"),
        sa.CheckConstraint("attempts >= 0", name="ck_desktop_pairing_rate_attempts_nonnegative"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bucket_digest", name="uq_desktop_pairing_rate_limits_bucket"),
    )
    op.create_index("idx_desktop_pairing_rate_limits_updated", "desktop_pairing_rate_limits", ["updated_at"])
    op.create_table(
        "desktop_pairing_maintenance",
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_succeeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade():
    op.drop_table("desktop_pairing_maintenance")
    op.drop_index("idx_desktop_pairing_rate_limits_updated", table_name="desktop_pairing_rate_limits")
    op.drop_table("desktop_pairing_rate_limits")
    op.drop_index("idx_desktop_pairings_creator_status", table_name="desktop_pairings")
    op.drop_index("idx_desktop_pairings_status_expires", table_name="desktop_pairings")
    op.drop_table("desktop_pairings")
