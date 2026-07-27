import { expect, test as base, type Page } from "@playwright/test";
import { installSyntheticApi } from "./synthetic-api";

type BrowserConsolePolicy = {
  problems: string[];
};

const test = base.extend<{ browserConsole: BrowserConsolePolicy }>({
  browserConsole: [async ({ page }, use) => {
    const problems: string[] = [];
    const externalRequests: string[] = [];
    page.on("request", (request) => {
      const url = request.url();
      if (!url.startsWith("http://127.0.0.1:4173/") && !url.startsWith("data:") && !url.startsWith("blob:")) {
        externalRequests.push(url);
      }
    });
    page.on("console", (message) => {
      if (["warning", "error"].includes(message.type())) problems.push(`console.${message.type()}: ${message.text()}`);
    });
    page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));
    await use({ problems });
    expect(problems, "Browser console warnings/errors and uncaught exceptions").toEqual([]);
    expect(externalRequests, "Synthetic smoke must not use production or other external network").toEqual([]);
  }, { auto: true }],
});

async function openWarehouseSurface(page: Page) {
  const warehouseHeading = page.getByRole("heading", { name: "Склад · PostgreSQL" });
  const operatorSurfaceHeading = page.getByRole("heading", { name: "Операторский складской контур" });
  if (await warehouseHeading.isVisible().catch(() => false)) return warehouseHeading;
  if (await operatorSurfaceHeading.isVisible().catch(() => false)) {
    await expect(warehouseHeading).toBeVisible();
    return warehouseHeading;
  }

  const loginHeading = page.getByRole("heading", { name: "Вход в складскую web-панель" });
  if (await loginHeading.isVisible().catch(() => false)) {
    await page.locator("input[inputmode=\"tel\"]").fill("+998 90 000 00 01");
    await page.locator("input[type=\"password\"]").fill("synthetic-password");
    await page.getByRole("button", { name: "Войти" }).click();
    await expect(warehouseHeading).toBeVisible();
    return warehouseHeading;
  }

  await page.goto("/");
  await expect(warehouseHeading).toBeVisible();
  return warehouseHeading;
}

test("@smoke login and session use only a synthetic user", async ({ page }) => {
  const api = await installSyntheticApi(page, { authenticated: false });
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Вход в складскую web-панель" })).toBeVisible();
  await page.locator('input[inputmode="tel"]').fill("+998 90 000 00 01");
  await page.locator('input[type="password"]').fill("synthetic-password");
  await page.getByRole("button", { name: "Войти" }).click();

  await expect(page.getByRole("heading", { name: "Склад · PostgreSQL" })).toBeVisible();
  await expect(page.getByText("Альфа Тест").first()).toBeVisible();
  expect(api.requests).toContain("POST /api/v1/auth/login");
  await page.getByRole("button", { name: "Выйти" }).click();
  await expect(page.getByRole("heading", { name: "Вход в складскую web-панель" })).toBeVisible();
});

test("@smoke warehouse scanner, completion and print stay inside the synthetic API", async ({ page }) => {
  const api = await installSyntheticApi(page);
  await page.addInitScript(() => {
    (window as typeof window & { __printCalls?: number }).__printCalls = 0;
    window.print = () => {
      (window as typeof window & { __printCalls?: number }).__printCalls = ((window as typeof window & { __printCalls?: number }).__printCalls ?? 0) + 1;
    };
  });
  page.on("dialog", async (dialog) => dialog.accept());
  await page.goto("/");

  await openWarehouseSurface(page);
  await expect(page.getByText("Smartup ID: 261000001")).toBeVisible();

  const scanner = page.getByRole("textbox", { name: "КИЗ" });
  await scanner.fill("0104006396053947217SYNTH1");
  await scanner.press("Enter");
  await expect.poll(() => api.scans).toBe(1);
  await expect(page.getByText("КИЗ подтверждён и записан.")).toBeVisible();

  await scanner.fill("0104006396053947217SYNTH2");
  await scanner.press("Enter");
  await expect.poll(() => api.scans).toBe(2);
  await expect(page.getByRole("button", { name: "Завершить заказ" })).toBeEnabled();

  await page.getByRole("button", { name: "Завершить заказ" }).click();
  await expect.poll(() => api.completes).toBe(1);
  await expect(page.getByRole("dialog", { name: "Печать сводного листа" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Открыть печать" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => (window as typeof window & { __printCalls?: number }).__printCalls ?? 0)).toBe(0);

  await page.getByRole("dialog", { name: "Печать сводного листа" })
    .getByRole("button", { name: "Печать", exact: true })
    .click();
  await expect.poll(() => page.evaluate(() => (window as typeof window & { __printCalls?: number }).__printCalls ?? 0)).toBe(1);
});

test("@smoke incidents and client-point actions stay inside synthetic API", async ({ page }) => {
  const api = await installSyntheticApi(page);
  await page.goto("/admin");
  await expect(page.getByRole("heading", { name: "Позиции заказов" })).toBeVisible();

  await page.getByRole("button", { name: "Клиенты" }).click();
  await expect(page.getByRole("heading", { name: "Клиенты и таймслоты" })).toBeVisible();
  await page.getByLabel("Поиск клиентов").fill("Альфа");
  await page.getByRole("button", { name: /Редактировать таймслот/ }).click();
  await page.getByLabel("Доставка с").fill("09:00");
  await page.getByRole("button", { name: /Сохранить таймслот/ }).click();
  await expect.poll(() => api.clientUpdates).toBe(1);
  await expect(page.getByRole("status")).toContainText("Таймслот сохранен");

  await page.getByRole("button", { name: "История действий" }).click();
  await page.getByRole("button", { name: "Инциденты" }).click();
  await expect(page.getByRole("heading", { name: "Инциденты и очередь" })).toBeVisible();
  await page.getByLabel("Поиск инцидентов и очереди").fill("Синтетический");
  await page.getByLabel("Фильтр уровня инцидента").selectOption("warning");
  await page.getByPlaceholder("Например: проверил импорт, можно повторить").fill("Synthetic incident resolution");
  await page.getByRole("button", { name: "Resolve" }).click();
  await expect.poll(() => api.incidentUpdates).toBe(1);
  await expect(page.getByRole("status")).toContainText("Инцидент закрыт");
});

test("@network-contract critical requests stay bounded and hidden panels stay lazy", async ({ page }) => {
  const api = await installSyntheticApi(page);
  await page.goto("/admin");
  await expect(page.getByRole("heading", { name: "Позиции заказов" })).toBeVisible();

  const critical = api.requests.filter((request) => (
    request.includes("/api/v1/admin/table")
    || request.includes("/api/v1/admin/dashboard/day-summary")
  ));
  expect(critical).toHaveLength(2);
  expect(api.requests.filter((request) => request.includes("/api/v1/auth/session"))).toHaveLength(1);
  for (const hiddenPath of ["/api/v1/imports", "/api/v1/admin/client-points", "/api/v1/admin/events", "/api/v1/admin/incidents"]) {
    expect(api.requests.some((request) => request.includes(hiddenPath))).toBe(false);
  }

  const tableRequestsBeforeSearch = api.requests.filter((request) => request.includes("/api/v1/admin/table")).length;
  const search = page.getByLabel("Поиск заказов");
  await search.fill("А");
  await search.fill("Аль");
  await search.fill("Альфа");
  await expect(page.getByText("Результат Альфа").first()).toBeVisible();
  expect(api.requests.filter((request) => request.includes("/api/v1/admin/table")).length).toBe(tableRequestsBeforeSearch + 1);

  await page.getByRole("button", { name: "Клиенты" }).click();
  await expect(page.getByRole("heading", { name: "Клиенты и таймслоты" })).toBeVisible();
  await expect.poll(() => api.requests.filter((request) => request === "GET /api/v1/admin/client-points").length).toBe(1);
  expect(api.requests.some((request) => request.includes("/api/v1/imports"))).toBe(false);
});
