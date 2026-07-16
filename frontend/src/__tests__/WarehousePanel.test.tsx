import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import WarehousePanel from "../features/warehouse/WarehousePanel";
import { activeOrder } from "./fixtures";
import { defaultHandlers, server } from "./server";

beforeEach(() => server.use(...defaultHandlers));

const config = { apiUrl: "", token: "", csrfToken: "synthetic-csrf" };

describe("DB-only warehouse operations", () => {
  it("checks availability and creates a scan through backend API", async () => {
    const requests: Array<Record<string, unknown>> = [];
    server.use(http.post("/api/v1/scans", async ({ request }) => {
      requests.push(await request.json() as Record<string, unknown>);
      return HttpResponse.json({ id: "scan-1", order_item_id: "item-1", code: "0104-test", scanned_blocks: 1, item_status: "in_progress" }, { status: 201 });
    }));
    const onError = vi.fn();
    const onNotice = vi.fn();
    const user = userEvent.setup();

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={onError} onNotice={onNotice} />);
    await screen.findByText(new RegExp(activeOrder.client));
    await user.type(screen.getByLabelText("КИЗ"), "0104-test");
    await user.click(screen.getByRole("button", { name: "Записать" }));

    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0]).toMatchObject({
      order_item_id: "item-1",
      code: "0104-test",
      workstation_id: "taksklad-web",
      scanned_by: "operator-test",
    });
    expect(onNotice).toHaveBeenCalledWith("КИЗ сохранён в PostgreSQL");
    expect(onError).not.toHaveBeenCalled();
  });

  it("looks up an archived order and sends an explicit full return", async () => {
    const returnPayloads: Array<Record<string, unknown>> = [];
    server.use(
      http.get("/api/v1/returns/lookup", () => HttpResponse.json({ ...activeOrder, status: "archive" })),
      http.post("/api/v1/returns/:orderId", async ({ request }) => {
        returnPayloads.push(await request.json() as Record<string, unknown>);
        return HttpResponse.json({ ...activeOrder, status: "returned" });
      }),
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const onNotice = vi.fn();
    const user = userEvent.setup();

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={vi.fn()} onNotice={onNotice} />);
    await screen.findByText(new RegExp(activeOrder.client));
    await user.type(screen.getByLabelText("Номер SkladBot, клиент или ID заказа"), "WH-R-TEST-1");
    await user.click(screen.getByRole("button", { name: "Найти" }));
    await screen.findByRole("button", { name: "Подтвердить полный возврат" });
    await user.click(screen.getByRole("button", { name: "Подтвердить полный возврат" }));

    await waitFor(() => expect(returnPayloads).toHaveLength(1));
    expect(returnPayloads[0]).toMatchObject({
      return_reference: "WH-R-TEST-1",
      returned_by: "operator-test",
      confirmed_items: [{ item_id: "item-1", product: "Тестовый товар", quantity_blocks: 2, quantity_pieces: 20 }],
    });
    expect(onNotice).toHaveBeenCalledWith("Возврат зафиксирован в PostgreSQL; КИЗы снова доступны");
  });
});
