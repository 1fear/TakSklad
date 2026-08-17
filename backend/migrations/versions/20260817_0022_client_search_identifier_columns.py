"""Store order identifiers in an indexable column for the client search.

Выражения продублированы литералами намеренно: миграция это снимок схемы
на свою дату, она не должна меняться следом за моделями. Совпадение с
`ORDER_SEARCH_IDENTIFIERS_SQL` и `ORDER_ITEM_SEARCH_IDENTIFIERS_SQL`
проверяет тест `test_search_identifier_columns_match_the_migration`
"""

from alembic import op


revision = "20260817_0022"
down_revision = "20260804_0021"
branch_labels = None
depends_on = None


ORDER_SEARCH_IDENTIFIERS_SQL = (
    "lower("
    "coalesce(raw_payload->>'source_order_id', '') || ' ' || "
    "coalesce(raw_payload->>'skladbot_request_number', '') || ' ' || "
    "coalesce(raw_payload->>'skladbot_request_id', '') || ' ' || "
    "coalesce(raw_payload->>'skladbot_return_request_number', '') || ' ' || "
    "coalesce(raw_payload->>'skladbot_return_request_id', '')"
    ")"
)
ORDER_ITEM_SEARCH_IDENTIFIERS_SQL = (
    "lower("
    "coalesce(raw_payload->>'source_order_id', '') || ' ' || "
    "coalesce(raw_payload->>'smartup_order_ids', '')"
    ")"
)


def upgrade():
    # ALTER берёт ACCESS EXCLUSIVE и переписывает таблицу: на стенде с боевым
    # объёмом (5414 заказов, 23 МБ) это 0.37 с, индексы по 0.02 с
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "ALTER TABLE orders ADD COLUMN search_identifiers text "
        f"GENERATED ALWAYS AS ({ORDER_SEARCH_IDENTIFIERS_SQL}) STORED"
    )
    op.execute(
        "ALTER TABLE order_items ADD COLUMN search_identifiers text "
        f"GENERATED ALWAYS AS ({ORDER_ITEM_SEARCH_IDENTIFIERS_SQL}) STORED"
    )
    op.execute(
        "CREATE INDEX idx_orders_search_identifiers "
        "ON orders USING gin (search_identifiers gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX idx_order_items_search_identifiers "
        "ON order_items USING gin (search_identifiers gin_trgm_ops)"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_order_items_search_identifiers")
    op.execute("DROP INDEX IF EXISTS idx_orders_search_identifiers")
    op.execute("ALTER TABLE order_items DROP COLUMN IF EXISTS search_identifiers")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS search_identifiers")
