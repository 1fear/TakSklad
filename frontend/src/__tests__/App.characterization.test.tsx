import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App";
import {
  adminTable,
  anonymousSession,
  authenticatedSession,
  clientPoint,
  firstAdminRow,
  logisticsCalendar,
  logisticsCalendarDayOrders,
  secondAdminRow,
} from "./fixtures";
import { defaultHandlers, server } from "./server";

beforeEach(() => {
  server.use(...defaultHandlers);
  window.history.pushState({}, "", "/");
});

function setPath(pathname: string) {
  window.history.pushState({}, "", pathname);
}

async function renderAuthenticatedAdminApp() {
  setPath("/admin");
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
    expect(await screen.findByRole("heading", { name: "Вход в складскую web-панель" })).toBeInTheDocument();
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
    await screen.findByRole("heading", { name: "Вход в складскую web-панель" });

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
    const { user } = await renderAuthenticatedAdminApp();

    expect(screen.getByText(/admin/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Обновить" }));

    expect(await screen.findByText("Сессия закончилась. Войдите снова.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Позиции заказов" })).not.toBeInTheDocument();
  });

  it("logs in from an anonymous session and loads only synthetic panel data", async () => {
    server.use(http.get("/api/v1/auth/session", () => HttpResponse.json(anonymousSession)));
    setPath("/admin");
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "Вход в панель управления" });

    await user.type(screen.getByLabelText("Телефон"), "+998901234567");
    await user.type(screen.getByLabelText("Пароль"), "synthetic-password");
    await user.click(screen.getByRole("button", { name: "Войти" }));

    expect(await screen.findByRole("heading", { name: "Позиции заказов" })).toBeInTheDocument();
    expect(await screen.findByText(firstAdminRow.client)).toBeInTheDocument();
  });

  it("chooses the operator workspace on root and keeps the admin link visible", async () => {
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Операторский складской контур" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Склад · PostgreSQL" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Панель управления" })).toHaveAttribute("href", "/admin");

    await user.click(screen.getByRole("button", { name: "Обновить" }));
    expect(await screen.findByText("Активные заказы обновлены: 1")).toBeInTheDocument();
  });

  it("links to /admin when a warehouse user has an imports-only admin section", async () => {
    server.use(http.get("/api/v1/auth/session", () => HttpResponse.json({
      ...authenticatedSession,
      permissions: ["warehouse:read", "imports:read"],
    })));

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Операторский складской контур" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Панель управления" })).toHaveAttribute("href", "/admin");
  });

  it("shows an access-denied link to /admin when warehouse:read is missing on root", async () => {
    server.use(http.get("/api/v1/auth/session", () => HttpResponse.json({
      ...authenticatedSession,
      permissions: authenticatedSession.permissions.filter((permission) => permission !== "warehouse:read"),
    })));

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Нет доступа к складской web-панели" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Открыть панель управления" })).toHaveAttribute("href", "/admin");
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

    await renderAuthenticatedAdminApp();

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

    await renderAuthenticatedAdminApp();

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
    const { user } = await renderAuthenticatedAdminApp();

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
    const { user } = await renderAuthenticatedAdminApp();

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
    const { user } = await renderAuthenticatedAdminApp();

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
    const { user } = await renderAuthenticatedAdminApp();

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
    const { user } = await renderAuthenticatedAdminApp();
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
    const { user } = await renderAuthenticatedAdminApp();
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
    expect(await screen.findByText("Нет данных")).toBeInTheDocument();
    await user.clear(clientSearch);
    expect(await screen.findByText("Клиент Альфа")).toBeInTheDocument();

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

  it("sends the client search to the backend so Smartup and SkladBot ids match", async () => {
    const queries: string[] = [];
    server.use(
      http.get("/api/v1/admin/client-points", ({ request }) => {
        const query = new URL(request.url).searchParams.get("query") ?? "";
        if (query) queries.push(query);
        return HttpResponse.json(query === "266627708" ? [] : [clientPoint]);
      }),
    );
    const { user } = await renderAuthenticatedAdminApp();
    await user.click(screen.getByRole("button", { name: "Клиенты" }));
    expect(await screen.findByRole("heading", { name: "Клиенты и таймслоты" })).toBeInTheDocument();

    const clientSearch = screen.getByRole("searchbox", { name: "Поиск клиентов" });
    await user.type(clientSearch, "266627707");
    await waitFor(() => expect(queries).toContain("266627707"));
    expect(screen.getByText("Клиент Альфа")).toBeInTheDocument();

    await user.clear(clientSearch);
    await user.type(clientSearch, "266627708");
    expect(await screen.findByText("Нет данных")).toBeInTheDocument();
    expect(queries).toContain("266627708");
  });

  it("renders the current empty table state without inventing placeholder data", async () => {
    server.use(http.get("/api/v1/admin/table", () => HttpResponse.json(adminTable([]))));
    setPath("/admin");
    render(<App />);

    const heading = await screen.findByRole("heading", { name: "Позиции заказов" });
    const tablePanel = heading.closest("section");
    expect(tablePanel).not.toBeNull();
    expect(within(tablePanel as HTMLElement).getByText("Нет данных")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Выбрать видимые заказы" })).toBeDisabled();
  });

  it("клик по дню календаря раскрывает детализацию именно выбранного дня", async () => {
    server.use(http.get("/api/v1/admin/logistics-calendar", () => HttpResponse.json({
      ...logisticsCalendar,
      days: [
        { ...logisticsCalendar.days[0], date: "2026-07-10", weekday: 4 },
        {
          ...logisticsCalendar.days[0],
          date: "2026-07-13",
          weekday: 0,
          orders_count: 0,
          active_orders: 0,
          completed_orders: 0,
          returned_orders: 0,
          planned_blocks: 0,
          city_orders: 0,
          region_orders: 0,
          city_returns: 0,
          region_returns: 0,
          city_blocks: 0,
          region_blocks: 0,
          excluded_orders: 0,
        },
      ],
    })));
    const { user } = await renderAuthenticatedAdminApp();
    await user.click(screen.getByRole("button", { name: "Календарь" }));

    // фолбэк без единого клика уводит на день с заказами, 10 июля, а не на 13-е,
    // у которого заказов нет, поэтому попасть на 13-е можно только настоящим кликом
    expect(await screen.findByText("10.07.2026, пятница")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /13\.07\.2026/ }));

    expect(await screen.findByText("13.07.2026, понедельник")).toBeInTheDocument();
    expect(await screen.findByRole("tab", { name: /Город/ })).toBeInTheDocument();
  });

  it("листает детализацию дня стрелками внутри загруженного месяца", async () => {
    server.use(http.get("/api/v1/admin/logistics-calendar", () => HttpResponse.json({
      ...logisticsCalendar,
      days: [
        { ...logisticsCalendar.days[0], date: "2026-07-10", weekday: 4 },
        { ...logisticsCalendar.days[0], date: "2026-07-11", weekday: 5 },
      ],
    })));
    const { user } = await renderAuthenticatedAdminApp();
    await user.click(screen.getByRole("button", { name: "Календарь" }));

    expect(await screen.findByText("10.07.2026, пятница")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Предыдущий день" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Следующий день" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Следующий день" }));
    expect(await screen.findByText("11.07.2026, суббота")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Предыдущий день" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Следующий день" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Предыдущий день" }));
    expect(await screen.findByText("10.07.2026, пятница")).toBeInTheDocument();
  });

  it("держит показанный день и запрошенные заказы на одной дате при смене месяца", async () => {
    const requestedDayDates: string[] = [];
    server.use(
      http.get("/api/v1/admin/logistics-calendar", ({ request }) => {
        const month = new URL(request.url).searchParams.get("month") || "";
        if (month === "2026-09") {
          return HttpResponse.json({
            ...logisticsCalendar,
            month: "2026-09",
            days: [{ ...logisticsCalendar.days[0], date: "2026-09-05", weekday: 5 }],
          });
        }
        return HttpResponse.json(logisticsCalendar);
      }),
      http.get("/api/v1/admin/logistics-calendar/day/:date/orders", ({ params }) => {
        const date = params.date as string;
        requestedDayDates.push(date);
        return HttpResponse.json({ ...logisticsCalendarDayOrders, date, orders: [] });
      }),
    );
    const { user } = await renderAuthenticatedAdminApp();
    await user.click(screen.getByRole("button", { name: "Календарь" }));

    // выбранная дата по умолчанию (сегодня) не входит в дни фикстуры, срабатывает
    // фолбэк на 10 июля, запрос заказов обязан уйти именно за 10 июля, а не за
    // исходную (нигде не показанную) дату
    expect(await screen.findByText("10.07.2026, пятница")).toBeInTheDocument();
    await waitFor(() => expect(requestedDayDates.at(-1)).toBe("2026-07-10"));

    const monthInput = screen.getByLabelText("Месяц календаря логистики");
    fireEvent.change(monthInput, { target: { value: "2026-09" } });

    // после смены месяца выбранный день обязан переехать в новый месяц, а запрос
    // заказов обязан уйти за дату, которая реально показана в шапке детализации
    expect(await screen.findByText("05.09.2026, суббота")).toBeInTheDocument();
    await waitFor(() => expect(requestedDayDates.at(-1)).toBe("2026-09-05"));
  });

  it("показывает индикатор загрузки, а не пустое состояние, на пути с поправкой даты", async () => {
    const requestedDayDates: string[] = [];
    server.use(
      http.get("/api/v1/admin/logistics-calendar", () => HttpResponse.json(logisticsCalendar)),
      http.get("/api/v1/admin/logistics-calendar/day/:date/orders", async ({ params }) => {
        const date = params.date as string;
        requestedDayDates.push(date);
        await delay(60);
        return HttpResponse.json({ ...logisticsCalendarDayOrders, date, orders: [] });
      }),
    );
    const { user } = await renderAuthenticatedAdminApp();
    await user.click(screen.getByRole("button", { name: "Календарь" }));

    // "сегодня" не входит в дни фикстуры, поэтому срабатывает та же поправка даты,
    // что и при смене месяца, именно на этом пути раньше на миг показывалось
    // "Заказов в этой зоне за день нет" вместо индикатора загрузки, потому что
    // второй прогон эффекта абортил запрос первого, а первый всё равно снимал
    // loading уже после аборта
    expect(await screen.findByText("10.07.2026, пятница")).toBeInTheDocument();
    expect(screen.getByText("Загрузка заказов дня")).toBeInTheDocument();
    expect(screen.queryByText("Заказов в этой зоне за день нет")).not.toBeInTheDocument();

    await waitFor(() => {
      expect(screen.queryByText("Загрузка заказов дня")).not.toBeInTheDocument();
    });
    expect(screen.getByText("Заказов в этой зоне за день нет")).toBeInTheDocument();

    // тот же самый первый прогон, что раньше ронял индикатор, ещё и слал второй
    // запрос за той же датой на бэкенд, задержка выше даёт время второму прогону
    // эффекта абортить первый до ответа, поэтому гонка воспроизводится детерминированно
    expect(requestedDayDates).toEqual(["2026-07-10"]);
  });

  it("показывает спокойное уведомление вместо красной ошибки, когда XLSX-отчёт логистики отвечает 404 на пустую зону", async () => {
    server.use(
      http.get("/api/v1/logistics/report", () => HttpResponse.json(
        { detail: { code: "empty_zone", message: "no orders for this zone and date" } },
        { status: 404, statusText: "Not Found" },
      )),
    );
    const { user } = await renderAuthenticatedAdminApp();
    await user.click(screen.getByRole("button", { name: "Календарь" }));
    await screen.findByText("10.07.2026, пятница");

    await user.click(screen.getByRole("button", { name: /Выгрузить XLSX город/ }));

    expect(await screen.findByRole("status")).toHaveTextContent("В этой зоне за день нет заказов для выгрузки");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(/Не удалось выгрузить отчёт логистики/)).not.toBeInTheDocument();
  });
});
