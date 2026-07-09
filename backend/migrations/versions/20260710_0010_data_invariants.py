"""validate warehouse data invariants with low-lock DDL

Revision ID: 20260710_0010
Revises: 20260710_0009
Create Date: 2026-07-10 10:00:00
"""

import sqlalchemy as sa
from alembic import op


revision = "20260710_0010"
down_revision = "20260710_0009"
branch_labels = None
depends_on = None


CHECKS = (
    ("order_items", "ck_order_items_quantities_nonnegative",
     "quantity_pieces >= 0 AND quantity_blocks >= 0 AND scanned_blocks >= 0"),
    ("order_items", "ck_order_items_pieces_per_block_positive",
     "pieces_per_block IS NULL OR pieces_per_block > 0"),
    ("order_items", "ck_order_items_scanned_within_plan",
     "scanned_blocks <= quantity_blocks"),
    ("orders", "ck_orders_supported_status",
     "status IN ('not_completed','completed','done','closed','returned','archived_no_kiz','cancelled')"),
    ("order_items", "ck_order_items_supported_status",
     "status IN ('not_completed','completed','done','closed','returned','removed_from_google_sheet',"
     "'archived_no_kiz','cancelled')"),
    ("imports", "ck_imports_supported_status",
     "status IN ('created','completed','completed_with_errors','failed')"),
    ("imports", "ck_imports_row_counts",
     "rows_total >= 0 AND rows_imported >= 0 AND rows_imported <= rows_total"),
    ("pending_events", "ck_pending_events_supported_status",
     "status IN ('pending','failed','error','processing','completed','blocked','dead','cancelled',"
     "'active','waiting_shipment_date','waiting_date_choice')"),
    ("pending_events", "ck_pending_events_attempts_nonnegative", "attempts >= 0"),
    ("order_items", "ck_order_items_source_identity_pair",
     "(source_import_id IS NULL AND source_import_key IS NULL) OR "
     "(source_import_id IS NOT NULL AND source_import_key IS NOT NULL)"),
    ("orders", "ck_orders_import_keys_nonblank",
     "(import_order_key IS NULL OR btrim(import_order_key) <> '') AND "
     "(import_source_order_key IS NULL OR btrim(import_source_order_key) <> '')"),
    ("order_items", "ck_order_items_import_keys_nonblank",
     "(import_item_key IS NULL OR btrim(import_item_key) <> '') AND "
     "(source_import_key IS NULL OR btrim(source_import_key) <> '')"),
)


def add_check_if_missing(table, name, expression):
    op.execute(
        "DO $migration$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = " + repr(name) + ") THEN "
        f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression}) NOT VALID; "
        "END IF; END $migration$"
    )


def ensure_concurrent_index(name, ddl, required_fragments):
    bind = op.get_bind()
    row = bind.execute(sa.text(
        "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) "
        "FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid WHERE c.relname=:name"
    ), {"name": name}).first()
    if row is not None and not row[0]:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        row = None
    if row is not None:
        definition = str(row[1] or "").lower()
        if not all(fragment.lower() in definition for fragment in required_fragments):
            raise RuntimeError(f"existing index {name} has unexpected definition")
        return
    op.execute(ddl)


def upgrade():
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    for table, name, expression in CHECKS:
        add_check_if_missing(table, name, expression)
    for table, name, _expression in CHECKS:
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")

    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '2s'")
        op.execute("SET statement_timeout = '30s'")
        ensure_concurrent_index(
            "uq_orders_active_import_order_key",
            "CREATE UNIQUE INDEX CONCURRENTLY uq_orders_active_import_order_key "
            "ON orders (import_order_key) WHERE import_order_key IS NOT NULL "
            "AND lower(status) <> 'returned' "
            "AND lower(coalesce(raw_payload->>'return_status', '')) "
            "NOT IN ('returned','return','возврат')",
            ("unique index", "import_order_key", "lower(status)", "return_status"),
        )
        ensure_concurrent_index(
            "uq_order_items_order_source_import_key",
            "CREATE UNIQUE INDEX CONCURRENTLY uq_order_items_order_source_import_key "
            "ON order_items (order_id, source_import_key) WHERE source_import_key IS NOT NULL",
            ("unique index", "order_id", "source_import_key"),
        )
        ensure_concurrent_index(
            "uq_order_items_order_import_item_key_fallback",
            "CREATE UNIQUE INDEX CONCURRENTLY uq_order_items_order_import_item_key_fallback "
            "ON order_items (order_id, import_item_key) "
            "WHERE source_import_key IS NULL AND import_item_key IS NOT NULL",
            ("unique index", "order_id", "import_item_key", "source_import_key is null"),
        )


def downgrade():
    raise RuntimeError(
        "Warehouse invariant migration is forward-only. Restore from backup or create a forward repair migration."
    )
