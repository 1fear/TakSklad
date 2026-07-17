import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import {
  adminTable,
  anonymousSession,
  firstAdminRow,
  secondAdminRow,
} from "./fixtures";
import { defaultHandlers, server } from "./server";

beforeEach(() => server.use(...defaultHandlers));

async function renderAuthenticatedApp() {
  const user = userEvent.setup();
  const view = render(<App />);
  await screen.findByRole("heading", { name: "Позиции заказов" });
  await screen.findByText(firstAdminRow.client);
  return { user, ...view };
}

describe("login and session characterization", () => {
  it("shows a deterministic loading gate before the anonymous login surface", async () => {
    server.use(
      http.get("/api/v1/auth/session", async () => {
        await delay(40);
        return HttpResponse.json(anonymousSession);
      }),
    );

    render(<App />);

    expect(screen.getByText("Загружаем доступ...")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Вход в панель" })).toBeInTheDocument();
    expect(screen.getByLabelText("Телефон")).toHaveAttribute("autocomplete", "username");
    expect(screen.getByLabelText("Пароль")).toHaveAttribute("autocomplete", "current-password");
  });

  it("maps a rejected login to the current operator-facing 401 message", async () => {
    server.use(
      http.get("/api/v1/auth/session", () => HttpResponse.json(anonymousSession)),
      http.post("/api/v1/auth/login", () => HttpResponse.json(
        { detail: { code: "auth_login_failed", message: "invalid credentials" } },
        { status: 401, statusText: "Unauthorized" },
      )),
    );
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "Вход в панель" });

    await user.type(screen.getByLabelText("Телефон"), "+998 90 111 22 33");
    await user.type(screen.getByLabelText("Пароль"), "synthetic-password");
    await user.click(screen.getByRole("button", { name: "Войти" }));

    expect(await screen.findByText("Телефон или пароль не подходят")).toBeInTheDocument();
  });

  it("establishes a session and clears protected content when it later expires", async () => {
    let tableRequests = 0;
    server.use(
      http.get("/api/v1/admin/table", () => {
        tableRequests += 1;
        if (tableRequests > 1) {
          return HttpResponse.json(
            { detail: { code: "session_expired", message: "expired" } },
            { status: 401, statusText: "Unauthorized" },
          );
        }
        return HttpResponse.json(adminTable([firstAdminRow]));
      }),
    );
    const { user } = await renderAuthenticatedApp();

    expect(screen.getByText(/admin/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Обновить" }));

    expect(await screen.findByText("Сессия закончилась. Войдите снова.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Позиции заказов" })).not.toBeInTheDocument();
  });

  it("logs in from an anonymous session and loads only synthetic panel data", async () => {
    server.use(http.get("/api/v1/auth/session", () => HttpResponse.json(anonymousSession)));
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "Вход в панель" });

    await user.type(screen.getByLabelText("Телефон"), "+998901234567");
    await user.type(screen.getByLabelText("Пароль"), "synthetic-password");
    await user.click(screen.getByRole("button", { name: "Войти" }));

    expect(await screen.findByRole("heading", { name: "Позиции заказов" })).toBeInTheDocument();
    expect(await screen.findByText(firstAdminRow.client)).toBeInTheDocument();
  });
});

describe("authenticated control-surface characterization", () => {
  it("shows canonical order references and return link regardless of stale status bucket", async () => {
    const row = {
      ...firstAdminRow,
      smartup_id: "731, 732",
      status_bucket: "active",
      skladbot_return_request_number: "WR-RET-1",
      skladbot_return_request_id: "903",
    };
    server.use(http.get("/api/v1/admin/table", () => HttpResponse.json(adminTable([row]))));

    await renderAuthenticatedApp();

    expect(screen.getByText("Smartup ID: 731, 732")).toBeInTheDocument();
    expect(screen.getByText("Заявка SkladBot: WH-R-TEST-1")).toBeInTheDocument();
    expect(screen.getByText("Заявка возврата: WR-RET-1")).toBeInTheDocument();
  });

  it("keeps operational SkladBot status lines under canonical references", async () => {
    const rows = [
      { ...firstAdminRow, item_id: "queued", order_id: "queued", skladbot_request_number: "", skladbot_request_id: "", skladbot_status: "create_queued" },
      { ...firstAdminRow, item_id: "ambiguous", order_id: "ambiguous", client: "Ambiguous client", skladbot_request_number: "", skladbot_request_id: "", skladbot_status: "ambiguous" },
      { ...firstAdminRow, item_id: "manual-review", order_id: "manual-review", client: "Manual review client", skladbot_request_number: "", skladbot_request_id: "", skladbot_status: "manual_review" },
      { ...firstAdminRow, item_id: "error", order_id: "error", client: "Error client", skladbot_request_number: "", skladbot_request_id: "", skladbot_status: "error" },
      {
        ...firstAdminRow,
        item_id: "return-queued",
        order_id: "return-queued",
        client: "Return queued client",
        skladbot_return_request_id: "2001",
        skladbot_return_request_number: "WR-2001",
        skladbot_return_status: "queued",
      },
      {
        ...firstAdminRow,
        item_id: "return-error",
        order_id: "return-error",
        client: "Return error client",
        skladbot_return_request_id: "2002",
        skladbot_return_request_number: "WR-2002",
        skladbot_return_status: "create_failed",
      },
      {
        ...firstAdminRow,
        item_id: "return-manual-review",
        order_id: "return-manual-review",
        client: "Return manual review client",
        skladbot_return_request_id: "2003",
        skladbot_return_request_number: "WR-2003",
        skladbot_return_status: "manual_review",
      },
    ];
    server.use(http.get("/api/v1/admin/table", () => HttpResponse.json(adminTable(rows))));

    await renderAuthenticatedApp();

    expect(screen.getByText("Создание в очереди")).toBeInTheDocument();
    expect(screen.getAllByText("Неоднозначно — ручная проверка")).toHaveLength(2);
    expect(screen.getByText("Ошибка")).toBeInTheDocument();
    expect(screen.getByText("Возврат: WR-2001 · В очереди")).toBeInTheDocument();
    expect(screen.getByText("Возврат: WR-2002 · Ошибка создания")).toBeInTheDocument();
    expect(screen.getByText("Возврат: WR-2003 · Неоднозначно — ручная проверка")).toBeInTheDocument();
    expect(screen.queryByText("manual_review")).not.toBeInTheDocument();
    expect(
      within(screen.getByText("Manual review client").closest("tr") as HTMLElement).queryByText("Без номера"),
    ).not.toBeInTheDocument();
  });

  it("shows canonical correlations for persisted dry-run orders and honest missing values", async () => {
    const { user } = await renderAuthenticatedApp();

    await user.click(screen.getByRole("button", { name: "История действий" }));
    await user.click(screen.getByRole("button", { name: "SkladBot dry-run" }));

    expect(await screen.findByRole("heading", { name: "SkladBot dry-run" })).toBeInTheDocument();
    expect(screen.getByText("Smartup ID: 731, 732")).toBeInTheDocument();
    expect(screen.getByText("Заявка SkladBot: WH-R-DRY-1")).toBeInTheDocument();
    expect(screen.getByText("Заявка возврата: WR-DRY-1")).toBeInTheDocument();
    expect(screen.getByText("Smartup ID: —")).toBeInTheDocument();
    expect(screen.getByText("Заявка SkladBot: —")).toBeInTheDocument();
  });

  it("keeps server-side filters and pagination parameters deterministic", async () => {
    const requests: URL[] = [];
    server.use(
      http.get("/api/v1/admin/table", ({ request }) => {
        const url = new URL(request.url);
        requests.push(url);
        const offset = Number(url.searchParams.get("offset") || "0");
        return url.searchParams.get("cursor") || offset > 0
          ? HttpResponse.json(adminTable([secondAdminRow], { offset, row_count: 1, total_rows: 2, has_more: false }))
          : HttpResponse.json(adminTable([firstAdminRow], { total_rows: 2, has_more: true, next_cursor: "synthetic-page-2" }));
      }),
    );
    const { user } = await renderAuthenticatedApp();

    await user.click(screen.getByRole("button", { name: /Загрузить еще 1/ }));
    expect(await screen.findByText(secondAdminRow.client)).toBeInTheDocument();
    expect(requests.some((url) => url.searchParams.get("cursor") === "synthetic-page-2")).toBe(true);

    await user.type(screen.getByRole("searchbox", { name: "Поиск заказов" }), "Альфа");
    await user.selectOptions(screen.getByLabelText("Фильтр сканирования"), "not_started");
    await waitFor(() => {
      expect(requests.some((url) => (
        url.searchParams.get("search") === "Альфа"
        && url.searchParams.get("scan_state") === "not_started"
        && url.searchParams.get("status_bucket") === "active"
      ))).toBe(true);
    });
  });

  it("submits the characterized order action with confirmation and audit fields", async () => {
    let actionBody: Record<string, unknown> | undefined;
    server.use(
      http.post("/api/v1/admin/orders/:orderId/archive-without-kiz", async ({ request }) => {
        actionBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json({ status: "archive_no_kiz" });
      }),
    );
    vi.spyOn(window, "prompt").mockReturnValue("Синтетическая причина");
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const { user } = await renderAuthenticatedApp();

    await user.click(screen.getByRole("checkbox", { name: `Выбрать заказ ${firstAdminRow.client}` }));
    await user.click(screen.getByRole("button", { name: "В архив без КИЗов" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Заказ перенесен в архив без КИЗов");
    expect(actionBody).toMatchObject({
      reason: "Синтетическая причина",
      actor: "web",
      source: "web",
      expected_updated_at: firstAdminRow.updated_at,
    });
    expect(actionBody?.idempotency_key).toEqual(expect.any(String));
  });

  it("announces a failed order action through an assertive alert", async () => {
    server.use(
      http.post("/api/v1/admin/orders/:orderId/archive-without-kiz", () => (
        HttpResponse.json({ message: "Синтетическая ошибка действия" }, { status: 500 })
      )),
    );
    vi.spyOn(window, "prompt").mockReturnValue("Синтетическая причина");
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const { user } = await renderAuthenticatedApp();

    await user.click(screen.getByRole("checkbox", { name: `Выбрать заказ ${firstAdminRow.client}` }));
    await user.click(screen.getByRole("button", { name: "В архив без КИЗов" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveAttribute("aria-live", "assertive");
    expect(alert).toHaveTextContent("Синтетическая ошибка действия");
  });

  it("filters incidents and resolves the selected incident with an explicit reason", async () => {
    let resolveBody: Record<string, unknown> | undefined;
    server.use(
      http.post("/api/v1/admin/incidents/:incidentId/status", async ({ request }) => {
        resolveBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json({ status: "resolved" });
      }),
    );
    const { user } = await renderAuthenticatedApp();
    await user.click(screen.getByRole("button", { name: "История действий" }));
    await user.click(screen.getByRole("button", { name: "Инциденты" }));

    expect(await screen.findByRole("heading", { name: "Инциденты и очередь" })).toBeInTheDocument();
    expect(screen.getAllByText("Синтетическая ошибка импорта")).toHaveLength(2);
    await user.selectOptions(screen.getByLabelText("Фильтр уровня инцидента"), "info");
    expect(screen.getByText("Инцидентов нет")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Фильтр уровня инцидента"), "all");

    await user.type(screen.getByLabelText("Причина действия"), "Проверено в unit test");
    await user.click(screen.getByRole("button", { name: "Resolve" }));
    expect(await screen.findByText("Инцидент закрыт")).toBeInTheDocument();
    expect(resolveBody).toEqual({
      status: "resolved",
      reason: "Проверено в unit test",
      actor: "web",
      source: "web",
    });
  });

  it("filters client points, expands order history and saves a timeslot", async () => {
    let timeslotBody: Record<string, unknown> | undefined;
    server.use(
      http.post("/api/v1/admin/client-points/timeslot", async ({ request }) => {
        timeslotBody = await request.json() as Record<string, unknown>;
        return HttpResponse.json({ status: "saved" });
      }),
    );
    const { user } = await renderAuthenticatedApp();
    await user.click(screen.getByRole("button", { name: "Клиенты" }));

    expect(await screen.findByRole("heading", { name: "Клиенты и таймслоты" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /История заказов Клиент Альфа: 2 заказов · 1 возвратов/ }));
    expect(await screen.findByText("Тестовый товар")).toBeInTheDocument();
    expect(screen.getByText("Smartup ID: 731, 732")).toBeInTheDocument();
    expect(screen.getByText("Заявка SkladBot: WH-R-TEST-1")).toBeInTheDocument();
    expect(screen.getByText("Smartup ID: 733")).toBeInTheDocument();
    expect(screen.getByText("Заявка SkladBot: ID 1002")).toBeInTheDocument();
    expect(screen.getByText("Smartup ID: 734")).toBeInTheDocument();
    expect(screen.getByText("Заявка SkladBot: —")).toBeInTheDocument();
    expect(screen.getByText("Заявка возврата: WR-RET-1")).toBeInTheDocument();

    const clientSearch = screen.getByRole("searchbox", { name: "Поиск клиентов" });
    await user.type(clientSearch, "Несуществующий клиент");
    expect(screen.getByText("Нет данных")).toBeInTheDocument();
    await user.clear(clientSearch);

    await user.click(screen.getByRole("button", { name: /Редактировать таймслот/ }));
    const from = screen.getByLabelText("Доставка с");
    await user.clear(from);
    await user.type(from, "10:30");
    await user.click(screen.getByRole("button", { name: /Сохранить таймслот/ }));

    expect(await screen.findByText("Таймслот сохранен")).toBeInTheDocument();
    expect(timeslotBody).toMatchObject({
      client_name: "Клиент Альфа",
      delivery_from: "10:30",
      delivery_to: "12:00",
      actor: "web",
    });
  });

  it("renders the current empty table state without inventing placeholder data", async () => {
    server.use(http.get("/api/v1/admin/table", () => HttpResponse.json(adminTable([]))));
    render(<App />);

    const heading = await screen.findByRole("heading", { name: "Позиции заказов" });
    const tablePanel = heading.closest("section");
    expect(tablePanel).not.toBeNull();
    expect(within(tablePanel as HTMLElement).getByText("Нет данных")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Выбрать видимые заказы" })).toBeDisabled();
  });
});
