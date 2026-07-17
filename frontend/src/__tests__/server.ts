import { http, HttpResponse } from "msw";

import {
  adminTable,
  activeOrder,
  authenticatedSession,
  clientOrderSummary,
  clientPoint,
  dashboardSummary,
  eventQueue,
  firstAdminRow,
  incident,
  logisticsCalendar,
  operationsAttention,
  readiness,
  secondAdminRow,
  skladbotDryRuns,
  smartupHistory,
} from "./fixtures";
import { server } from "../test/server";

export const defaultHandlers = [
  http.get("/api/v1/auth/session", () => HttpResponse.json(authenticatedSession)),
  http.post("/api/v1/auth/login", () => HttpResponse.json(authenticatedSession)),
  http.post("/api/v1/auth/logout", () => HttpResponse.json({ ...authenticatedSession, authenticated: false })),
  http.get("/api/v1/orders/active", () => HttpResponse.json([activeOrder])),
  http.post("/api/v1/orders/:orderId/complete", () => HttpResponse.json({ ...activeOrder, status: "archive" })),
  http.get("/api/v1/returns/lookup", () => HttpResponse.json({ ...activeOrder, status: "archive" })),
  http.post("/api/v1/returns/:orderId", () => HttpResponse.json({ ...activeOrder, status: "returned" })),
  http.get("/api/v1/admin/table", ({ request }) => {
    const url = new URL(request.url);
    const offset = Number(url.searchParams.get("offset") || "0");
    if (url.searchParams.get("cursor") || offset > 0) {
      return HttpResponse.json(adminTable([secondAdminRow], {
        offset,
        total_rows: 2,
        row_count: 1,
        has_more: false,
      }));
    }
    return HttpResponse.json(adminTable([firstAdminRow], {
      total_rows: 2,
      has_more: true,
      next_cursor: "synthetic-page-2",
    }));
  }),
  http.get("/api/v1/admin/dashboard/day-summary", () => HttpResponse.json(dashboardSummary)),
  http.get("/api/v1/imports", () => HttpResponse.json([])),
  http.get("/api/v1/admin/client-points", () => HttpResponse.json([clientPoint])),
  http.get("/api/v1/admin/client-points/order-summary", () => HttpResponse.json(clientOrderSummary)),
  http.post("/api/v1/admin/client-points/timeslot", () => HttpResponse.json(clientPoint)),
  http.get("/api/v1/readiness", () => HttpResponse.json(readiness)),
  http.get("/api/v1/admin/events", () => HttpResponse.json(eventQueue)),
  http.get("/api/v1/admin/operations", () => HttpResponse.json(operationsAttention)),
  http.get("/api/v1/admin/smartup-auto-imports/history", () => HttpResponse.json(smartupHistory)),
  http.get("/api/v1/admin/logistics-calendar", () => HttpResponse.json(logisticsCalendar)),
  http.get("/api/v1/admin/incidents", () => HttpResponse.json({ items: [incident], summary: { total: 1 } })),
  http.get("/api/v1/admin/skladbot/dry-runs", () => HttpResponse.json(skladbotDryRuns)),
  http.post("/api/v1/admin/orders/:orderId/archive-without-kiz", () => HttpResponse.json({})),
  http.post("/api/v1/admin/incidents/:incidentId/status", () => HttpResponse.json(incident)),
  http.post("/api/v1/admin/events/:eventId/retry", () => HttpResponse.json(eventQueue.recent_events[0])),
];

export { server };
