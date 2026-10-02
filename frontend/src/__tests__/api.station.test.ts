import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import {
  ACTIVE_ORDERS_MAX_PAGES,
  ApiRequestError,
  apiRequestWithHeaders,
  listAllActiveOrders,
  releaseKiz,
  stationLogin,
  type ApiConfig,
  type Order,
} from "../api";
import { activeOrder, anonymousSession } from "./fixtures";
import { server } from "./server";

const config: ApiConfig = { apiUrl: "", token: "", csrfToken: "" };
const CURSOR_HEADER = "X-TakSklad-Next-Cursor";

const stationSession = {
  ...anonymousSession,
  authenticated: true,
  login: "warehouse-station",
  role: "station",
  permissions: ["warehouse:read", "warehouse:write", "reports:read"],
  csrf_token: "station-csrf",
};

function orderWithId(id: string): Order {
  return { ...activeOrder, id };
}

async function rejection(promise: Promise<unknown>): Promise<ApiRequestError> {
  const error = await promise.then(() => null, (reason: unknown) => reason);
  expect(error).toBeInstanceOf(ApiRequestError);
  return error as ApiRequestError;
}

describe("apiRequestWithHeaders", () => {
  it("returns the parsed body together with the response headers", async () => {
    server.use(http.get("/api/v1/probe", () => HttpResponse.json({ ok: true }, { headers: { "X-Probe": "yes" } })));

    const { data, headers } = await apiRequestWithHeaders<{ ok: boolean }>(config, "/api/v1/probe");

    expect(data).toEqual({ ok: true });
    expect(headers.get("X-Probe")).toBe("yes");
  });

  it("puts Retry-After seconds on the error", async () => {
    server.use(http.get("/api/v1/probe", () => HttpResponse.json({}, { status: 429, headers: { "Retry-After": "75" } })));

    const failure = await rejection(apiRequestWithHeaders(config, "/api/v1/probe"));

    expect(failure.status).toBe(429);
    expect(failure.retryAfterSeconds).toBe(75);
  });

  it("reads Retry-After given as an HTTP date and ignores garbage", async () => {
    const inTwoMinutes = new Date(Date.now() + 120_000).toUTCString();
    server.use(
      http.get("/api/v1/dated", () => HttpResponse.json({}, { status: 429, headers: { "Retry-After": inTwoMinutes } })),
      http.get("/api/v1/garbage", () => HttpResponse.json({}, { status: 429, headers: { "Retry-After": "soon" } })),
      http.get("/api/v1/absent", () => HttpResponse.json({}, { status: 503 })),
    );

    const dated = await rejection(apiRequestWithHeaders(config, "/api/v1/dated"));
    const garbage = await rejection(apiRequestWithHeaders(config, "/api/v1/garbage"));
    const absent = await rejection(apiRequestWithHeaders(config, "/api/v1/absent"));

    expect(dated.retryAfterSeconds).toBeGreaterThan(100);
    expect(dated.retryAfterSeconds).toBeLessThanOrEqual(120);
    expect(garbage.retryAfterSeconds).toBe(0);
    expect(absent.retryAfterSeconds).toBe(0);
  });
});

describe("stationLogin", () => {
  it("posts with no body and returns the station session", async () => {
    let seen: { method: string; body: string } | null = null;
    server.use(http.post("/api/v1/auth/station", async ({ request }) => {
      seen = { method: request.method, body: await request.text() };
      return HttpResponse.json(stationSession);
    }));

    await expect(stationLogin(config)).resolves.toEqual(stationSession);

    expect(seen).toEqual({ method: "POST", body: "" });
  });

  it("surfaces the denial code outside the warehouse network", async () => {
    server.use(http.post("/api/v1/auth/station", () => HttpResponse.json(
      { detail: { code: "station_network_denied" } },
      { status: 403 },
    )));

    const failure = await rejection(stationLogin(config));

    expect(failure.status).toBe(403);
    expect(failure.code).toBe("station_network_denied");
  });
});

describe("listAllActiveOrders", () => {
  it("follows the cursor header across three pages and keeps the order", async () => {
    const urls: string[] = [];
    server.use(http.get("/api/v1/orders/active", ({ request }) => {
      const url = new URL(request.url);
      urls.push(`${url.searchParams.get("limit")}|${url.searchParams.get("cursor") ?? ""}`);
      const cursor = url.searchParams.get("cursor");
      if (!cursor) return HttpResponse.json([orderWithId("o1"), orderWithId("o2")], { headers: { [CURSOR_HEADER]: "c2" } });
      if (cursor === "c2") return HttpResponse.json([orderWithId("o3")], { headers: { [CURSOR_HEADER]: "c3" } });
      return HttpResponse.json([orderWithId("o4")]);
    }));

    const orders = await listAllActiveOrders(config);

    expect(orders.map((order) => order.id)).toEqual(["o1", "o2", "o3", "o4"]);
    expect(urls).toEqual(["200|", "200|c2", "200|c3"]);
  });

  it("stops after the first page when the header is absent", async () => {
    let requests = 0;
    server.use(http.get("/api/v1/orders/active", () => {
      requests += 1;
      return HttpResponse.json([orderWithId("only")]);
    }));

    await expect(listAllActiveOrders(config)).resolves.toHaveLength(1);

    expect(requests).toBe(1);
  });

  it("treats an empty cursor header as the end", async () => {
    server.use(http.get("/api/v1/orders/active", () => HttpResponse.json([], { headers: { [CURSOR_HEADER]: " " } })));

    await expect(listAllActiveOrders(config)).resolves.toEqual([]);
  });

  it("stops with an error after 100 pages", async () => {
    let requests = 0;
    server.use(http.get("/api/v1/orders/active", () => {
      requests += 1;
      return HttpResponse.json([orderWithId(`o${requests}`)], { headers: { [CURSOR_HEADER]: `page-${requests + 1}` } });
    }));

    await expect(listAllActiveOrders(config)).rejects.toThrow("Backend pagination exceeded the page safety limit");

    expect(ACTIVE_ORDERS_MAX_PAGES).toBe(100);
    expect(requests).toBe(100);
  });

  it("stops at a repeated cursor instead of looping", async () => {
    let requests = 0;
    server.use(http.get("/api/v1/orders/active", () => {
      requests += 1;
      return HttpResponse.json([orderWithId(`o${requests}`)], { headers: { [CURSOR_HEADER]: "same" } });
    }));

    await expect(listAllActiveOrders(config)).rejects.toThrow("Backend pagination returned a repeated cursor");

    expect(requests).toBe(2);
  });

  it("passes the abort signal to the request", async () => {
    server.use(http.get("/api/v1/orders/active", () => HttpResponse.json([])));
    const controller = new AbortController();
    controller.abort();

    await expect(listAllActiveOrders(config, { signal: controller.signal })).rejects.toThrow();
  });
});

describe("releaseKiz", () => {
  it("posts the five desktop fields and returns the result", async () => {
    let payload: unknown = null;
    const result = {
      code: "0104006396053947217ABCDEF",
      released: true,
      outcome: "released",
      latest_movement_type: "outbound",
      donor_order_item_id: "item-9",
      donor_request_number: "WH-R-9",
    };
    server.use(http.post("/api/v1/kiz/release", async ({ request }) => {
      payload = await request.json();
      return HttpResponse.json(result);
    }));

    const released = await releaseKiz({ ...config, csrfToken: "csrf" }, {
      code: "0104006396053947217ABCDEF",
      reason: "returned_to_shelf",
      comment: "",
      workstation_id: "web-station",
      actor: "station",
    });

    expect(released).toEqual(result);
    expect(payload).toEqual({
      code: "0104006396053947217ABCDEF",
      reason: "returned_to_shelf",
      comment: "",
      workstation_id: "web-station",
      actor: "station",
    });
  });
});
