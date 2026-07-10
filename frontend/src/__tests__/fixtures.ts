import type {
  AdminIncident,
  AdminTable,
  AdminTableRow,
  AuthSession,
  ClientPoint,
  ClientPointOrderSummary,
  DashboardDaySummary,
  EventQueueDiagnostics,
  LogisticsCalendar,
  OperationsAttention,
  ReadinessResponse,
  SmartupAutoImportHistory,
} from "../api";

export const fullPermissions = [
  "admin:read",
  "admin:write",
  "imports:read",
  "client_points:read",
  "client_points:write",
  "diagnostics:read",
];

export const authenticatedSession: AuthSession = {
  authenticated: true,
  login: "+998901234567",
  role: "admin",
  permissions: fullPermissions,
  expires_at: "2030-01-01T00:00:00Z",
  csrf_token: "test-csrf-token",
};

export const anonymousSession: AuthSession = {
  authenticated: false,
  login: "",
  role: "",
  permissions: [],
  expires_at: null,
  csrf_token: "",
};

export function adminRow(overrides: Partial<AdminTableRow> = {}): AdminTableRow {
  return {
    order_id: "order-1",
    item_id: "item-1",
    order_date: "2026-07-10",
    payment_type: "Перечисление",
    client: "Клиент Альфа",
    address: "Ташкент, улица Тестовая, 1",
    coordinates: "41.3,69.2",
    representative: "Тестовый ТП",
    order_status: "active",
    item_status: "active",
    status_bucket: "active",
    product: "Тестовый товар",
    quantity_pieces: 20,
    quantity_blocks: 2,
    scanned_blocks: 0,
    remaining_blocks: 2,
    scan_codes_count: 0,
    block_price: 100,
    line_total: 200,
    skladbot_request_number: "WH-R-TEST-1",
    skladbot_request_id: "skladbot-1",
    skladbot_status: "found",
    skladbot_return_request_number: "",
    skladbot_return_request_id: "",
    skladbot_return_status: "",
    source_file: "synthetic.xlsx",
    google_sheet_status: "synced",
    google_sheet_row_number: 1,
    google_sheet_synced_at: "2026-07-10T08:00:00Z",
    pending_google_exports: 0,
    return_status: "",
    returned_at: "",
    return_reference: "",
    created_at: "2026-07-10T08:00:00Z",
    updated_at: "2026-07-10T08:30:00Z",
    ...overrides,
  };
}

export const firstAdminRow = adminRow();
export const secondAdminRow = adminRow({
  order_id: "order-2",
  item_id: "item-2",
  client: "Клиент Бета",
  product: "Второй товар",
  skladbot_request_number: "WH-R-TEST-2",
  skladbot_request_id: "skladbot-2",
});

export function adminTable(
  rows: AdminTableRow[] = [firstAdminRow],
  overrides: Partial<AdminTable> = {},
): AdminTable {
  return {
    generated_at: "2026-07-10T09:00:00Z",
    totals: {
      orders: rows.length,
      items: rows.length,
      active_orders: rows.length,
      archived_orders: 0,
      returned_orders: 0,
      planned_blocks: rows.reduce((sum, row) => sum + row.quantity_blocks, 0),
      scanned_blocks: rows.reduce((sum, row) => sum + row.scanned_blocks, 0),
      remaining_blocks: rows.reduce((sum, row) => sum + row.remaining_blocks, 0),
      total_price: rows.reduce((sum, row) => sum + row.line_total, 0),
      pending_google_exports: 0,
    },
    rows,
    recent_activity: [],
    limit: 500,
    offset: 0,
    row_count: rows.length,
    total_rows: rows.length,
    has_more: false,
    ...overrides,
  };
}

export const dashboardSummary: DashboardDaySummary = {
  report_date: "2026-07-10",
  source: "synthetic",
  generated_at: "2026-07-10T09:00:00Z",
  totals: {
    orders: 2,
    completed_orders: 0,
    active_orders: 2,
    returned_orders: 0,
    items: 2,
    completed_items: 0,
    planned_blocks: 4,
    scanned_blocks: 0,
    scanned_today: 0,
    remaining_blocks: 4,
    scan_codes: 0,
  },
};

export const clientPoint: ClientPoint = {
  id: "client-point-1",
  client_name: "Клиент Альфа",
  point_name: "Главная точка",
  address: "Ташкент, улица Тестовая, 1",
  coordinates: "41.3,69.2",
  representative: "Тестовый ТП",
  delivery_from: "09:00",
  delivery_to: "12:00",
  is_active: true,
  is_saved: true,
  source: "synthetic",
  has_custom_timeslot: true,
  orders_count: 2,
  returned_orders_count: 1,
  last_order_date: "2026-07-10",
  created_at: "2026-07-01T00:00:00Z",
  updated_at: "2026-07-10T00:00:00Z",
};

export const clientOrderSummary: ClientPointOrderSummary = {
  client_name: clientPoint.client_name,
  normalized_client: "клиент альфа",
  totals: {
    orders_count: 2,
    returned_orders_count: 1,
    positions_count: 1,
    quantity_blocks: 2,
    quantity_pieces: 20,
  },
  dates: [{
    shipment_date: "2026-07-10",
    payment_type: "Перечисление",
    orders_count: 2,
    returned_orders_count: 1,
    positions_count: 1,
    quantity_blocks: 2,
    quantity_pieces: 20,
    order_references: [
      {
        order_id: "order-with-number",
        skladbot_request_number: "WH-R-TEST-1",
        skladbot_request_id: "1001",
        is_returned: false,
      },
      {
        order_id: "order-with-id",
        skladbot_request_number: "",
        skladbot_request_id: "1002",
        is_returned: false,
      },
      {
        order_id: "order-without-reference",
        skladbot_request_number: "",
        skladbot_request_id: "",
        is_returned: true,
      },
    ],
    products: [{
      product: "Тестовый товар",
      positions_count: 1,
      quantity_blocks: 2,
      quantity_pieces: 20,
    }],
  }],
};

export const incident: AdminIncident = {
  id: "incident-1",
  source: "synthetic-import",
  severity: "critical",
  status: "open",
  title: "Синтетическая ошибка импорта",
  message: "Требуется проверка тестового события",
  entity_type: "import",
  entity_id: "import-1",
  pending_event_id: "event-1",
  order_id: "order-1",
  order_item_id: "item-1",
  import_id: "import-1",
  scan_code_id: "",
  external_ref: "TEST-1",
  raw_payload: { synthetic: true },
  created_at: "2026-07-10T08:00:00Z",
  updated_at: "2026-07-10T08:30:00Z",
  resolved_at: null,
};

export const eventQueue: EventQueueDiagnostics = {
  generated_at: "2026-07-10T09:00:00Z",
  summary: { total: 1, active: 1, terminal: 0 },
  stale_processing: [],
  recent_events: [{
    id: "event-1",
    event_type: "synthetic.retry",
    status: "failed",
    attempts: 1,
    last_error: "Synthetic failure",
    idempotency_key: "synthetic-key-1",
    next_attempt_at: "",
    payload_status: "failed",
    retryable: true,
    linked_order_id: "order-1",
    linked_import_id: "import-1",
    linked_entity_type: "import",
    linked_entity_id: "import-1",
    raw_payload: { synthetic: true },
    age_seconds: 60,
    created_at: "2026-07-10T08:00:00Z",
    updated_at: "2026-07-10T08:30:00Z",
  }],
};

export const readiness: ReadinessResponse = {
  generated_at: "2026-07-10T09:00:00Z",
  status: "ok",
  service: "taksklad-test",
  version: "test",
  environment: "synthetic",
  database: { status: "ok" },
  migrations: { status: "ok", current_revision: "synthetic" },
  queue: { summary: { total: 1, active: 1, terminal: 0 } },
  imports: { recent_errors: [] },
};

export const operationsAttention: OperationsAttention = {
  generated_at: "2026-07-10T09:00:00Z",
  status: "ok",
  summary: { total: 0, hot_path: 0, mirror: 0 },
  items: [],
  readiness_status: "ok",
  google_mirror_status: "ok",
  telegram_summary: "synthetic",
};

export const smartupHistory: SmartupAutoImportHistory = {
  generated_at: "2026-07-10T09:00:00Z",
  summary: { total: 0, completed: 0, orders_created: 0 },
  runs: [],
  events: [],
  audit: [],
};

export const logisticsCalendar: LogisticsCalendar = {
  generated_at: "2026-07-10T09:00:00Z",
  month: "2026-07",
  default_non_working_weekdays: [5, 6],
  days: [{
    date: "2026-07-10",
    weekday: 4,
    is_weekend: false,
    is_non_working: false,
    is_manual: false,
    reason: "",
    source: "synthetic",
    orders_count: 2,
    active_orders: 2,
    completed_orders: 0,
    returned_orders: 0,
    planned_blocks: 4,
    clients: ["Клиент Альфа"],
  }],
};
