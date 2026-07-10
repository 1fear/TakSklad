"""add revocable user sessions and scoped service identities

Revision ID: 20260710_0012
Revises: 20260710_0011
Create Date: 2026-07-10 15:00:00
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260710_0012"
down_revision = "20260710_0011"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '30s'")

    op.add_column(
        "users",
        sa.Column("auth_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "users",
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_check_constraint("ck_users_auth_version_positive", "users", "auth_version > 0")

    op.execute("""
        CREATE FUNCTION taksklad_bump_user_auth_version() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.password_hash IS DISTINCT FROM OLD.password_hash
               OR NEW.role IS DISTINCT FROM OLD.role
               OR NEW.is_active IS DISTINCT FROM OLD.is_active THEN
                NEW.auth_version := GREATEST(NEW.auth_version, OLD.auth_version + 1);
            END IF;
            IF NEW.auth_version IS DISTINCT FROM OLD.auth_version THEN
                NEW.updated_at := now();
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_users_bump_auth_version
        BEFORE UPDATE OF password_hash, role, is_active, auth_version ON users
        FOR EACH ROW EXECUTE FUNCTION taksklad_bump_user_auth_version()
    """)

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("subject", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=40), nullable=False),
        sa.Column("auth_version", sa.Integer(), nullable=False),
        sa.Column("auth_state_digest", sa.String(length=64), nullable=False),
        sa.Column("session_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("auth_version > 0", name="ck_auth_sessions_auth_version_positive"),
        sa.CheckConstraint("trim(subject) <> ''", name="ck_auth_sessions_subject_nonblank"),
        sa.CheckConstraint("trim(role) <> ''", name="ck_auth_sessions_role_nonblank"),
        sa.CheckConstraint("length(auth_state_digest) = 64", name="ck_auth_sessions_auth_state_digest_length"),
        sa.CheckConstraint("length(session_digest) = 64", name="ck_auth_sessions_session_digest_length"),
        sa.CheckConstraint("expires_at > created_at", name="ck_auth_sessions_expiry_after_creation"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_digest", name="uq_auth_sessions_session_digest"),
    )
    op.create_index(
        "idx_auth_sessions_user_active",
        "auth_sessions",
        ["user_id", "revoked_at", "expires_at"],
    )
    op.create_index("idx_auth_sessions_expires_at", "auth_sessions", ["expires_at"])

    op.create_table(
        "service_principals",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("identifier", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column(
            "scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("trim(identifier) <> ''", name="ck_service_principals_identifier_nonblank"),
        sa.CheckConstraint(
            "kind IN ('desktop','worker','acceptance')",
            name="ck_service_principals_supported_kind",
        ),
        sa.CheckConstraint("jsonb_typeof(scopes) = 'array'", name="ck_service_principals_scopes_array"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identifier", name="uq_service_principals_identifier"),
    )
    op.create_index(
        "idx_service_principals_kind_active",
        "service_principals",
        ["kind", "is_active"],
    )
    op.create_index("idx_service_principals_expires_at", "service_principals", ["expires_at"])

    op.create_table(
        "service_principal_tokens",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_token_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("length(token_digest) = 64", name="ck_service_principal_tokens_digest_length"),
        sa.CheckConstraint("expires_at > issued_at", name="ck_service_principal_tokens_expiry_after_issue"),
        sa.ForeignKeyConstraint(["principal_id"], ["service_principals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["replaced_by_token_id"],
            ["service_principal_tokens.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_service_principal_tokens_token_digest"),
    )
    op.create_index(
        "idx_service_principal_tokens_principal_active",
        "service_principal_tokens",
        ["principal_id", "revoked_at", "expires_at"],
    )
    op.create_index(
        "idx_service_principal_tokens_expires_at",
        "service_principal_tokens",
        ["expires_at"],
    )

    op.add_column("audit_log", sa.Column("actor_service_principal_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_audit_log_actor_service_principal_id",
        "audit_log",
        "service_principals",
        ["actor_service_principal_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "idx_audit_log_actor_service_principal_id",
        "audit_log",
        ["actor_service_principal_id"],
    )


def downgrade():
    raise RuntimeError(
        "Auth identity migration is forward-only. The expanded schema remains compatible with the previous application."
    )
