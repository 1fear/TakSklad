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

export type AdminTable = {
  generated_at: string;
  totals: AdminTableTotals;
  rows: AdminTableRow[];
  recent_activity: AdminActivity[];
};

export type ApiConfig = {
  apiUrl: string;
  token: string;
};

export type AuthSession = {
  authenticated: boolean;
  login: string;
  expires_at: string | null;
};

type RequestOptions = {
  method?: string;
  body?: unknown;
};

export type AdminActionPayload = {
  reason: string;
  actor?: string;
  idempotency_key?: string;
};

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
    throw new Error(detail || "Ошибка запроса");
  }

  return response.json() as Promise<T>;
}

export function listActiveOrders(config: ApiConfig) {
  return apiRequest<Order[]>(config, "/api/v1/orders/active");
}

export function getAdminTable(config: ApiConfig) {
  return apiRequest<AdminTable>(config, "/api/v1/admin/table");
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

export function getDayReport(config: ApiConfig, reportDate: string) {
  const query = reportDate ? `?report_date=${encodeURIComponent(reportDate)}` : "";
  return apiRequest<DayReport>(config, `/api/v1/reports/day${query}`);
}

export function listImports(config: ApiConfig) {
  return apiRequest<ImportRecord[]>(config, "/api/v1/imports");
}
