CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username varchar(120) NOT NULL UNIQUE,
    password_hash varchar(255),
    role varchar(40) NOT NULL DEFAULT 'operator',
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source varchar(40) NOT NULL DEFAULT 'google_sheets',
    external_id varchar(120),
    order_date date,
    payment_type varchar(120) NOT NULL,
    client varchar(255) NOT NULL,
    address text NOT NULL,
    representative varchar(255),
    status varchar(40) NOT NULL DEFAULT 'not_completed',
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id uuid NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product varchar(255) NOT NULL,
    quantity_pieces integer NOT NULL DEFAULT 0,
    quantity_blocks integer NOT NULL DEFAULT 0,
    pieces_per_block integer,
    scanned_blocks integer NOT NULL DEFAULT 0,
    requires_kiz boolean NOT NULL DEFAULT true,
    status varchar(40) NOT NULL DEFAULT 'not_completed',
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS scan_codes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    order_item_id uuid NOT NULL REFERENCES order_items(id) ON DELETE CASCADE,
    code text NOT NULL,
    source varchar(40) NOT NULL DEFAULT 'desktop',
    workstation_id varchar(120),
    scanned_by varchar(120),
    scanned_at timestamptz NOT NULL DEFAULT now(),
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb
);

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

CREATE TABLE IF NOT EXISTS imports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source varchar(40) NOT NULL DEFAULT 'excel',
    status varchar(40) NOT NULL DEFAULT 'created',
    rows_total integer NOT NULL DEFAULT 0,
    rows_imported integer NOT NULL DEFAULT 0,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS import_files (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    import_id uuid REFERENCES imports(id) ON DELETE SET NULL,
    filename varchar(255) NOT NULL,
    sha256 varchar(64) NOT NULL UNIQUE,
    size_bytes integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pending_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type varchar(80) NOT NULL,
    idempotency_key varchar(180),
    status varchar(40) NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE pending_events ADD COLUMN IF NOT EXISTS idempotency_key varchar(180);

CREATE TABLE IF NOT EXISTS client_points (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_name varchar(255) NOT NULL,
    point_name varchar(255),
    address text NOT NULL,
    normalized_client varchar(255) NOT NULL,
    normalized_address text NOT NULL,
    coordinates text,
    representative varchar(255),
    delivery_from varchar(5) NOT NULL DEFAULT '10:00',
    delivery_to varchar(5) NOT NULL DEFAULT '18:00',
    is_active boolean NOT NULL DEFAULT true,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_client_points_normalized UNIQUE (normalized_client, normalized_address)
);

CREATE TABLE IF NOT EXISTS logistics_calendar_days (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    service_date date NOT NULL,
    is_non_working boolean NOT NULL DEFAULT true,
    reason varchar(255),
    source varchar(40) NOT NULL DEFAULT 'manual',
    actor varchar(120),
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_logistics_calendar_days_service_date UNIQUE (service_date)
);

CREATE TABLE IF NOT EXISTS representative_contacts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name varchar(255) NOT NULL,
    normalized_name varchar(255) NOT NULL,
    work_phone varchar(80),
    personal_phone varchar(80),
    work_zone varchar(255),
    is_active boolean NOT NULL DEFAULT true,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_representative_contacts_normalized_name UNIQUE (normalized_name)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
    action varchar(120) NOT NULL,
    entity_type varchar(80),
    entity_id varchar(120),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_orders_status_date ON orders(status, order_date);
CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_scan_codes_order_item_id ON scan_codes(order_item_id);
CREATE INDEX IF NOT EXISTS idx_scan_codes_code ON scan_codes(code);
CREATE INDEX IF NOT EXISTS idx_scan_codes_code_order_item_id ON scan_codes(code, order_item_id);
CREATE INDEX IF NOT EXISTS idx_kiz_movements_kiz_id_occurred_at ON kiz_movements(kiz_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_kiz_movements_order_id ON kiz_movements(order_id);
CREATE INDEX IF NOT EXISTS idx_kiz_movements_order_item_id ON kiz_movements(order_item_id);
CREATE INDEX IF NOT EXISTS idx_kiz_movements_scan_code_id ON kiz_movements(scan_code_id);
CREATE INDEX IF NOT EXISTS idx_import_files_sha256 ON import_files(sha256);
CREATE INDEX IF NOT EXISTS idx_pending_events_status ON pending_events(status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_events_idempotency_key ON pending_events(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_client_points_normalized ON client_points(normalized_client, normalized_address);
CREATE INDEX IF NOT EXISTS idx_client_points_timeslot ON client_points(delivery_from, delivery_to);
CREATE INDEX IF NOT EXISTS idx_logistics_calendar_days_service_date ON logistics_calendar_days(service_date);
CREATE INDEX IF NOT EXISTS idx_representative_contacts_normalized_name ON representative_contacts(normalized_name);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at);
