import { expect, test as base } from "@playwright/test";
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

test("@smoke login and session use only a synthetic user", async ({ page }) => {
  const api = await installSyntheticApi(page, { authenticated: false });
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Вход в панель" })).toBeVisible();
  await page.locator('input[inputmode="tel"]').fill("+998 90 000 00 01");
  await page.locator('input[type="password"]').fill("synthetic-password");
  await page.getByRole("button", { name: "Войти" }).click();

  await expect(page.getByRole("heading", { name: "Позиции заказов" })).toBeVisible();
  await expect(page.getByText("Альфа Тест").first()).toBeVisible();
  expect(api.requests).toContain("POST /api/v1/auth/login");
  await page.getByRole("button", { name: "Выйти" }).click();
  await expect(page.getByRole("heading", { name: "Вход в панель" })).toBeVisible();
});

test("@smoke table filters, pagination and order action are deterministic", async ({ page }) => {
  const api = await installSyntheticApi(page);
  page.on("dialog", async (dialog) => dialog.accept(dialog.type() === "prompt" ? "Synthetic e2e reason" : undefined));
  await page.goto("/");

  await expect(page.getByText("Альфа Тест").first()).toBeVisible();
  await page.getByRole("button", { name: /Загрузить еще/ }).click();
  await expect(page.getByText("Бета Тест").first()).toBeVisible();

  await page.getByLabel("Поиск заказов").fill("Гамма");
  await expect(page.getByText("Результат Гамма").first()).toBeVisible();
  await page.getByLabel("Фильтр статуса заказа").selectOption("all");
  await expect.poll(() => api.requests.some((request) => request.includes("status_bucket") === false && request.includes("search=%D0%93%D0%B0%D0%BC%D0%BC%D0%B0"))).toBe(true);

  await page.getByLabel("Поиск заказов").fill("");
  await expect(page.getByText("Альфа Тест").first()).toBeVisible();
  await page.getByLabel("Выбрать заказ Альфа Тест").check();
  await page.getByRole("button", { name: "Ресинк Google" }).click();
  await expect.poll(() => api.resyncs).toBe(1);
  await expect(page.getByRole("status")).toContainText("Google");
});

test("@smoke incidents and client-point actions stay inside synthetic API", async ({ page }) => {
  const api = await installSyntheticApi(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Позиции заказов" })).toBeVisible();

  await page.getByRole("button", { name: "Клиенты" }).click();
  await expect(page.getByRole("heading", { name: "Клиенты и таймслоты" })).toBeVisible();
  await page.getByLabel("Поиск клиентов").fill("Альфа");
  await page.getByRole("button", { name: "Редактировать" }).click();
  await page.getByLabel("Доставка с").fill("09:00");
  await page.getByRole("button", { name: "Сохранить" }).click();
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
