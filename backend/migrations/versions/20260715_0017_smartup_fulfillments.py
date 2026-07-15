"""Add durable Smartup fulfillment workflow and canonical order mappings."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260715_0017"
down_revision = "20260711_0016"
branch_labels = None
depends_on = None


FULFILLMENT_STATES = (
    "'local_ready','smartup_write_started','smartup_confirmed','skladbot_create_queued',"
    "'skladbot_post_started','skladbot_created','smartup_ambiguous','skladbot_ambiguous',"
    "'blocked_validation','blocked_stock','payload_mismatch','manual_review','cancelled'"
)


def upgrade():
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    op.create_table(
        "smartup_fulfillments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workflow_key", sa.String(length=180), nullable=False),
        sa.Column("source_scope", sa.String(length=160), nullable=False),
        sa.Column("deal_id", sa.String(length=180), nullable=False),
        sa.Column("request_type", sa.String(length=60), server_default="shipment", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("target_status", sa.String(length=80), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=40), server_default="local_ready", nullable=False),
        sa.Column("retry_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reconciliation_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("state_changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("canonical_import_id", sa.Uuid(), nullable=True),
        sa.Column("legacy_saga_event_id", sa.Uuid(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            f"state IN ({FULFILLMENT_STATES})",
            name="ck_smartup_fulfillments_supported_state",
        ),
        sa.CheckConstraint("revision > 0", name="ck_smartup_fulfillments_revision_positive"),
        sa.CheckConstraint(
            "retry_attempts >= 0 AND reconciliation_attempts >= 0",
            name="ck_smartup_fulfillments_attempts_nonnegative",
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name="ck_smartup_fulfillments_payload_hash_length",
        ),
        sa.CheckConstraint(
            "btrim(workflow_key) <> '' AND btrim(source_scope) <> '' AND btrim(deal_id) <> '' "
            "AND btrim(request_type) <> '' AND btrim(target_status) <> ''",
            name="ck_smartup_fulfillments_identity_nonblank",
        ),
        sa.ForeignKeyConstraint(["canonical_import_id"], ["imports.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["legacy_saga_event_id"], ["pending_events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_scope",
            "deal_id",
            "request_type",
            "revision",
            name="uq_smartup_fulfillments_business_identity",
        ),
        sa.UniqueConstraint("workflow_key", name="uq_smartup_fulfillments_workflow_key"),
        sa.UniqueConstraint("legacy_saga_event_id", name="uq_smartup_fulfillments_legacy_saga_event"),
    )
    op.create_index(
        "idx_smartup_fulfillments_state_available",
        "smartup_fulfillments",
        ["state", "available_at", "id"],
    )
    op.create_index(
        "idx_smartup_fulfillments_deal",
        "smartup_fulfillments",
        ["source_scope", "deal_id", "revision"],
    )

    op.create_table(
        "smartup_fulfillment_orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("fulfillment_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("skladbot_event_id", sa.Uuid(), nullable=True),
        sa.Column("remote_request_id", sa.String(length=180), nullable=True),
        sa.Column("state", sa.String(length=40), server_default="pending", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "remote_request_id IS NULL OR btrim(remote_request_id) <> ''",
            name="ck_smartup_fulfillment_orders_remote_request_nonblank",
        ),
        sa.CheckConstraint(
            "state IN ('pending','create_queued','post_started','created','ambiguous','blocked_stock','manual_review')",
            name="ck_smartup_fulfillment_orders_supported_state",
        ),
        sa.ForeignKeyConstraint(["fulfillment_id"], ["smartup_fulfillments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["skladbot_event_id"], ["pending_events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "fulfillment_id",
            "order_id",
            name="uq_smartup_fulfillment_orders_mapping",
        ),
        sa.UniqueConstraint("skladbot_event_id", name="uq_smartup_fulfillment_orders_skladbot_event"),
        sa.UniqueConstraint("remote_request_id", name="uq_smartup_fulfillment_orders_remote_request"),
    )
    op.create_index(
        "idx_smartup_fulfillment_orders_order",
        "smartup_fulfillment_orders",
        ["order_id", "fulfillment_id"],
    )


def downgrade():
    op.drop_index("idx_smartup_fulfillment_orders_order", table_name="smartup_fulfillment_orders")
    op.drop_table("smartup_fulfillment_orders")
    op.drop_index("idx_smartup_fulfillments_deal", table_name="smartup_fulfillments")
    op.drop_index("idx_smartup_fulfillments_state_available", table_name="smartup_fulfillments")
    op.drop_table("smartup_fulfillments")
