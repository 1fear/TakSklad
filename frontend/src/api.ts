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

export type ScanResult = {
  id: string;
  order_item_id: string;
  code: string;
  scanned_blocks: number;
  item_status: string;
  scanned_at: string;
};

export type ApiConfig = {
  apiUrl: string;
  token: string;
};

type RequestOptions = {
  method?: string;
  body?: unknown;
};

export function defaultApiUrl() {
  const configured = import.meta.env.VITE_TAKSKLAD_API_URL;
  if (configured) {
    return configured.replace(/\/$/, "");
  }

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

export function getDayReport(config: ApiConfig, reportDate: string) {
  const query = reportDate ? `?report_date=${encodeURIComponent(reportDate)}` : "";
  return apiRequest<DayReport>(config, `/api/v1/reports/day${query}`);
}

export function listImports(config: ApiConfig) {
  return apiRequest<ImportRecord[]>(config, "/api/v1/imports");
}

export function createScan(config: ApiConfig, orderItemId: string, code: string) {
  return apiRequest<ScanResult>(config, "/api/v1/scans", {
    method: "POST",
    body: {
      order_item_id: orderItemId,
      code,
      workstation_id: "web-draft",
      scanned_by: "web",
      raw_payload: { source: "taksklad-web-draft" },
    },
  });
}

export function completeOrder(config: ApiConfig, orderId: string) {
  return apiRequest<Order>(config, `/api/v1/orders/${orderId}/complete`, {
    method: "POST",
  });
}
