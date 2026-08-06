import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import WarehousePanel from "../features/warehouse/WarehousePanel";
import { activeOrder, orderItem } from "./fixtures";
import { defaultHandlers, server } from "./server";

// jsdom has no IndexedDB. The queue logic itself is proven against the real one in
// frontend/e2e/offline-queue-store.spec.ts, so here the panel gets the in-memory
// store with the identical contract.
const queueHolder = vi.hoisted(() => ({ store: null as unknown }));

vi.mock("../features/warehouse/offline/queueStore", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../features/warehouse/offline/queueStore")>();
  return {
    ...actual,
    createIndexedDbQueueStore: () => queueHolder.store ?? actual.createMemoryQueueStore(),
  };
});

beforeEach(async () => {
  const { createMemoryQueueStore } = await import("../features/warehouse/offline/queueStore");
  queueHolder.store = createMemoryQueueStore();
  server.use(...defaultHandlers);
  vi.restoreAllMocks();
  Object.defineProperty(window, "print", { configurable: true, value: vi.fn() });
});

const config = { apiUrl: "", token: "", csrfToken: "synthetic-csrf" };

describe("DB-only warehouse operations", () => {
  it("submits a scan with availability preflight and Enter", async () => {
    const availabilityRequests: Array<{ code: string; order_item_id: string }> = [];
    const scanPayloads: Array<Record<string, unknown>> = [];
    const onError = vi.fn();
    const onNotice = vi.fn();
    const user = userEvent.setup();

    server.use(
      http.get("/api/v1/kiz/availability", ({ request }) => {
        const url = new URL(request.url);
        availabilityRequests.push({
          code: url.searchParams.get("code") || "",
          order_item_id: url.searchParams.get("order_item_id") || "",
        });
        return HttpResponse.json({
          code: url.searchParams.get("code") || "",
          available: true,
          reason: "no_backend_history",
          latest_movement_type: "",
          latest_order_item_id: "",
          existing_order_item_id: "",
        });
      }),
      http.post("/api/v1/scans", async ({ request }) => {
        scanPayloads.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({
          id: "scan-1",
          order_item_id: "item-1",
          code: "0104006396053947217TEST01XXXXXXXXXX",
          scanned_blocks: 1,
          item_status: "not_completed",
          scanned_at: "2026-07-10T08:05:00Z",
          scan_type: "unit",
          block_quantity: 1,
        }, { status: 201 });
      }),
    );

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={onError} onNotice={onNotice} />);

    const input = await screen.findByLabelText("КИЗ");
    await waitFor(() => expect(input).toHaveFocus());
    await user.type(input, "0104006396053947217TEST01XXXXXXXXXX{Enter}");

    await waitFor(() => expect(scanPayloads).toHaveLength(1));
    expect(availabilityRequests).toEqual([{ code: "0104006396053947217TEST01XXXXXXXXXX", order_item_id: "item-1" }]);
    expect(scanPayloads[0]).toMatchObject({
      order_item_id: "item-1",
      code: "0104006396053947217TEST01XXXXXXXXXX",
      workstation_id: "taksklad-web",
      scanned_by: "operator-test",
    });
    await waitFor(() => expect(input).toHaveValue(""));
    expect(screen.getByText("КИЗ подтверждён и записан.")).toBeInTheDocument();
    expect(input).toHaveFocus();
    expect(onNotice).toHaveBeenCalledWith("КИЗ сохранён в PostgreSQL");
    expect(onError).not.toHaveBeenCalled();
  });

  it("does not refetch active orders while the operator types a KIZ", async () => {
    let activeOrderRequests = 0;
    const user = userEvent.setup();

    server.use(http.get("/api/v1/orders/active", () => {
      activeOrderRequests += 1;
      return HttpResponse.json([activeOrder]);
    }));

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={vi.fn()} onNotice={vi.fn()} />);

    const input = await screen.findByLabelText("КИЗ");
    expect(activeOrderRequests).toBe(1);

    await user.type(input, "0104006396053947217TYPED1XXXXXXXXXX");
    expect(activeOrderRequests).toBe(1);
  });

  it("reports a saved scan honestly when the follow-up order refresh fails", async () => {
    let activeOrderRequests = 0;
    const onError = vi.fn();
    const onNotice = vi.fn();
    const user = userEvent.setup();

    server.use(http.get("/api/v1/orders/active", () => {
      activeOrderRequests += 1;
      return activeOrderRequests === 1
        ? HttpResponse.json([activeOrder])
        : HttpResponse.json({ detail: "synthetic refresh failure" }, { status: 503 });
    }));

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={onError} onNotice={onNotice} />);

    const input = await screen.findByLabelText("КИЗ");
    await user.type(input, "0104006396053947217REFRESHXXXXXXXXX{Enter}");

    await waitFor(() => expect(onNotice).toHaveBeenCalledWith(
      "КИЗ сохранён, но список не обновился — нажмите Обновить.",
    ));
    expect(screen.getByText("КИЗ подтверждён и записан.")).toBeInTheDocument();
    expect(onError).not.toHaveBeenCalled();
  });

  it("fails closed when the authoritative response returns another code", async () => {
    const onError = vi.fn();
    const user = userEvent.setup();

    server.use(http.post("/api/v1/scans", () => HttpResponse.json({
      id: "scan-1",
      order_item_id: "item-1",
      code: "0104006396053947217OTHER1XXXXXXXXXX",
      scanned_blocks: 1,
      item_status: "not_completed",
      scanned_at: "2026-07-10T08:05:00Z",
      scan_type: "unit",
      block_quantity: 1,
    }, { status: 201 })));

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={onError} onNotice={vi.fn()} />);

    const input = await screen.findByLabelText("КИЗ");
    await user.type(input, "0104006396053947217TEST01XXXXXXXXXX");
    await user.click(screen.getByRole("button", { name: "Записать" }));

    expect(await screen.findByText("Сервер вернул другой КИЗ. Скан не подтвержден.")).toBeInTheDocument();
    await waitFor(() => expect(onError).toHaveBeenCalledWith(expect.any(Error), "Не удалось сохранить КИЗ"));
    expect(input).toHaveValue("0104006396053947217TEST01XXXXXXXXXX");
    expect(input).toHaveFocus();
  });

  it("keeps the scanned code and focus when backend save fails", async () => {
    const onError = vi.fn();
    const user = userEvent.setup();

    server.use(http.post("/api/v1/scans", () => HttpResponse.json({
      detail: {
        message: "Code already scanned in another order item",
      },
    }, { status: 409 })));

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={onError} onNotice={vi.fn()} />);

    const input = await screen.findByLabelText("КИЗ");
    await user.type(input, "0104006396053947217TEST01XXXXXXXXXX");
    await user.click(screen.getByRole("button", { name: "Записать" }));

    await waitFor(() => expect(onError).toHaveBeenCalledWith(expect.any(Error), "Не удалось сохранить КИЗ"));
    expect(await screen.findByText("Сохранение не подтверждено. Проверьте код и обновите заказ.")).toBeInTheDocument();
    expect(input).toHaveValue("0104006396053947217TEST01XXXXXXXXXX");
    expect(input).toHaveFocus();
  });

  it("stops after unavailable preflight and does not post the scan", async () => {
    let scanRequests = 0;
    const onError = vi.fn();
    const user = userEvent.setup();

    server.use(
      http.get("/api/v1/kiz/availability", ({ request }) => HttpResponse.json({
        code: new URL(request.url).searchParams.get("code") || "",
        available: false,
        reason: "other_order_item_scan_busy",
        latest_movement_type: "outbound",
        latest_order_item_id: "item-other",
        existing_order_item_id: "item-other",
      })),
      http.post("/api/v1/scans", () => {
        scanRequests += 1;
        return HttpResponse.json({}, { status: 500 });
      }),
    );

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={onError} onNotice={vi.fn()} />);

    const input = await screen.findByLabelText("КИЗ");
    await user.type(input, "0104006396053947217BUSY01XXXXXXXXXX");
    await user.click(screen.getByRole("button", { name: "Записать" }));

    expect(await screen.findByText("Этот КИЗ уже занят другой позицией.")).toBeInTheDocument();
    expect(scanRequests).toBe(0);
    expect(input).toHaveValue("0104006396053947217BUSY01XXXXXXXXXX");
    expect(input).toHaveFocus();
    expect(onError).toHaveBeenCalledWith(expect.any(Error), "Этот КИЗ уже занят другой позицией.");
  });

  it.each([
    { label: "слишком короткий", code: "010123456789", message: "Код слишком короткий для КИЗа (минимум 20 символов)" },
    { label: "не с 01", code: "1234567890123456789012", message: "КИЗ должен начинаться с 01" },
    { label: "с кириллицей", code: "0104006396053947217ПРИВЕТ", message: "Код содержит русские буквы! Используйте только латиницу" },
  ])("отклоняет КИЗ $label локально, как это делает desktop", async ({ code, message }) => {
    let availabilityRequests = 0;
    let scanRequests = 0;
    const onError = vi.fn();
    const user = userEvent.setup();

    server.use(
      http.get("/api/v1/kiz/availability", () => {
        availabilityRequests += 1;
        return HttpResponse.json({
          code,
          available: true,
          reason: "no_backend_history",
          latest_movement_type: "",
          latest_order_item_id: "",
          existing_order_item_id: "",
        });
      }),
      http.post("/api/v1/scans", () => {
        scanRequests += 1;
        return HttpResponse.json({}, { status: 201 });
      }),
    );

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={onError} onNotice={vi.fn()} />);

    const input = await screen.findByLabelText("КИЗ");
    await user.type(input, code);
    await user.click(screen.getByRole("button", { name: "Записать" }));

    expect(await screen.findByText(message)).toBeInTheDocument();
    // Malformed codes must never reach the backend at all.
    expect(availabilityRequests).toBe(0);
    expect(scanRequests).toBe(0);
    // The operator keeps the code and the focus so it can be re-read or fixed.
    expect(input).toHaveValue(code);
    expect(input).toHaveFocus();
    expect(onError).toHaveBeenCalledWith(expect.any(Error), message);
  });

  it("выделяет отклонённый код, чтобы следующий скан заменил его, а не дописался", async () => {
    const scanned: string[] = [];
    const user = userEvent.setup();

    server.use(
      http.get("/api/v1/kiz/availability", ({ request }) => {
        const code = new URL(request.url).searchParams.get("code") || "";
        // Первый код занят другой позицией, второй — свободен.
        const busy = code === "0104006396053947217BUSY01XXXXXXXXXX";
        return HttpResponse.json({
          code,
          available: !busy,
          reason: busy ? "other_order_item_scan_busy" : "no_backend_history",
          latest_movement_type: "",
          latest_order_item_id: "",
          existing_order_item_id: "",
        });
      }),
      http.post("/api/v1/scans", async ({ request }) => {
        const payload = await request.json() as { code: string };
        scanned.push(payload.code);
        return HttpResponse.json({
          id: "scan-1",
          order_item_id: "item-1",
          code: payload.code,
          scanned_blocks: 1,
          item_status: "not_completed",
          scanned_at: "2026-07-10T08:05:00Z",
          scan_type: "unit",
          block_quantity: 1,
        }, { status: 201 });
      }),
    );

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={vi.fn()} onNotice={vi.fn()} />);

    const input = await screen.findByLabelText("КИЗ") as HTMLInputElement;
    await user.type(input, "0104006396053947217BUSY01XXXXXXXXXX{Enter}");
    expect(await screen.findByText("Этот КИЗ уже занят другой позицией.")).toBeInTheDocument();

    // Отклонённый код остаётся в поле для оператора, но целиком выделен.
    expect(input).toHaveValue("0104006396053947217BUSY01XXXXXXXXXX");
    expect(input).toHaveFocus();
    expect(input.selectionStart).toBe(0);
    expect(input.selectionEnd).toBe("0104006396053947217BUSY01XXXXXXXXXX".length);

    // Аппаратный сканер печатает следующий код поверх выделения.
    await user.keyboard("0104006396053947217GOOD01XXXXXXXXXX{Enter}");

    await waitFor(() => expect(scanned).toEqual(["0104006396053947217GOOD01XXXXXXXXXX"]));
    expect(scanned[0]).not.toContain("BUSY01");
  });

  it("undoes the last scan with an explicit request payload", async () => {
    const undoPayloads: Array<Record<string, unknown>> = [];
    const onNotice = vi.fn();
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);

    server.use(
      http.get("/api/v1/orders/active", () => HttpResponse.json([{
        ...activeOrder,
        items: [orderItem({
          scanned_blocks: 1,
          scan_codes: ["0104-last"],
          scan_entries: [{
            code: "0104-last",
            scan_type: "unit",
            block_quantity: 1,
            scanned_at: "2026-07-10T08:05:00Z",
          }],
        })],
      }])),
      http.post("/api/v1/scans/undo", async ({ request }) => {
        undoPayloads.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({
          id: "scan-1",
          order_item_id: "item-1",
          code: "0104-last",
          scanned_blocks: 0,
          item_status: "active",
          scanned_at: "2026-07-10T08:05:00Z",
          scan_type: "unit",
          block_quantity: 1,
        });
      }),
    );

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={vi.fn()} onNotice={onNotice} />);

    await screen.findByText("0104-last");
    await user.click(screen.getByRole("button", { name: "Отменить последний КИЗ" }));

    await waitFor(() => expect(undoPayloads).toHaveLength(1));
    expect(undoPayloads[0]).toMatchObject({
      order_item_id: "item-1",
      code: "0104-last",
      workstation_id: "taksklad-web",
      actor: "operator-test",
    });
    expect(onNotice).toHaveBeenCalledWith("Последний КИЗ отменён в PostgreSQL");
  });

  it("enables completion only when required KIZ items are complete", async () => {
    server.use(http.get("/api/v1/orders/active", () => HttpResponse.json([{
      ...activeOrder,
      items: [
        orderItem({
          scanned_blocks: 2,
          scan_codes: ["0104-1", "0104-2"],
          scan_entries: [
            { code: "0104-1", scan_type: "unit", block_quantity: 1, scanned_at: "2026-07-10T08:05:00Z" },
            { code: "0104-2", scan_type: "unit", block_quantity: 1, scanned_at: "2026-07-10T08:06:00Z" },
          ],
        }),
        orderItem({
          id: "item-2",
          product: "Без маркировки",
          requires_kiz: false,
          quantity_blocks: 5,
          quantity_pieces: 50,
          scanned_blocks: 0,
          scan_codes: [],
          scan_entries: [],
        }),
      ],
    }])));

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={vi.fn()} onNotice={vi.fn()} />);

    expect(await screen.findByText("2/2 блоков · 1/1 позиций")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Завершить заказ" })).toBeEnabled();
  });

  it("treats a missing requires_kiz flag as required by default", async () => {
    server.use(http.get("/api/v1/orders/active", () => HttpResponse.json([{
      ...activeOrder,
      items: [{
        id: "item-legacy",
        product: "Старый backend товар",
        quantity_pieces: 10,
        quantity_blocks: 1,
        scanned_blocks: 0,
        status: "active",
        scan_codes: [],
        scan_entries: [],
      }],
    }])));

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={vi.fn()} onNotice={vi.fn()} />);

    expect(await screen.findByLabelText("КИЗ")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Завершить заказ" })).toBeDisabled();
  });

  it("looks up an archived order and keeps only the full return flow", async () => {
    const returnPayloads: Array<Record<string, unknown>> = [];
    const onNotice = vi.fn();
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);

    server.use(
      http.get("/api/v1/returns/lookup", () => HttpResponse.json({
        ...activeOrder,
        status: "archive",
        skladbot_return_request_number: "WR-RET-1",
        skladbot_return_request_id: "903",
      })),
      http.post("/api/v1/returns/:orderId", async ({ request }) => {
        returnPayloads.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({ ...activeOrder, status: "returned" });
      }),
    );

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={vi.fn()} onNotice={onNotice} />);

    await screen.findByText(new RegExp(activeOrder.client));
    await user.type(screen.getByLabelText("Номер или ID SkladBot, либо ID заказа"), "WH-R-TEST-1");
    await user.click(screen.getByRole("button", { name: "Найти" }));

    expect(await screen.findByText("Будет оформлен только полный возврат всех позиций и КИЗов заказа.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Подтвердить полный возврат" }));

    await waitFor(() => expect(returnPayloads).toHaveLength(1));
    expect(returnPayloads[0]).toMatchObject({
      return_reference: "WH-R-TEST-1",
      returned_by: "operator-test",
      confirmed_items: [{ item_id: "item-1", product: "Тестовый товар", quantity_blocks: 2, quantity_pieces: 20 }],
    });
    expect(onNotice).toHaveBeenCalledWith("Возврат зафиксирован в PostgreSQL; КИЗы снова доступны");
  });

  it("opens print modal explicitly and calls print only from the print button", async () => {
    let activeCalls = 0;
    const completedOrder = {
      ...activeOrder,
      status: "active",
      items: [orderItem({
        scanned_blocks: 2,
        scan_codes: ["0104-1", "0104-2"],
        scan_entries: [
          { code: "0104-1", scan_type: "unit", block_quantity: 1, scanned_at: "2026-07-10T08:05:00Z" },
          { code: "0104-2", scan_type: "unit", block_quantity: 1, scanned_at: "2026-07-10T08:06:00Z" },
        ],
        status: "completed",
      })],
    };
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);

    server.use(
      http.get("/api/v1/orders/active", () => {
        activeCalls += 1;
        return HttpResponse.json(activeCalls > 1 ? [] : [completedOrder]);
      }),
      http.post("/api/v1/orders/:orderId/complete", () => HttpResponse.json(completedOrder)),
    );

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={vi.fn()} onNotice={vi.fn()} />);

    await screen.findByText("2/2 блоков · 1/1 позиций");
    await user.click(screen.getByRole("button", { name: "Завершить заказ" }));

    const printButton = await screen.findByRole("button", { name: "Печать" });
    expect(window.print).not.toHaveBeenCalled();
    expect(printButton).toHaveFocus();

    await user.tab();
    expect(screen.getByRole("button", { name: "Закрыть" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Закрыть печать" })).toHaveFocus();
    await user.tab();
    expect(printButton).toHaveFocus();

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const openPrintButton = await screen.findByRole("button", { name: "Открыть печать" });
    expect(openPrintButton).toHaveFocus();

    await user.click(openPrintButton);
    await user.click(await screen.findByRole("button", { name: "Печать" }));
    expect(window.print).toHaveBeenCalledTimes(1);
  });
});

describe("офлайн-очередь на операторском экране", () => {
  const VALID_CODE = "0104006396053947217OFFLINE1XXXXXXXX";
  const SECOND_CODE = "0104006396053947217OFFLINE2XXXXXXXX";

  function availabilityOk() {
    return http.get("/api/v1/kiz/availability", ({ request }) => HttpResponse.json({
      code: new URL(request.url).searchParams.get("code") || "",
      available: true,
      reason: "no_backend_history",
      latest_movement_type: "",
      latest_order_item_id: "",
      existing_order_item_id: "",
    }));
  }

  it("при недоступном backend скан уходит в очередь, поле очищается, фокус возвращается", async () => {
    const onError = vi.fn();
    const onNotice = vi.fn();
    const user = userEvent.setup();

    server.use(availabilityOk(), http.post("/api/v1/scans", () => HttpResponse.error()));

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={onError} onNotice={onNotice} />);

    const input = await screen.findByLabelText("КИЗ");
    await user.type(input, `${VALID_CODE}{Enter}`);

    await waitFor(() => expect(onNotice).toHaveBeenCalledWith("КИЗ сохранён локально, отправим при связи"));
    expect(input).toHaveValue("");
    expect(input).toHaveFocus();
    expect(await screen.findByText("В очереди: 1")).toBeInTheDocument();
    expect(onError).not.toHaveBeenCalled();
  });

  it("невалидный код офлайн в очередь не попадает", async () => {
    const onError = vi.fn();
    const onNotice = vi.fn();
    const user = userEvent.setup();

    server.use(availabilityOk(), http.post("/api/v1/scans", () => HttpResponse.error()));

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={onError} onNotice={onNotice} />);

    const input = await screen.findByLabelText("КИЗ");
    await user.type(input, "ПРИВЕТ{Enter}");

    await waitFor(() => expect(onError).toHaveBeenCalled());
    expect(screen.queryByText(/В очереди:/)).not.toBeInTheDocument();
    expect(onNotice).not.toHaveBeenCalledWith("КИЗ сохранён локально, отправим при связи");
  });

  it("повторный скан кода из очереди отклоняется отдельным сообщением", async () => {
    const onError = vi.fn();
    const user = userEvent.setup();

    server.use(availabilityOk(), http.post("/api/v1/scans", () => HttpResponse.error()));

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={onError} onNotice={vi.fn()} />);

    const input = await screen.findByLabelText("КИЗ");
    await user.type(input, `${VALID_CODE}{Enter}`);
    await screen.findByText("В очереди: 1");

    await user.type(input, `${VALID_CODE}{Enter}`);

    await waitFor(() => expect(onError).toHaveBeenCalledWith(
      expect.anything(),
      "Этот КИЗ уже в очереди на отправку",
    ));
    expect(screen.getByText("В очереди: 1")).toBeInTheDocument();
  });

  it("отказ по существу в очередь не уходит, поведение прежнее", async () => {
    const onError = vi.fn();
    const onNotice = vi.fn();
    const user = userEvent.setup();

    server.use(
      availabilityOk(),
      http.post("/api/v1/scans", () => HttpResponse.json(
        { detail: { code: "scan_product_mismatch", message: "Товар не совпадает" } },
        { status: 409 },
      )),
    );

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={onError} onNotice={onNotice} />);

    const input = await screen.findByLabelText("КИЗ");
    await user.type(input, `${VALID_CODE}{Enter}`);

    await waitFor(() => expect(onError).toHaveBeenCalled());
    expect(screen.queryByText(/В очереди:/)).not.toBeInTheDocument();
    expect(onNotice).not.toHaveBeenCalledWith("КИЗ сохранён локально, отправим при связи");
  });

  it("успешная запись очередь не задействует", async () => {
    const onNotice = vi.fn();
    const user = userEvent.setup();

    server.use(
      availabilityOk(),
      http.post("/api/v1/scans", () => HttpResponse.json({
        id: "scan-1",
        order_item_id: "item-1",
        code: VALID_CODE,
        scanned_blocks: 1,
        item_status: "not_completed",
        scanned_at: "2026-07-10T08:05:00Z",
        scan_type: "unit",
        block_quantity: 1,
      }, { status: 201 })),
    );

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={vi.fn()} onNotice={onNotice} />);

    const input = await screen.findByLabelText("КИЗ");
    await user.type(input, `${VALID_CODE}{Enter}`);

    await waitFor(() => expect(onNotice).toHaveBeenCalledWith("КИЗ сохранён в PostgreSQL"));
    expect(screen.queryByText(/В очереди:/)).not.toBeInTheDocument();
  });

  it("кнопка Отправить сейчас разбирает очередь, когда связь вернулась", async () => {
    const onNotice = vi.fn();
    const user = userEvent.setup();
    let scansAccepted = 0;
    let backendUp = false;

    server.use(
      availabilityOk(),
      http.post("/api/v1/scans", () => {
        if (!backendUp) return HttpResponse.error();
        scansAccepted += 1;
        return HttpResponse.json({
          id: `scan-${scansAccepted}`,
          order_item_id: "item-1",
          code: VALID_CODE,
          scanned_blocks: scansAccepted,
          item_status: "not_completed",
          scanned_at: "2026-07-10T08:05:00Z",
          scan_type: "unit",
          block_quantity: 1,
        }, { status: 201 });
      }),
    );

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={vi.fn()} onNotice={onNotice} />);

    const input = await screen.findByLabelText("КИЗ");
    await user.type(input, `${VALID_CODE}{Enter}`);
    await screen.findByText("В очереди: 1");

    backendUp = true;
    await user.click(screen.getByRole("button", { name: "Отправить сейчас" }));

    await waitFor(() => expect(screen.queryByText(/В очереди:/)).not.toBeInTheDocument());
    expect(scansAccepted).toBe(1);
  });

  it("последний блок в очереди закрывает позицию локально, но завершение ждёт отправки", async () => {
    const user = userEvent.setup();
    // Сервер подтвердил один блок из двух, второй оператор сканирует уже офлайн
    const almostDone = orderItem({ quantity_blocks: 2, scanned_blocks: 1, scan_codes: [VALID_CODE] });

    server.use(
      http.get("/api/v1/orders/active", () => HttpResponse.json([{ ...activeOrder, items: [almostDone] }])),
      availabilityOk(),
      http.post("/api/v1/scans", () => HttpResponse.error()),
    );

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={vi.fn()} onNotice={vi.fn()} />);

    const complete = await screen.findByRole("button", { name: "Завершить заказ" });
    expect(complete).toBeDisabled();
    expect(screen.queryByText("Сначала отправьте очередь")).not.toBeInTheDocument();

    const input = screen.getByLabelText("КИЗ");
    await user.type(input, `${SECOND_CODE}{Enter}`);
    await screen.findByText("В очереди: 1");

    // Прогресс позиции учитывает очередь, иначе оператор отсканирует блок дважды
    expect(screen.getByText("Готово: 2/2 блоков.")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Завершить заказ" })).toBeDisabled();
    expect(screen.getByText("Сначала отправьте очередь")).toBeInTheDocument();
  });
});

describe("отмена последнего КИЗа с непустой очередью", () => {
  const QUEUED_CODE = "0104006396053947217UNDOQUEUEXXXXXXX";

  it("снимает код из очереди, а не отменяет чужой скан на сервере", async () => {
    const onNotice = vi.fn();
    const user = userEvent.setup();
    const undoCalls: unknown[] = [];
    const serverItem = orderItem({
      quantity_blocks: 3,
      scanned_blocks: 1,
      scan_codes: ["0104006396053947217SERVERONEXXXXXXX"],
    });

    server.use(
      http.get("/api/v1/orders/active", () => HttpResponse.json([{ ...activeOrder, items: [serverItem] }])),
      http.get("/api/v1/kiz/availability", ({ request }) => HttpResponse.json({
        code: new URL(request.url).searchParams.get("code") || "",
        available: true,
        reason: "no_backend_history",
        latest_movement_type: "",
        latest_order_item_id: "",
        existing_order_item_id: "",
      })),
      http.post("/api/v1/scans", () => HttpResponse.error()),
      http.post("/api/v1/scans/undo", async ({ request }) => {
        undoCalls.push(await request.json());
        return HttpResponse.json({ ok: true });
      }),
    );

    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={vi.fn()} onNotice={onNotice} />);

    const input = await screen.findByLabelText("КИЗ");
    await user.type(input, `${QUEUED_CODE}{Enter}`);
    await screen.findByText("В очереди: 1");

    await user.click(screen.getByRole("button", { name: "Отменить последний КИЗ" }));

    await waitFor(() => expect(screen.queryByText(/В очереди:/)).not.toBeInTheDocument());
    expect(undoCalls).toEqual([]);
    expect(onNotice).toHaveBeenCalledWith("КИЗ убран из очереди, на сервер он не уходил");
  });
});
