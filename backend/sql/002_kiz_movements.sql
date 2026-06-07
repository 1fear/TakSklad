CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE scan_codes DROP CONSTRAINT IF EXISTS uq_scan_codes_code;

CREATE TABLE IF NOT EXISTS kiz_codes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_kiz_codes_code UNIQUE (code)
);

CREATE TABLE IF NOT EXISTS kiz_movements (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kiz_id uuid NOT NULL REFERENCES kiz_codes(id) ON DELETE CASCADE,
    movement_type varchar(40) NOT NULL,
    order_id uuid REFERENCES orders(id) ON DELETE SET NULL,
    order_item_id uuid REFERENCES order_items(id) ON DELETE SET NULL,
    scan_code_id uuid REFERENCES scan_codes(id) ON DELETE SET NULL,
    return_reference varchar(120),
    source varchar(40) NOT NULL DEFAULT 'backend',
    actor varchar(120),
    workstation_id varchar(120),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_scan_codes_order_item_id ON scan_codes(order_item_id);
CREATE INDEX IF NOT EXISTS idx_scan_codes_code ON scan_codes(code);
CREATE INDEX IF NOT EXISTS idx_scan_codes_code_order_item_id ON scan_codes(code, order_item_id);
CREATE INDEX IF NOT EXISTS idx_kiz_movements_kiz_id_occurred_at ON kiz_movements(kiz_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_kiz_movements_order_id ON kiz_movements(order_id);
CREATE INDEX IF NOT EXISTS idx_kiz_movements_order_item_id ON kiz_movements(order_item_id);
CREATE INDEX IF NOT EXISTS idx_kiz_movements_scan_code_id ON kiz_movements(scan_code_id);

INSERT INTO kiz_codes (code, first_seen_at, updated_at)
SELECT scan_codes.code, MIN(scan_codes.scanned_at), now()
FROM scan_codes
WHERE btrim(scan_codes.code) <> ''
GROUP BY scan_codes.code
ON CONFLICT (code) DO NOTHING;

INSERT INTO kiz_movements (
    kiz_id,
    movement_type,
    order_id,
    order_item_id,
    scan_code_id,
    source,
    actor,
    workstation_id,
    occurred_at,
    raw_payload
)
SELECT
    kiz_codes.id,
    'outbound',
    order_items.order_id,
    scan_codes.order_item_id,
    scan_codes.id,
    COALESCE(NULLIF(scan_codes.source, ''), 'migration'),
    NULLIF(scan_codes.scanned_by, ''),
    NULLIF(scan_codes.workstation_id, ''),
    COALESCE(scan_codes.scanned_at, now()),
    jsonb_build_object('backfilled', true, 'source', '002_kiz_movements')
FROM scan_codes
JOIN order_items ON order_items.id = scan_codes.order_item_id
JOIN kiz_codes ON kiz_codes.code = scan_codes.code
WHERE NOT EXISTS (
    SELECT 1
    FROM kiz_movements existing
    WHERE existing.scan_code_id = scan_codes.id
      AND existing.movement_type IN ('outbound', 're_outbound')
);

INSERT INTO kiz_movements (
    kiz_id,
    movement_type,
    order_id,
    order_item_id,
    scan_code_id,
    return_reference,
    source,
    actor,
    occurred_at,
    raw_payload
)
SELECT
    kiz_codes.id,
    'return',
    orders.id,
    scan_codes.order_item_id,
    scan_codes.id,
    NULLIF(COALESCE(orders.raw_payload ->> 'return_reference', orders.raw_payload ->> 'skladbot_request_number', ''), ''),
    'migration',
    NULLIF(COALESCE(orders.raw_payload ->> 'returned_by', 'migration'), ''),
    GREATEST(COALESCE(orders.updated_at, now()), COALESCE(scan_codes.scanned_at, now())) + interval '1 microsecond',
    jsonb_build_object('backfilled', true, 'source', '002_kiz_movements')
FROM scan_codes
JOIN order_items ON order_items.id = scan_codes.order_item_id
JOIN orders ON orders.id = order_items.order_id
JOIN kiz_codes ON kiz_codes.code = scan_codes.code
WHERE (
    orders.status = 'returned'
    OR orders.raw_payload ->> 'return_status' = 'returned'
)
AND NOT EXISTS (
    SELECT 1
    FROM kiz_movements existing
    WHERE existing.scan_code_id = scan_codes.id
      AND existing.movement_type = 'return'
);
