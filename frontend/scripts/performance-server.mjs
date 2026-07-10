import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, resolve, sep } from "node:path";
import process from "node:process";
import { URL } from "node:url";

const host = "127.0.0.1";
const port = Number(process.env.TAKSKLAD_PERF_PORT ?? 4180);
const distRoot = resolve(import.meta.dirname, "../dist");
const now = "2026-07-10T08:00:00Z";

const syntheticUser = {
  authenticated: true,
  login: "+998900000001",
  role: "admin",
  permissions: ["admin:read", "admin:write", "imports:read", "client_points:read", "client_points:write", "diagnostics:read"],
  expires_at: "2099-01-01T00:00:00Z",
  csrf_token: "synthetic-csrf",
};

const orderRow = {
  order_id: "order-1",
  item_id: "order-1-item",
  order_date: "2026-07-10",
  payment_type: "Перечисление",
  client: "Альфа Тест",
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
  google_sheet_status: "synced",
  google_sheet_row_number: 1,
  google_sheet_synced_at: now,
  pending_google_exports: 0,
  return_status: "",
  returned_at: "",
  return_reference: "",
  created_at: now,
  updated_at: now,
};

const initialResponses = {
  "/api/v1/auth/session": syntheticUser,
  "/api/v1/admin/table": {
    generated_at: now,
    totals: {
      orders: 1,
      items: 1,
      active_orders: 1,
      archived_orders: 0,
      returned_orders: 0,
      planned_blocks: 2,
      scanned_blocks: 0,
      remaining_blocks: 2,
      total_price: 200,
      pending_google_exports: 0,
    },
    rows: [orderRow],
    recent_activity: [],
    limit: 500,
    offset: 0,
    row_count: 1,
    total_rows: 1,
    has_more: false,
    next_cursor: "",
    order_capabilities: {
      "order-1": {
        order_id: "order-1",
        items_count: 1,
        planned_blocks: 2,
        scanned_blocks: 0,
        scan_codes_count: 0,
        pending_google_exports: 0,
        allowed: {
          resync: true,
          archive: true,
          completeWithoutKiz: true,
          cancel: true,
          deleteActive: true,
          resetRescan: true,
          restore: false,
          resyncSkladBot: true,
        },
        disabled_reasons: {
          resync: "",
          archive: "",
          completeWithoutKiz: "",
          cancel: "",
          deleteActive: "",
          resetRescan: "",
          restore: "Доступно только для отмененных заказов или архива без КИЗов",
          resyncSkladBot: "",
        },
      },
    },
  },
  "/api/v1/admin/dashboard/day-summary": {
    report_date: "2026-07-10",
    source: "synthetic",
    generated_at: now,
    totals: {
      orders: 1,
      completed_orders: 0,
      active_orders: 1,
      returned_orders: 0,
      items: 1,
      completed_items: 0,
      planned_blocks: 2,
      scanned_blocks: 0,
      scanned_today: 0,
      remaining_blocks: 2,
      scan_codes: 0,
    },
  },
};

const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml",
};

function responseHeaders(contentType) {
  return {
    "cache-control": "no-store",
    "content-type": contentType,
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
  };
}

const server = createServer((request, response) => {
  const url = new URL(request.url ?? "/", `http://${host}:${port}`);
  const apiResponse = initialResponses[url.pathname];
  if (apiResponse !== undefined) {
    response.writeHead(200, responseHeaders("application/json; charset=utf-8"));
    response.end(JSON.stringify(apiResponse));
    return;
  }
  if (url.pathname.startsWith("/api/")) {
    response.writeHead(501, responseHeaders("application/json; charset=utf-8"));
    response.end(JSON.stringify({ detail: { code: "synthetic_route_missing", message: "Synthetic performance route is not defined" } }));
    return;
  }

  const requestedPath = url.pathname === "/" ? "/index.html" : decodeURIComponent(url.pathname);
  const candidate = resolve(distRoot, `.${requestedPath}`);
  const safeCandidate = candidate === distRoot || candidate.startsWith(`${distRoot}${sep}`);
  const filePath = safeCandidate && existsSync(candidate) && statSync(candidate).isFile()
    ? candidate
    : resolve(distRoot, "index.html");
  response.writeHead(200, responseHeaders(contentTypes[extname(filePath)] ?? "application/octet-stream"));
  createReadStream(filePath).pipe(response);
});

server.listen(port, host, () => process.stdout.write(`PERFORMANCE_SERVER_READY http://${host}:${port}\n`));

function shutdown() {
  server.close(() => process.exit(0));
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
