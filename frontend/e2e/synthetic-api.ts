import type { Page, Route } from "@playwright/test";

export const syntheticUser = {
  authenticated: true,
  login: "+998900000001",
  role: "admin",
  permissions: ["admin:read", "admin:write", "warehouse:read", "warehouse:write", "imports:read", "client_points:read", "client_points:write", "diagnostics:read"],
  expires_at: "2099-01-01T00:00:00Z",
  csrf_token: "synthetic-csrf",
};

const now = "2026-07-10T08:00:00Z";

function orderRow(id: string, client: string, itemId = `${id}-item`) {
  return {
    order_id: id,
    item_id: itemId,
    order_date: "2026-07-10",
    payment_type: "Перечисление",
    client,
    address: "Синтетическая улица, 1",
    coordinates: "",
    representative: "Тестовый ТП",
    order_status: "active",
    item_status: "active",
    status_bucket: "active",
    product: "Синтетический товар",
    quantity_pieces: 20,
    quantity_blocks: 2,
    scanned_blocks: 0,
    remaining_blocks: 2,
    scan_codes_count: 0,
    block_price: 100,
    line_total: 200,
    skladbot_request_number: "",
    skladbot_request_id: "",
    skladbot_status: "missing",
    skladbot_return_request_number: "",
    skladbot_return_request_id: "",
    skladbot_return_status: "",
    source_file: "synthetic.xlsx",
    return_status: "",
    returned_at: "",
    return_reference: "",
    created_at: now,
    updated_at: now,
  };
}

function adminTable(rows = [orderRow("order-1", "Альфа Тест")], hasMore = true, offset = 0) {
  const orderCapabilities = Object.fromEntries(rows.map((row) => [row.order_id, {
    order_id: row.order_id,
    items_count: 1,
    planned_blocks: row.quantity_blocks,
    scanned_blocks: row.scanned_blocks,
    scan_codes_count: row.scan_codes_count,
    allowed: {
      archive: true,
      completeWithoutKiz: true,
      cancel: true,
      deleteActive: true,
      resetRescan: true,
      restore: false,
      resyncSkladBot: true,
    },
    disabled_reasons: {
      archive: "",
      completeWithoutKiz: "",
      cancel: "",
      deleteActive: "",
      resetRescan: "",
      restore: "Доступно только для отмененных заказов или архива без КИЗов",
      resyncSkladBot: "",
    },
  }]));
  return {
    generated_at: now,
    totals: {
      orders: hasMore ? 2 : rows.length,
      items: hasMore ? 2 : rows.length,
      active_orders: hasMore ? 2 : rows.length,
      archived_orders: 0,
      returned_orders: 0,
      planned_blocks: hasMore ? 4 : rows.length * 2,
      scanned_blocks: 0,
      remaining_blocks: hasMore ? 4 : rows.length * 2,
      total_price: hasMore ? 400 : rows.length * 200,
    },
    rows,
    recent_activity: [],
    limit: 500,
    offset,
    row_count: rows.length,
    total_rows: hasMore ? 2 : rows.length,
    has_more: hasMore,
    next_cursor: hasMore ? "synthetic-next-page" : "",
    order_capabilities: orderCapabilities,
  };
}

const daySummary = {
  report_date: "2026-07-10",
  source: "synthetic",
  generated_at: now,
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

const clientPoint = {
  id: "point-1",
  client_name: "Альфа Тест",
  point_name: "Точка №1",
  address: "Синтетическая улица, 1",
  coordinates: "41.0,69.0",
  representative: "Тестовый ТП",
  delivery_from: "10:00",
  delivery_to: "18:00",
  is_active: true,
  is_saved: true,
  source: "synthetic",
  has_custom_timeslot: false,
  orders_count: 2,
  returned_orders_count: 0,
  last_order_date: "2026-07-10",
  created_at: now,
  updated_at: now,
};

const incident = {
  id: "incident-1",
  source: "synthetic",
  severity: "warning",
  status: "open",
  title: "Синтетический инцидент",
  message: "Проверка operator flow без production data",
  entity_type: "order",
  entity_id: "order-1",
  pending_event_id: "event-1",
  order_id: "order-1",
  order_item_id: "",
  import_id: "",
  scan_code_id: "",
  external_ref: "SYN-1",
  raw_payload: { synthetic: true },
  created_at: now,
  updated_at: now,
  resolved_at: null,
};

const queueEvent = {
  id: "event-1",
  event_type: "synthetic.retry",
  status: "failed",
  attempts: 1,
  last_error: "synthetic failure",
  idempotency_key: "synthetic-event-1",
  next_attempt_at: now,
  payload_status: "failed",
  retryable: true,
  linked_order_id: "order-1",
  linked_import_id: "",
  linked_entity_type: "order",
  linked_entity_id: "order-1",
  raw_payload: { synthetic: true },
  age_seconds: 10,
  created_at: now,
  updated_at: now,
};

export type SyntheticApiOptions = {
  authenticated?: boolean;
  empty?: boolean;
  initialTableDelayMs?: number;
  fail?: Partial<Record<"login" | "table" | "scan" | "clientUpdate" | "incidentUpdate", number | "timeout">>;
};

export type SyntheticApiState = {
  requests: string[];
  scans: number;
  completes: number;
  clientUpdates: number;
  incidentUpdates: number;
  calendarUpdates: number;
  loggedIn: boolean;
};

function buildWarehouseOrder(scannedCodes: string[], completed = false) {
  return {
    id: "order-1",
    order_date: "2026-07-10",
    payment_type: "Перечисление",
    client: "Альфа Тест",
    address: "Синтетическая улица, 1",
    representative: "Тестовый ТП",
    status: completed ? "completed" : "active",
    smartup_id: "261000001",
    skladbot_request_number: "WH-R-SYNTHETIC",
    skladbot_request_id: "synthetic",
    skladbot_return_request_number: "",
    skladbot_return_request_id: "",
    items: [
      {
        id: "order-1-item",
        product: "Синтетический товар",
        quantity_pieces: 20,
        quantity_blocks: 2,
        scanned_blocks: scannedCodes.length,
        requires_kiz: true,
        status: completed || scannedCodes.length >= 2 ? "completed" : (scannedCodes.length > 0 ? "not_completed" : "active"),
        scan_codes: [...scannedCodes],
        scan_entries: scannedCodes.map((code, index) => ({
          code,
          scan_type: "unit",
          block_quantity: 1,
          scanned_at: `2026-07-10T08:0${5 + index}:00Z`,
        })),
      },
      {
        id: "order-1-item-optional",
        product: "Синтетический товар без КИЗ",
        quantity_pieces: 10,
        quantity_blocks: 1,
        scanned_blocks: 0,
        requires_kiz: false,
        status: completed ? "completed" : "active",
        scan_codes: [],
        scan_entries: [],
      },
    ],
  };
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

function apiError(route: Route, status: number) {
  return json(route, { detail: { code: `synthetic_${status}`, message: `Synthetic ${status} state` } }, status);
}

async function configuredFailure(route: Route, failure: number | "timeout" | undefined) {
  if (failure === "timeout") {
    await route.abort("timedout");
    return true;
  }
  if (failure) {
    await apiError(route, failure);
    return true;
  }
  return false;
}


// Синтетический месяц логистики. Нужен, чтобы календарь и его дневная
// врезка снимались с данными, а не пустым каркасом: без мока экран
// отвечал 501 и дизайн заполненного состояния увидеть было нельзя.
const CALENDAR_MONTH = "2026-08";

function daysInMonth(month: string) {
  const [y, m] = month.split("-").map(Number);
  return new Date(Date.UTC(y, m, 0)).getUTCDate();
}

function syntheticCalendarDay(day: number, month: string = CALENDAR_MONTH) {
  const date = `${month}-${String(day).padStart(2, "0")}`;
  const weekday = new Date(`${date}T00:00:00Z`).getUTCDay() || 7; // 1..7, пн..вс
  const isWeekend = weekday >= 6;
  // 12 августа помечен вручную нерабочим, чтобы состояние «ручной» было видно
  const isManual = day === 12;
  const busy = !isWeekend && !isManual && day % 3 === 0;
  const cityOrders = busy ? 2 + (day % 3) : isWeekend ? 0 : day % 2;
  const regionOrders = busy ? 1 + (day % 2) : 0;
  const orders = cityOrders + regionOrders;
  const returned = busy && day % 6 === 0 ? 1 : 0;
  return {
    date,
    weekday,
    is_weekend: isWeekend,
    is_non_working: isWeekend || isManual,
    is_manual: isManual,
    reason: isManual ? "Инвентаризация склада" : "",
    source: isManual ? "manual" : "schedule",
    orders_count: orders,
    active_orders: Math.max(0, orders - returned),
    completed_orders: busy ? 1 : 0,
    returned_orders: returned,
    planned_blocks: orders * 4,
    city_orders: cityOrders,
    region_orders: regionOrders,
    city_returns: returned,
    region_returns: 0,
    city_blocks: cityOrders * 4,
    region_blocks: regionOrders * 4,
    excluded_orders: 0,
    clients: orders ? ["Альфа Тест", "Бета Тест"].slice(0, Math.min(2, orders)) : [],
  };
}

function syntheticCalendar(month: string = CALENDAR_MONTH) {
  const days = Array.from({ length: daysInMonth(month) }, (_, index) => syntheticCalendarDay(index + 1, month));
  return {
    generated_at: "2026-08-25T09:00:00Z",
    month,
    default_non_working_weekdays: [6, 7],
    region_directory_empty: false,
    days,
  };
}

function syntheticCalendarDayOrders(date: string) {
  const day = Number(date.slice(-2)) || 1;
  const count = syntheticCalendarDay(day, date.slice(0, 7)).orders_count;
  return {
    date,
    generated_at: "2026-08-25T09:00:00Z",
    region_directory_empty: false,
    orders: Array.from({ length: count }, (_, index) => ({
      order_id: `WH-R-${date.replace(/-/g, "")}-${index + 1}`,
      zone: index % 3 === 2 ? ("region" as const) : ("city" as const),
      lifecycle_status: (["assembling", "assembled", "shipped", "delivered"] as const)[index % 4],
      is_returned: false,
      client: index % 2 ? "Бета Тест" : "Альфа Тест",
      address: index % 2 ? "Синтетическая улица, 7" : "Синтетическая улица, 1",
      representative: "Синтетический ТП",
      products: "Синтетический товар",
      source_file: "synthetic.xlsx",
      quantity_blocks: 4,
      scanned_blocks: index % 4,
      remaining_blocks: 4 - (index % 4),
      status: "Активно",
      delivery_from: "09:00",
      delivery_to: "18:00",
      skladbot_request_number: `WH-R-${index + 1}`,
      smartup_id: `2610000${index + 1}`,
    })),
  };
}

export async function installSyntheticApi(page: Page, options: SyntheticApiOptions = {}): Promise<SyntheticApiState> {
  const state: SyntheticApiState = {
    requests: [],
    scans: 0,
    completes: 0,
    clientUpdates: 0,
    incidentUpdates: 0,
    calendarUpdates: 0,
    loggedIn: options.authenticated !== false,
  };
  let point = { ...clientPoint };
  let currentIncident = { ...incident };
  let warehouseScans: string[] = [];
  let completedWarehouseOrder: ReturnType<typeof buildWarehouseOrder> | null = null;

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    state.requests.push(`${request.method()} ${path}${url.search}`);

    if (path === "/api/v1/auth/session") {
      return json(route, state.loggedIn ? syntheticUser : { ...syntheticUser, authenticated: false, login: "", role: "", permissions: [], csrf_token: "" });
    }
    if (path === "/api/v1/auth/login") {
      if (await configuredFailure(route, options.fail?.login)) return;
      state.loggedIn = true;
      return json(route, syntheticUser);
    }
    if (path === "/api/v1/auth/logout") {
      state.loggedIn = false;
      return json(route, { ...syntheticUser, authenticated: false });
    }
    if (path === "/api/v1/admin/table") {
      if (await configuredFailure(route, options.fail?.table)) return;
      if (options.initialTableDelayMs && state.requests.filter((item) => item.includes("/api/v1/admin/table")).length === 1) {
        await new Promise((resolve) => setTimeout(resolve, options.initialTableDelayMs));
      }
      if (options.empty) return json(route, adminTable([], false));
      const offset = Number(url.searchParams.get("offset") ?? 0);
      const cursor = url.searchParams.get("cursor");
      const search = url.searchParams.get("search");
      if (search) return json(route, adminTable([orderRow("order-search", `Результат ${search}`)], false));
      return json(route, cursor || offset > 0 ? adminTable([orderRow("order-2", "Бета Тест")], false, offset) : adminTable());
    }
    if (path === "/api/v1/admin/dashboard/day-summary") return json(route, daySummary);
    if (path === "/api/v1/imports") return json(route, []);
    if (path === "/api/v1/admin/skladbot/dry-runs") return json(route, []);
    if (path === "/api/v1/readiness") return json(route, { generated_at: now, status: "ok", service: "synthetic", version: "test", environment: "e2e", database: {}, migrations: {}, queue: {}, imports: {} });
    if (path === "/api/v1/admin/events") return json(route, { generated_at: now, summary: { failed: 1 }, stale_processing: [], recent_events: [queueEvent] });
    if (path === "/api/v1/admin/operations") return json(route, { generated_at: now, status: "attention", summary: {}, items: [], readiness_status: "ok", telegram_summary: "synthetic" });
    if (path === "/api/v1/admin/smartup-auto-imports/history") return json(route, { generated_at: now, summary: {}, runs: [], events: [], audit: [] });
    if (path === "/api/v1/admin/logistics-calendar") {
      return json(route, syntheticCalendar(url.searchParams.get("month") ?? CALENDAR_MONTH));
    }
    if (path === "/api/v1/admin/client-points" && request.method() === "GET") return json(route, options.empty ? [] : [point]);
    if (path === "/api/v1/admin/client-points/order-summary") return json(route, { client_name: point.client_name, normalized_client: "альфа тест", totals: { orders_count: 2, returned_orders_count: 0, positions_count: 2, quantity_blocks: 4, quantity_pieces: 40 }, dates: [] });
    if (path === "/api/v1/admin/client-points/timeslot") {
      if (await configuredFailure(route, options.fail?.clientUpdate)) return;
      state.clientUpdates += 1;
      const payload = request.postDataJSON() as { delivery_from?: string; delivery_to?: string };
      point = { ...point, delivery_from: payload.delivery_from ?? point.delivery_from, delivery_to: payload.delivery_to ?? point.delivery_to, has_custom_timeslot: true };
      return json(route, point);
    }
    if (path === "/api/v1/admin/incidents" && request.method() === "GET") return json(route, { items: options.empty ? [] : [currentIncident], summary: { total: options.empty ? 0 : 1 } });
    if (/^\/api\/v1\/admin\/incidents\/[^/]+\/status$/.test(path)) {
      if (await configuredFailure(route, options.fail?.incidentUpdate)) return;
      state.incidentUpdates += 1;
      const payload = request.postDataJSON() as { status?: string };
      currentIncident = { ...currentIncident, status: payload.status ?? currentIncident.status, resolved_at: now };
      return json(route, currentIncident);
    }
    if (path === "/api/v1/orders/active") return json(route, completedWarehouseOrder ? [] : [buildWarehouseOrder(warehouseScans)]);
    if (path === "/api/v1/kiz/availability") return json(route, {
      code: url.searchParams.get("code") || "",
      available: true,
      reason: "no_backend_history",
      latest_movement_type: "",
      latest_order_item_id: "",
      existing_order_item_id: "",
    });
    if (path === "/api/v1/scans") {
      if (await configuredFailure(route, options.fail?.scan)) return;
      const payload = request.postDataJSON() as { code?: string };
      const code = payload.code || `010400639605394721SYNTH${warehouseScans.length + 1}`;
      warehouseScans = [...warehouseScans, code];
      state.scans = warehouseScans.length;
      return json(route, {
        id: `scan-${warehouseScans.length}`,
        order_item_id: "order-1-item",
        code,
        scanned_blocks: state.scans,
        item_status: state.scans >= 2 ? "completed" : "not_completed",
        scanned_at: "2026-07-10T08:05:00Z",
        scan_type: "unit",
        block_quantity: 1,
      }, 201);
    }
    if (path === "/api/v1/scans/undo") {
      const code = warehouseScans.at(-1) || "";
      warehouseScans = warehouseScans.slice(0, -1);
      state.scans = warehouseScans.length;
      return json(route, {
        id: `scan-${warehouseScans.length + 1}`,
        order_item_id: "order-1-item",
        code,
        scanned_blocks: state.scans,
        item_status: state.scans > 0 ? "not_completed" : "active",
        scanned_at: "2026-07-10T08:05:00Z",
        scan_type: "unit",
        block_quantity: 1,
      });
    }
    if (/^\/api\/v1\/orders\/[^/]+\/complete$/.test(path)) {
      completedWarehouseOrder = buildWarehouseOrder(warehouseScans, true);
      state.completes += 1;
      return json(route, completedWarehouseOrder);
    }
    if (/^\/api\/v1\/admin\/events\/[^/]+\/retry$/.test(path)) return json(route, { ...queueEvent, status: "pending" });
    if (/^\/api\/v1\/admin\/logistics-calendar\/day\/[^/]+\/orders$/.test(path)) {
      return json(route, syntheticCalendarDayOrders(path.split("/").at(-2) ?? `${CALENDAR_MONTH}-03`));
    }
    if (path === "/api/v1/admin/logistics-calendar/day") {
      state.calendarUpdates += 1;
      return json(route, syntheticCalendar());
    }
    if (path === "/api/v1/sync/sources") return json(route, { status: "completed", skladbot: { status: "synthetic" } });

    return json(route, { detail: { code: "synthetic_route_missing", message: `${request.method()} ${path} is not mocked` } }, 501);
  });

  return state;
}
