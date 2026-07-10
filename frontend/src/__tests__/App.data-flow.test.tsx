import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import App from "../App";
import { adminTable, clientPoint, dashboardSummary, firstAdminRow } from "./fixtures";
import { defaultHandlers, server } from "./server";

beforeEach(() => server.use(...defaultHandlers));

describe("authenticated data-flow integration", () => {
  it("loads two critical resources and keeps hidden panels request-free until opened", async () => {
    const counts = { table: 0, dashboard: 0, clients: 0, imports: 0, events: 0 };
    server.use(
      http.get("/api/v1/admin/table", () => {
        counts.table += 1;
        return HttpResponse.json(adminTable());
      }),
      http.get("/api/v1/admin/dashboard/day-summary", () => {
        counts.dashboard += 1;
        return HttpResponse.json(dashboardSummary);
      }),
      http.get("/api/v1/admin/client-points", () => {
        counts.clients += 1;
        return HttpResponse.json([clientPoint]);
      }),
      http.get("/api/v1/imports", () => {
        counts.imports += 1;
        return HttpResponse.json([]);
      }),
      http.get("/api/v1/admin/events", () => {
        counts.events += 1;
        return HttpResponse.json({ generated_at: "2026-07-10T09:00:00Z", summary: {}, stale_processing: [], recent_events: [] });
      }),
    );
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "Позиции заказов" });

    expect(counts).toEqual({ table: 1, dashboard: 1, clients: 0, imports: 0, events: 0 });
    await user.click(screen.getByRole("button", { name: "Клиенты" }));
    await screen.findByRole("heading", { name: "Клиенты и таймслоты" });
    await waitFor(() => expect(counts.clients).toBe(1));
    expect(counts.imports).toBe(0);
    expect(counts.events).toBe(0);
  });

  it("aborts the sibling critical request when the other initial request fails", async () => {
    let dashboardAborted = false;
    server.use(
      http.get("/api/v1/admin/table", () => HttpResponse.json({ message: "synthetic failure" }, { status: 500 })),
      http.get("/api/v1/admin/dashboard/day-summary", async ({ request }) => {
        await new Promise<void>((resolve) => {
          request.signal.addEventListener("abort", () => {
            dashboardAborted = true;
            resolve();
          }, { once: true });
        });
        return HttpResponse.json(dashboardSummary);
      }),
    );

    render(<App />);

    await waitFor(() => expect(dashboardAborted).toBe(true));
  });

  it("uses complete server capabilities instead of the visible item row", async () => {
    const payload = adminTable([firstAdminRow]);
    payload.order_capabilities[firstAdminRow.order_id] = {
      ...payload.order_capabilities[firstAdminRow.order_id],
      scanned_blocks: 4,
      scan_codes_count: 4,
      allowed: {
        ...payload.order_capabilities[firstAdminRow.order_id].allowed,
        archive: false,
      },
      disabled_reasons: {
        ...payload.order_capabilities[firstAdminRow.order_id].disabled_reasons,
        archive: "Полный заказ уже содержит отсканированные КИЗы",
      },
    };
    server.use(http.get("/api/v1/admin/table", () => HttpResponse.json(payload)));
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(firstAdminRow.client);

    expect(firstAdminRow.scanned_blocks).toBe(0);
    await user.click(screen.getByRole("checkbox", { name: `Выбрать заказ ${firstAdminRow.client}` }));
    expect(screen.getByRole("button", { name: "В архив без КИЗов" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "В архив без КИЗов" })).toHaveAttribute(
      "title",
      "Полный заказ уже содержит отсканированные КИЗы",
    );
  });

  it("aborts a delayed search and never lets its old response overwrite the latest", async () => {
    let markOldStarted: (() => void) | undefined;
    const oldStarted = new Promise<void>((resolve) => { markOldStarted = resolve; });
    let oldAborted = false;
    server.use(http.get("/api/v1/admin/table", async ({ request }) => {
      const search = new URL(request.url).searchParams.get("search") || "";
      if (search === "Старый") {
        markOldStarted?.();
        request.signal.addEventListener("abort", () => { oldAborted = true; }, { once: true });
        await delay(600);
        return HttpResponse.json(adminTable([{ ...firstAdminRow, client: "Старый ответ" }]));
      }
      if (search === "Новый") {
        return HttpResponse.json(adminTable([{ ...firstAdminRow, client: "Новый ответ" }]));
      }
      return HttpResponse.json(adminTable());
    }));
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "Позиции заказов" });

    const search = screen.getByRole("searchbox", { name: "Поиск заказов" });
    await user.type(search, "Старый");
    await oldStarted;
    await user.clear(search);
    await user.type(search, "Новый");

    expect(await screen.findByText("Новый ответ")).toBeInTheDocument();
    await waitFor(() => expect(oldAborted).toBe(true));
    await delay(350);
    expect(screen.queryByText("Старый ответ")).not.toBeInTheDocument();
  });

  it("aborts a hidden protected request and clears protected state on logout", async () => {
    let markStarted: (() => void) | undefined;
    const started = new Promise<void>((resolve) => { markStarted = resolve; });
    let aborted = false;
    server.use(http.get("/api/v1/admin/client-points", async ({ request }) => {
      markStarted?.();
      await new Promise<void>((resolve) => {
        request.signal.addEventListener("abort", () => {
          aborted = true;
          resolve();
        }, { once: true });
      });
      return HttpResponse.json([]);
    }));
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "Позиции заказов" });

    await user.click(screen.getByRole("button", { name: "Клиенты" }));
    await started;
    await user.click(screen.getByRole("button", { name: "Выйти" }));

    expect(await screen.findByRole("heading", { name: "Вход в панель" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Позиции заказов" })).not.toBeInTheDocument();
    await waitFor(() => expect(aborted).toBe(true));
  });
});
