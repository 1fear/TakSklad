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
  it("shows exact order correlations and contains no web scanner affordances", async () => {
    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={vi.fn()} onNotice={vi.fn()} />);

    await screen.findByText(new RegExp(activeOrder.client));
    expect(screen.getByText("Smartup ID: 731")).toBeInTheDocument();
    expect(screen.getByText("Заявка SkladBot: WH-R-TEST-1")).toBeInTheDocument();
    expect(screen.queryByText(/Заявка возврата:/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("КИЗ")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Записать" })).not.toBeInTheDocument();
    expect(screen.queryByText("Отменить последний КИЗ")).not.toBeInTheDocument();
  });

  it("looks up an archived order and sends an explicit full return", async () => {
    const returnPayloads: Array<Record<string, unknown>> = [];
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
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const onNotice = vi.fn();
    const user = userEvent.setup();

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={vi.fn()} onNotice={onNotice} />);
    await screen.findByText(new RegExp(activeOrder.client));
    await user.type(screen.getByLabelText("Номер SkladBot, клиент или ID заказа"), "WH-R-TEST-1");
    await user.click(screen.getByRole("button", { name: "Найти" }));
    await screen.findByRole("button", { name: "Подтвердить полный возврат" });
    expect(screen.getByText("Заявка возврата: WR-RET-1")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Подтвердить полный возврат" }));

    await waitFor(() => expect(returnPayloads).toHaveLength(1));
    expect(returnPayloads[0]).toMatchObject({
      return_reference: "WH-R-TEST-1",
      returned_by: "operator-test",
      confirmed_items: [{ item_id: "item-1", product: "Тестовый товар", quantity_blocks: 2, quantity_pieces: 20 }],
    });
    expect(onNotice).toHaveBeenCalledWith("Возврат зафиксирован в PostgreSQL; КИЗы снова доступны");
  });

  it("uses decimal fallback only when canonical numbers are absent and rejects unsafe values", async () => {
    server.use(http.get("/api/v1/orders/active", () => HttpResponse.json([{
      ...activeOrder,
      smartup_id: "<script>alert(1)</script>",
      skladbot_request_number: "not-a-request",
      skladbot_request_id: "904",
    }])));

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={vi.fn()} onNotice={vi.fn()} />);

    expect(await screen.findByText("Smartup ID: —")).toBeInTheDocument();
    expect(screen.getByText("Заявка SkladBot: ID 904")).toBeInTheDocument();
    expect(screen.queryByText("<script>alert(1)</script>")).not.toBeInTheDocument();
  });

  it("fails closed for overlong correlation values", async () => {
    server.use(http.get("/api/v1/orders/active", () => HttpResponse.json([{
      ...activeOrder,
      smartup_id: "7".repeat(41),
      skladbot_request_number: `WH-R-${"A".repeat(81)}`,
      skladbot_request_id: "9".repeat(21),
    }])));

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={vi.fn()} onNotice={vi.fn()} />);

    expect(await screen.findByText("Smartup ID: —")).toBeInTheDocument();
    expect(screen.getByText("Заявка SkladBot: —")).toBeInTheDocument();
  });

  it("shows honest placeholders when correlations are missing", async () => {
    server.use(http.get("/api/v1/orders/active", () => HttpResponse.json([{
      ...activeOrder,
      smartup_id: "",
      skladbot_request_number: "",
      skladbot_request_id: "",
    }])));

    render(<WarehousePanel config={config} canWrite actor="operator-test" onError={vi.fn()} onNotice={vi.fn()} />);

    expect(await screen.findByText("Smartup ID: —")).toBeInTheDocument();
    expect(screen.getByText("Заявка SkladBot: —")).toBeInTheDocument();
    expect(screen.queryByText(/Заявка возврата:/)).not.toBeInTheDocument();
  });
});
