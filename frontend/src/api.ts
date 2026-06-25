export type OrderItem = {
  id: string;
  product: string;
  quantity_pieces: number;
  quantity_blocks: number;
  scanned_blocks: number;
  status: string;
  scan_codes: string[];
};

export type Order = {
  id: string;
  order_date: string | null;
  payment_type: string;
  client: string;
  address: string;
  representative: string | null;
  status: string;
  skladbot_request_number: string;
  skladbot_request_id: string;
  items: OrderItem[];
};

export type DayReport = {
  report_date: string;
  source: string;
  generated_at: string;
  totals: {
    orders: number;
    completed_orders: number;
    active_orders: number;
    items: number;
    completed_items: number;
    planned_blocks: number;
    scanned_blocks: number;
    scanned_today: number;
    remaining_blocks: number;
    scan_codes: number;
  };
  payment_groups: Array<{
    payment_group: string;
    payment_type: string;
    orders: number;
    planned_blocks: number;
    scanned_blocks: number;
    scanned_today: number;
    remaining_blocks: number;
    scan_codes: number;
  }>;
  orders: Array<{
    id: string;
    order_date: string | null;
    payment_type: string;
    payment_group: string;
    client: string;
    address: string;
    representative: string | null;
    status: string;
    skladbot_request_number: string;
    items: number;
    completed_items: number;
    planned_blocks: number;
    scanned_blocks: number;
    scanned_today: number;
    remaining_blocks: number;
    scan_codes: number;
  }>;
};

export type ImportRecord = {
  id: string;
  source: string;
  status: string;
  rows_total: number;
  rows_imported: number;
  raw_payload: Record<string, unknown>;
  created_at: string;
};

export type SkladBotDryRunProduct = {
  product: string;
  quantity_blocks: number;
  product_data_id: number | null;
  barcode: string;
  is_main_barcode: boolean;
  status: "ready" | "blocked" | string;
  error: string;
};

export type SkladBotDryRun = {
  id: string;
  event_id: string;
  import_id: string;
  order_id: string;
  client: string;
  order_date: string | null;
  payment_type: string;
  address: string;
  blocks: number;
  status: "ready" | "blocked" | "already_linked" | string;
  error: string;
  products: SkladBotDryRunProduct[];
  payload: Record<string, unknown>;
  generated_at: string | null;
};

export type AdminTableTotals = {
  orders: number;
  items: number;
  active_orders: number;
  archived_orders: number;
  returned_orders: number;
  planned_blocks: number;
  scanned_blocks: number;
  remaining_blocks: number;
  total_price: number;
  pending_google_exports: number;
};

export type AdminTableRow = {
  order_id: string;
  item_id: string;
  order_date: string | null;
  payment_type: string;
  client: string;
  address: string;
  coordinates: string;
  representative: string | null;
  order_status: string;
  item_status: string;
  status_bucket: string;
  product: string;
  quantity_pieces: number;
  quantity_blocks: number;
  scanned_blocks: number;
  remaining_blocks: number;
  scan_codes_count: number;
  block_price: number;
  line_total: number;
  skladbot_request_number: string;
  skladbot_request_id: string;
  skladbot_status: string;
  skladbot_return_request_number: string;
  skladbot_return_request_id: string;
  skladbot_return_status: string;
  source_file: string;
  google_sheet_status: string;
  google_sheet_row_number: number | null;
  google_sheet_synced_at: string;
  pending_google_exports: number;
  return_status: string;
  returned_at: string;
  return_reference: string;
  created_at: string | null;
  updated_at: string | null;
};

export type AdminActivity = {
  id: string;
  action: string;
  entity_type: string;
  entity_id: string;
  payload: Record<string, unknown>;
  created_at: string | null;
};

export type EventQueueEvent = {
  id: string;
  event_type: string;
  status: string;
  attempts: number;
  last_error: string;
  idempotency_key: string;
  next_attempt_at: string;
  payload_status: string;
  retryable: boolean;
  linked_order_id: string;
  linked_import_id: string;
  linked_entity_type: string;
  linked_entity_id: string;
  raw_payload: Record<string, unknown>;
  age_seconds: number;
  created_at: string | null;
  updated_at: string | null;
};

export type EventQueueDiagnostics = {
  generated_at: string;
  summary: Record<string, unknown>;
  stale_processing: EventQueueEvent[];
  recent_events: EventQueueEvent[];
};

export type AdminIncident = {
  id: string;
  source: string;
  severity: string;
  status: string;
  title: string;
  message: string;
  entity_type: string;
  entity_id: string;
  pending_event_id: string;
  order_id: string;
  order_item_id: string;
  import_id: string;
  scan_code_id: string;
  external_ref: string;
  raw_payload: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
  resolved_at: string | null;
};

export type AdminIncidentsResponse = {
  items: AdminIncident[];
  summary: Record<string, unknown>;
};

export type ClientPoint = {
  id: string;
  client_name: string;
  point_name: string;
  address: string;
  coordinates: string;
  representative: string;
  delivery_from: string;
  delivery_to: string;
  is_active: boolean;
  is_saved: boolean;
  source: string;
  has_custom_timeslot: boolean;
  orders_count: number;
  last_order_date: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type ClientPointOrderSummary = {
  client_name: string;
  normalized_client: string;
  totals: ClientPointOrderSummaryTotals;
  dates: ClientPointOrderSummaryDate[];
};

export type ClientPointOrderSummaryTotals = {
  orders_count: number;
  positions_count: number;
  quantity_blocks: number;
  quantity_pieces: number;
};

export type ClientPointOrderSummaryDate = {
  shipment_date: string | null;
  orders_count: number;
  positions_count: number;
  quantity_blocks: number;
  quantity_pieces: number;
  products: ClientPointOrderSummaryProduct[];
};

export type ClientPointOrderSummaryProduct = {
  product: string;
  positions_count: number;
  quantity_blocks: number;
  quantity_pieces: number;
};

export type ClientPointTimeslotPayload = {
  client_name: string;
  address: string;
  point_name?: string;
  coordinates?: string;
  representative?: string;
  delivery_from: string;
  delivery_to: string;
  is_active?: boolean;
  actor?: string;
  reason?: string;
};

export type ReadinessResponse = {
  generated_at: string;
  status: string;
  service: string;
  version: string;
  environment: string;
  database: Record<string, unknown>;
  migrations: Record<string, unknown>;
  queue: Record<string, unknown>;
  imports: Record<string, unknown>;
};

export type AdminTable = {
  generated_at: string;
  totals: AdminTableTotals;
  rows: AdminTableRow[];
  recent_activity: AdminActivity[];
  limit: number;
  offset: number;
  row_count: number;
  total_rows: number;
  has_more: boolean;
};

export type ApiConfig = {
  apiUrl: string;
  token: string;
};

export type AdminTableRequest = {
  limit?: number;
  offset?: number;
  activityLimit?: number;
};

export type AuthSession = {
  authenticated: boolean;
  login: string;
  role: string;
  permissions: string[];
  expires_at: string | null;
};

type RequestOptions = {
  method?: string;
  body?: unknown;
};

export class ApiRequestError extends Error {
  status: number;
  statusText: string;

  constructor(status: number, statusText: string, detail: string) {
    const prefix = `${status} ${statusText}`.trim();
    super(detail ? `${prefix}: ${detail}` : prefix || "Ошибка запроса");
    this.name = "ApiRequestError";
    this.status = status;
    this.statusText = statusText;
  }
}

export type AdminActionPayload = {
  reason?: string;
  actor?: string;
  source?: string;
  idempotency_key?: string;
  expected_updated_at?: string;
  expected_updated_at_by_order?: Record<string, string>;
};

export type AdminBulkActionResult = {
  requested: number;
  completed: number;
  failed: number;
  errors: Array<{ order_id: string; message: string }>;
  dry_run: boolean;
};

export type ActiveOrderDeleteResult = {
  order_id: string;
  deleted: boolean;
  dry_run: boolean;
  google_delete_event_id: string;
  skladbot_request_number: string;
  skladbot_request_id: string;
  message: string;
};

export type EventQueueActionPayload = {
  reason: string;
  actor?: string;
  source?: string;
  idempotency_key?: string;
};

export type SyncSourcesResult = {
  status?: string;
  errors?: string[];
  google_sheets_pending?: Record<string, unknown>;
  google_sheets?: Record<string, unknown>;
  skladbot?: Record<string, unknown>;
};

export const plannedAdminActionEndpoints = {
  deleteActive: "/api/v1/admin/orders/{order_id}/delete-active",
  resetRescan: "/api/v1/admin/orders/{order_id}/reset-rescan",
  restore: "/api/v1/admin/orders/{order_id}/restore",
  resyncSkladBot: "/api/v1/admin/orders/{order_id}/resync-skladbot",
} as const;

export function defaultApiUrl() {
  return "";
}

export async function apiRequest<T>(
  config: ApiConfig,
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const apiUrl = config.apiUrl.replace(/\/$/, "");
  const response = await fetch(`${apiUrl}${path}`, {
    method: options.method ?? "GET",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(config.token ? { Authorization: `Bearer ${config.token}` } : {}),
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = await response.json();
      detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail ?? payload);
    } catch {
      detail = await response.text();
    }
    throw new ApiRequestError(response.status, response.statusText, detail);
  }

  return response.json() as Promise<T>;
}

export function listActiveOrders(config: ApiConfig) {
  return apiRequest<Order[]>(config, "/api/v1/orders/active");
}

export function getAdminTable(config: ApiConfig, options: AdminTableRequest = {}) {
  const query = new URLSearchParams({
    limit: String(options.limit ?? 5000),
    offset: String(options.offset ?? 0),
    activity_limit: String(options.activityLimit ?? 30),
  });
  return apiRequest<AdminTable>(config, `/api/v1/admin/table?${query.toString()}`);
}

export function getAdminEvents(config: ApiConfig) {
  return apiRequest<EventQueueDiagnostics>(config, "/api/v1/admin/events?limit=100");
}

export function getAdminIncidents(config: ApiConfig, params: Record<string, string> = {}) {
  const query = new URLSearchParams({ limit: "200", ...params });
  return apiRequest<AdminIncidentsResponse>(config, `/api/v1/admin/incidents?${query.toString()}`);
}

export function listClientPoints(config: ApiConfig, params: { query?: string; customTimeslot?: boolean; limit?: number } = {}) {
  const query = new URLSearchParams({
    limit: String(params.limit ?? 1000),
  });
  if (params.query) query.set("query", params.query);
  if (params.customTimeslot !== undefined) query.set("custom_timeslot", params.customTimeslot ? "true" : "false");
  return apiRequest<ClientPoint[]>(config, `/api/v1/admin/client-points?${query.toString()}`);
}

export function getClientPointOrderSummary(config: ApiConfig, clientName: string) {
  const query = new URLSearchParams({ client_name: clientName });
  return apiRequest<ClientPointOrderSummary>(config, `/api/v1/admin/client-points/order-summary?${query.toString()}`);
}

export function updateClientPointTimeslot(config: ApiConfig, payload: ClientPointTimeslotPayload) {
  return apiRequest<ClientPoint>(config, "/api/v1/admin/client-points/timeslot", {
    method: "POST",
    body: payload,
  });
}

export function updateIncidentStatus(config: ApiConfig, incidentId: string, payload: EventQueueActionPayload & { status: string }) {
  return apiRequest<AdminIncident>(config, `/api/v1/admin/incidents/${encodeURIComponent(incidentId)}/status`, {
    method: "POST",
    body: payload,
  });
}

export function retryAdminEvent(config: ApiConfig, eventId: string, payload: EventQueueActionPayload) {
  return apiRequest<EventQueueEvent>(config, `/api/v1/admin/events/${encodeURIComponent(eventId)}/retry`, {
    method: "POST",
    body: payload,
  });
}

export function getReadiness(config: ApiConfig) {
  return apiRequest<ReadinessResponse>(config, "/api/v1/readiness");
}

export function getAuthSession(config: ApiConfig) {
  return apiRequest<AuthSession>(config, "/api/v1/auth/session");
}

export function loginWeb(config: ApiConfig, login: string, password: string) {
  return apiRequest<AuthSession>(config, "/api/v1/auth/login", {
    method: "POST",
    body: { login, password },
  });
}

export function logoutWeb(config: ApiConfig) {
  return apiRequest<AuthSession>(config, "/api/v1/auth/logout", {
    method: "POST",
  });
}

export function retryPendingGoogle(config: ApiConfig) {
  return apiRequest<Record<string, unknown>>(config, "/api/v1/admin/google/pending/retry", {
    method: "POST",
  });
}

export function resyncGoogleOrder(config: ApiConfig, orderId: string, payload: AdminActionPayload) {
  return apiRequest<Order>(config, `/api/v1/admin/orders/${orderId}/resync-google`, {
    method: "POST",
    body: payload,
  });
}

export function archiveOrderWithoutKiz(config: ApiConfig, orderId: string, payload: AdminActionPayload) {
  return apiRequest<Order>(config, `/api/v1/admin/orders/${orderId}/archive-without-kiz`, {
    method: "POST",
    body: payload,
  });
}

export function cancelOrder(config: ApiConfig, orderId: string, payload: AdminActionPayload) {
  return apiRequest<Order>(config, `/api/v1/admin/orders/${orderId}/cancel`, {
    method: "POST",
    body: payload,
  });
}

export function deleteActiveOrder(config: ApiConfig, orderId: string, payload: AdminActionPayload) {
  return apiRequest<ActiveOrderDeleteResult>(config, `/api/v1/admin/orders/${orderId}/delete-active`, {
    method: "POST",
    body: payload,
  });
}

export function resetOrderForRescan(config: ApiConfig, orderId: string, payload: AdminActionPayload) {
  return apiRequest<Order>(config, `/api/v1/admin/orders/${orderId}/reset-rescan`, {
    method: "POST",
    body: payload,
  });
}

export function restoreOrder(config: ApiConfig, orderId: string, payload: AdminActionPayload) {
  return apiRequest<Order>(config, `/api/v1/admin/orders/${orderId}/restore`, {
    method: "POST",
    body: payload,
  });
}

export function resyncSkladBotOrder(config: ApiConfig, orderId: string, payload: AdminActionPayload) {
  return apiRequest<Order>(config, `/api/v1/admin/orders/${orderId}/resync-skladbot`, {
    method: "POST",
    body: payload,
  });
}

export function completeOrdersWithoutKiz(config: ApiConfig, orderIds: string[], payload: AdminActionPayload) {
  return apiRequest<AdminBulkActionResult>(config, "/api/v1/admin/orders/bulk/complete-without-kiz", {
    method: "POST",
    body: {
      order_ids: orderIds,
      ...payload,
    },
  });
}

export function syncSources(config: ApiConfig, options: { skladbot?: boolean; waitSkladbot?: boolean } = {}) {
  const params = new URLSearchParams({
    skladbot: options.skladbot === false ? "0" : "1",
    wait_skladbot: options.waitSkladbot ? "1" : "0",
  });
  return apiRequest<SyncSourcesResult>(config, `/api/v1/sync/sources?${params.toString()}`, {
    method: "POST",
  });
}

export async function downloadDiagnosticsLog(config: ApiConfig) {
  const apiUrl = config.apiUrl.replace(/\/$/, "");
  const response = await fetch(`${apiUrl}/api/v1/diagnostics/logs`, {
    credentials: "include",
    headers: {
      ...(config.token ? { Authorization: `Bearer ${config.token}` } : {}),
    },
  });

  if (!response.ok) {
    throw new ApiRequestError(response.status, response.statusText, "Не удалось скачать audit log");
  }

  return {
    blob: await response.blob(),
    filename: response.headers.get("X-TakSklad-Filename") || "TakSklad_backend_diagnostics.txt",
  };
}

export function getDayReport(config: ApiConfig, reportDate: string) {
  const query = reportDate ? `?report_date=${encodeURIComponent(reportDate)}` : "";
  return apiRequest<DayReport>(config, `/api/v1/reports/day${query}`);
}

export function listImports(config: ApiConfig) {
  return apiRequest<ImportRecord[]>(config, "/api/v1/imports");
}

export function listSkladBotDryRuns(config: ApiConfig, importId = "") {
  const query = importId ? `?import_id=${encodeURIComponent(importId)}` : "";
  return apiRequest<SkladBotDryRun[]>(config, `/api/v1/admin/skladbot/dry-runs${query}`);
}

export function rebuildSkladBotDryRun(config: ApiConfig, dryRunId: string) {
  return apiRequest<SkladBotDryRun[]>(config, `/api/v1/admin/skladbot/dry-runs/${encodeURIComponent(dryRunId)}/rebuild`, {
    method: "POST",
  });
}
