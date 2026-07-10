import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";

const cookieConfig: api.ApiConfig = {
  apiUrl: "",
  token: "",
  csrfToken: "synthetic-csrf",
};

const bearerConfig: api.ApiConfig = {
  apiUrl: "https://api.synthetic.test/",
  token: "synthetic-token",
  csrfToken: "unused-csrf",
};

function jsonResponse(payload: unknown = { ok: true }, init: ResponseInit = {}) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

function mockJsonFetch(payload: unknown = { ok: true }) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async () => jsonResponse(payload));
}

function lastRequest(fetchSpy: ReturnType<typeof vi.spyOn>) {
  const [url, options] = fetchSpy.mock.calls.at(-1) as [string, RequestInit];
  return { url, options };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("API request transport contract", () => {
  it("sends cookie GET requests same-origin without auth or CSRF headers", async () => {
    const fetchSpy = mockJsonFetch({ value: 1 });

    await expect(api.apiRequest(cookieConfig, "/test")).resolves.toEqual({ value: 1 });

    const { url, options } = lastRequest(fetchSpy);
    expect(url).toBe("/test");
    expect(options).toMatchObject({ method: "GET", credentials: "same-origin" });
    expect(options.headers).toEqual({ "Content-Type": "application/json" });
    expect(options.body).toBeUndefined();
    expect(options.signal).toBeInstanceOf(AbortSignal);
  });

  it("normalizes bearer URLs and sends JSON without cookie credentials", async () => {
    const fetchSpy = mockJsonFetch();

    await api.apiRequest(bearerConfig, "/test", {
      method: "post",
      body: { synthetic: true },
      timeoutMs: 0,
    });

    const { url, options } = lastRequest(fetchSpy);
    expect(url).toBe("https://api.synthetic.test/test");
    expect(options).toMatchObject({
      method: "POST",
      credentials: "omit",
      body: JSON.stringify({ synthetic: true }),
      signal: undefined,
    });
    expect(options.headers).toEqual({
      "Content-Type": "application/json",
      Authorization: "Bearer synthetic-token",
    });
  });

  it("adds CSRF only to unsafe cookie requests", async () => {
    const fetchSpy = mockJsonFetch();

    for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
      await api.apiRequest(cookieConfig, "/test", { method, body: { method } });
      expect(lastRequest(fetchSpy).options.headers).toMatchObject({
        "X-TakSklad-CSRF": "synthetic-csrf",
      });
    }
    for (const method of ["GET", "HEAD", "OPTIONS"]) {
      await api.apiRequest(cookieConfig, "/test", { method });
      expect(lastRequest(fetchSpy).options.headers).not.toHaveProperty("X-TakSklad-CSRF");
    }
  });

  it("omits CSRF when an unsafe cookie request has no token", async () => {
    const fetchSpy = mockJsonFetch();
    await api.apiRequest({ ...cookieConfig, csrfToken: "" }, "/test", { method: "POST" });
    expect(lastRequest(fetchSpy).options.headers).not.toHaveProperty("X-TakSklad-CSRF");
  });

  it("rejects cross-origin cookie APIs but permits the same target with bearer auth", async () => {
    const crossOrigin = { ...cookieConfig, apiUrl: "https://cross-origin.synthetic.test" };
    await expect(api.apiRequest(crossOrigin, "/test")).rejects.toThrow(
      "Cookie-сессия разрешена только для same-origin API.",
    );

    const fetchSpy = mockJsonFetch();
    await expect(api.apiRequest({ ...crossOrigin, token: "synthetic" }, "/test")).resolves.toEqual({ ok: true });
    expect(lastRequest(fetchSpy).options.credentials).toBe("omit");
  });

  it("converts AbortError failures into bounded operator timeout messages", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new DOMException("aborted", "AbortError"));
    await expect(api.apiRequest(cookieConfig, "/slow", { timeoutMs: 1_500 })).rejects.toThrow(
      "Запрос /slow не ответил за 2 сек.",
    );
  });

  it("preserves non-abort transport failures", async () => {
    const failure = new TypeError("synthetic network failure");
    vi.spyOn(globalThis, "fetch").mockRejectedValue(failure);
    await expect(api.apiRequest(cookieConfig, "/offline")).rejects.toBe(failure);
  });

  it.each([
    {
      name: "plain detail",
      payload: { detail: "Простая ошибка" },
      expected: "Простая ошибка",
      code: "",
    },
    {
      name: "message and errors",
      payload: { detail: { code: "invalid", message: "Ошибка", errors: [
        { message: "Первая", order_id: "order-1" },
        { message: "Вторая" },
        { order_id: "ignored" },
        "ignored",
      ] } },
      expected: "Ошибка: Первая [order-1]; Вторая",
      code: "invalid",
    },
    {
      name: "message only",
      payload: { detail: { message: "Только сообщение" } },
      expected: "Только сообщение",
      code: "",
    },
    {
      name: "top-level code",
      payload: { code: "top_level", message: "Сверху" },
      expected: "Сверху",
      code: "top_level",
    },
    {
      name: "array detail",
      payload: { detail: ["one", "two"] },
      expected: '["one","two"]',
      code: "",
    },
    {
      name: "null detail",
      payload: { detail: null },
      expected: '{"detail":null}',
      code: "",
    },
  ])("formats structured failure: $name", async ({ payload, expected, code }) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(payload, {
      status: 422,
      statusText: "Unprocessable Entity",
    }));

    const error = await api.apiRequest(cookieConfig, "/invalid").catch((value: unknown) => value);
    expect(error).toBeInstanceOf(api.ApiRequestError);
    expect(error).toMatchObject({ status: 422, statusText: "Unprocessable Entity", code });
    expect((error as Error).message).toContain(expected);
  });

  it.each([
    {
      name: "401 response",
      status: 401,
      body: "upstream details that must not leak",
      expected: "Сессия закончилась или доступ к API не подтвержден. Войдите снова.",
    },
    {
      name: "HTML title",
      status: 500,
      body: "<html><title>  Gateway   unavailable </title><body>private</body></html>",
      expected: "API вернул HTML-ошибку: Gateway unavailable",
    },
    {
      name: "HTML heading",
      status: 500,
      body: "<html><body><h1><span>Backend</span> unavailable</h1></body></html>",
      expected: "API вернул HTML-ошибку: Backend unavailable",
    },
    {
      name: "HTML without title",
      status: 500,
      body: "<html><body>private</body></html>",
      expected: "API вернул HTML-ошибку",
    },
    {
      name: "bounded text",
      status: 500,
      body: `  ${"x".repeat(600)}  `,
      expected: "x".repeat(500),
    },
    {
      name: "empty text",
      status: 500,
      body: "   ",
      expected: "500 Internal Server Error",
    },
  ])("sanitizes text failure: $name", async ({ status, body, expected }) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(body, {
      status,
      statusText: "Internal Server Error",
      headers: { "Content-Type": "text/plain" },
    }));
    await expect(api.apiRequest(cookieConfig, "/failed")).rejects.toThrow(expected);
  });

  it("keeps ApiRequestError fields and fallback message deterministic", () => {
    const detailed = new api.ApiRequestError(409, "Conflict", "Изменено", "write_conflict");
    expect(detailed).toMatchObject({
      name: "ApiRequestError",
      status: 409,
      statusText: "Conflict",
      code: "write_conflict",
      message: "409 Conflict: Изменено",
    });
    expect(new api.ApiRequestError("" as unknown as number, "", "").message).toBe("Ошибка запроса");
  });
});

describe("API endpoint wrapper contracts", () => {
  it("exposes the planned admin endpoints and default relative API URL", () => {
    expect(api.defaultApiUrl()).toBe("");
    expect(api.plannedAdminActionEndpoints).toEqual({
      deleteActive: "/api/v1/admin/orders/{order_id}/delete-active",
      resetRescan: "/api/v1/admin/orders/{order_id}/reset-rescan",
      restore: "/api/v1/admin/orders/{order_id}/restore",
      resyncSkladBot: "/api/v1/admin/orders/{order_id}/resync-skladbot",
    });
  });

  it.each([
    ["active orders", () => api.listActiveOrders(cookieConfig), "/api/v1/orders/active"],
    ["admin events", () => api.getAdminEvents(cookieConfig), "/api/v1/admin/events"],
    ["operations", () => api.getOperationsAttention(cookieConfig), "/api/v1/admin/operations"],
    ["readiness", () => api.getReadiness(cookieConfig), "/api/v1/readiness"],
    ["auth session", () => api.getAuthSession(cookieConfig), "/api/v1/auth/session"],
    ["imports", () => api.listImports(cookieConfig), "/api/v1/imports"],
    ["dashboard without date", () => api.getDashboardDaySummary(cookieConfig, ""), "/api/v1/admin/dashboard/day-summary"],
    ["dashboard with date", () => api.getDashboardDaySummary(cookieConfig, "2026-07-10"), "/api/v1/admin/dashboard/day-summary?report_date=2026-07-10"],
    ["calendar default", () => api.getLogisticsCalendar(cookieConfig), "/api/v1/admin/logistics-calendar"],
    ["calendar month", () => api.getLogisticsCalendar(cookieConfig, "2026-07"), "/api/v1/admin/logistics-calendar?month=2026-07"],
    ["day report default", () => api.getDayReport(cookieConfig, ""), "/api/v1/reports/day"],
    ["day report dated", () => api.getDayReport(cookieConfig, "2026/07/10"), "/api/v1/reports/day?report_date=2026%2F07%2F10"],
    ["dry runs default", () => api.listSkladBotDryRuns(cookieConfig), "/api/v1/admin/skladbot/dry-runs"],
    ["dry runs filtered", () => api.listSkladBotDryRuns(cookieConfig, "import/1"), "/api/v1/admin/skladbot/dry-runs?import_id=import%2F1"],
    ["incidents default", () => api.getAdminIncidents(cookieConfig), "/api/v1/admin/incidents?"],
    ["incident filters", () => api.getAdminIncidents(cookieConfig, { status: "open", severity: "high" }), "/api/v1/admin/incidents?status=open&severity=high"],
    ["smartup history default", () => api.getSmartupAutoImportHistory(cookieConfig), "/api/v1/admin/smartup-auto-imports/history?limit=50"],
    ["smartup history limit", () => api.getSmartupAutoImportHistory(cookieConfig, 7), "/api/v1/admin/smartup-auto-imports/history?limit=7"],
    ["client summary", () => api.getClientPointOrderSummary(cookieConfig, "Клиент & Ко"), "/api/v1/admin/client-points/order-summary?client_name=%D0%9A%D0%BB%D0%B8%D0%B5%D0%BD%D1%82+%26+%D0%9A%D0%BE"],
  ])("builds GET endpoint for %s", async (_name, invoke, expectedPath) => {
    const fetchSpy = mockJsonFetch();
    await invoke();
    const { url, options } = lastRequest(fetchSpy);
    expect(url).toBe(expectedPath);
    expect(options.method).toBe("GET");
  });

  it("builds all admin table filters and defaults", async () => {
    const fetchSpy = mockJsonFetch();
    await api.getAdminTable(cookieConfig);
    expect(lastRequest(fetchSpy).url).toBe("/api/v1/admin/table?offset=0&activity_limit=30");

    await api.getAdminTable(cookieConfig, {
      limit: 25,
      offset: 50,
      activityLimit: 5,
      statusBucket: "active",
      shipmentDate: "2026-07-10",
      search: "Клиент & Ко",
      scanState: "partial",
      skladbotFilter: "linked",
      googleSheetStatus: "pending",
    });
    expect(lastRequest(fetchSpy).url).toContain(
      "/api/v1/admin/table?offset=50&activity_limit=5&limit=25&status_bucket=active&shipment_date=2026-07-10",
    );
    expect(lastRequest(fetchSpy).url).toContain(
      "search=%D0%9A%D0%BB%D0%B8%D0%B5%D0%BD%D1%82+%26+%D0%9A%D0%BE&scan_state=partial&skladbot_filter=linked&google_sheet_status=pending",
    );
  });

  it("builds client-point query variants", async () => {
    const fetchSpy = mockJsonFetch();
    await api.listClientPoints(cookieConfig);
    expect(lastRequest(fetchSpy).url).toBe("/api/v1/admin/client-points?");
    await api.listClientPoints(cookieConfig, { limit: 0, query: "Альфа", customTimeslot: true });
    expect(lastRequest(fetchSpy).url).toContain("limit=0&query=%D0%90%D0%BB%D1%8C%D1%84%D0%B0&custom_timeslot=true");
    await api.listClientPoints(cookieConfig, { customTimeslot: false });
    expect(lastRequest(fetchSpy).url).toMatch(/custom_timeslot=false$/);
  });

  it.each([
    ["calendar update", () => api.updateLogisticsCalendarDay(cookieConfig, { service_date: "2026-07-10", is_non_working: true }), "/api/v1/admin/logistics-calendar/day"],
    ["timeslot update", () => api.updateClientPointTimeslot(cookieConfig, { client_name: "Альфа", address: "Тест", delivery_from: "10:00", delivery_to: "12:00" }), "/api/v1/admin/client-points/timeslot"],
    ["incident update", () => api.updateIncidentStatus(cookieConfig, "incident/1", { status: "resolved", reason: "synthetic" }), "/api/v1/admin/incidents/incident%2F1/status"],
    ["event retry", () => api.retryAdminEvent(cookieConfig, "event/1", { reason: "synthetic" }), "/api/v1/admin/events/event%2F1/retry"],
    ["login", () => api.loginWeb(cookieConfig, "synthetic-user", "synthetic-password"), "/api/v1/auth/login"],
    ["logout", () => api.logoutWeb(cookieConfig), "/api/v1/auth/logout"],
    ["google retry", () => api.retryPendingGoogle(cookieConfig), "/api/v1/admin/google/pending/retry"],
    ["google resync", () => api.resyncGoogleOrder(cookieConfig, "order-1", { reason: "synthetic" }), "/api/v1/admin/orders/order-1/resync-google"],
    ["archive", () => api.archiveOrderWithoutKiz(cookieConfig, "order-1", { reason: "synthetic" }), "/api/v1/admin/orders/order-1/archive-without-kiz"],
    ["cancel", () => api.cancelOrder(cookieConfig, "order-1", { reason: "synthetic" }), "/api/v1/admin/orders/order-1/cancel"],
    ["delete", () => api.deleteActiveOrder(cookieConfig, "order-1", { reason: "synthetic" }), "/api/v1/admin/orders/order-1/delete-active"],
    ["reset", () => api.resetOrderForRescan(cookieConfig, "order-1", { reason: "synthetic" }), "/api/v1/admin/orders/order-1/reset-rescan"],
    ["restore", () => api.restoreOrder(cookieConfig, "order-1", { reason: "synthetic" }), "/api/v1/admin/orders/order-1/restore"],
    ["skladbot resync", () => api.resyncSkladBotOrder(cookieConfig, "order-1", { reason: "synthetic" }), "/api/v1/admin/orders/order-1/resync-skladbot"],
    ["rebuild dry run", () => api.rebuildSkladBotDryRun(cookieConfig, "dry/run"), "/api/v1/admin/skladbot/dry-runs/dry%2Frun/rebuild"],
  ])("builds POST endpoint for %s", async (_name, invoke, expectedPath) => {
    const fetchSpy = mockJsonFetch();
    await invoke();
    const { url, options } = lastRequest(fetchSpy);
    expect(url).toBe(expectedPath);
    expect(options.method).toBe("POST");
  });

  it("merges bulk order identifiers with audit payload", async () => {
    const fetchSpy = mockJsonFetch();
    await api.completeOrdersWithoutKiz(cookieConfig, ["order-1", "order-2"], {
      reason: "synthetic",
      actor: "unit-test",
    });
    expect(lastRequest(fetchSpy)).toMatchObject({
      url: "/api/v1/admin/orders/bulk/complete-without-kiz",
      options: {
        method: "POST",
        body: JSON.stringify({
          order_ids: ["order-1", "order-2"],
          reason: "synthetic",
          actor: "unit-test",
        }),
      },
    });
  });

  it("builds source synchronization defaults and overrides", async () => {
    const fetchSpy = mockJsonFetch();
    await api.syncSources(cookieConfig);
    expect(lastRequest(fetchSpy).url).toBe("/api/v1/sync/sources?skladbot=1&wait_skladbot=0");
    await api.syncSources(cookieConfig, { skladbot: false, waitSkladbot: true });
    expect(lastRequest(fetchSpy).url).toBe("/api/v1/sync/sources?skladbot=0&wait_skladbot=1");
  });
});

describe("diagnostics download contract", () => {
  it.each([
    ["server filename", "synthetic-audit.txt", "synthetic-audit.txt"],
    ["fallback filename", null, "TakSklad_backend_diagnostics.txt"],
  ])("returns blob and %s", async (_name, header, expectedFilename) => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("synthetic log", {
      headers: header ? { "X-TakSklad-Filename": header } : {},
    }));
    const result = await api.downloadDiagnosticsLog(cookieConfig);
    expect(result.filename).toBe(expectedFilename);
    expect(await result.blob.text()).toBe("synthetic log");
    expect(lastRequest(fetchSpy).options.credentials).toBe("same-origin");
  });

  it("uses bearer auth without cookies", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("synthetic log"));
    await api.downloadDiagnosticsLog(bearerConfig);
    expect(lastRequest(fetchSpy)).toMatchObject({
      url: "https://api.synthetic.test/api/v1/diagnostics/logs",
      options: {
        credentials: "omit",
        headers: { Authorization: "Bearer synthetic-token" },
      },
    });
  });

  it("rejects failed downloads with typed status evidence", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("failed", {
      status: 503,
      statusText: "Service Unavailable",
    }));
    await expect(api.downloadDiagnosticsLog(cookieConfig)).rejects.toMatchObject({
      name: "ApiRequestError",
      status: 503,
      statusText: "Service Unavailable",
      message: "503 Service Unavailable: Не удалось скачать audit log",
    });
  });
});
