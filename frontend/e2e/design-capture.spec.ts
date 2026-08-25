/**
 * Съёмка экранов для дизайн-работы. Не проверяет поведение, только снимает.
 * Кладёт файлы в test-results/design/<label>/<screen>.png
 * Запуск: npx playwright test e2e/design-capture.spec.ts
 * Метка задаётся через DESIGN_LABEL, по умолчанию "before".
 */
import { test, type Page } from "@playwright/test";
import { installSyntheticApi } from "./synthetic-api";

const LABEL = process.env.DESIGN_LABEL ?? "before";
const DIR = `test-results/design/${LABEL}`;
const WIDE = { width: 1440, height: 900 };

async function shot(page: Page, name: string) {
  await page.waitForTimeout(350);
  await page.screenshot({ path: `${DIR}/${name}.png`, fullPage: false });
}

test.use({ viewport: WIDE });

test("логин", async ({ page }) => {
  await installSyntheticApi(page, { authenticated: false });
  await page.goto("/");
  await page.getByRole("heading", { name: "Вход в складскую web-панель" }).waitFor();
  await shot(page, "01-login");
});

test("операторский контур и сканер КИЗ", async ({ page }) => {
  const api = await installSyntheticApi(page);
  page.on("dialog", (d) => d.accept());
  await page.goto("/");
  await page.getByRole("heading", { name: "Склад · PostgreSQL" }).waitFor();
  await shot(page, "02-operator");

  const scanner = page.getByRole("textbox", { name: "КИЗ" });
  await scanner.fill("0104006396053947217SYNTH1XXXXXXXXXX");
  await scanner.press("Enter");
  await page.getByText("КИЗ подтверждён и записан.").waitFor();
  await shot(page, "03-kiz-scanned");

  await scanner.fill("0104006396053947217SYNTH2XXXXXXXXXX");
  await scanner.press("Enter");
  await page.getByRole("button", { name: "Завершить заказ" }).waitFor({ state: "attached" });
  await page.getByRole("button", { name: "Завершить заказ" }).click();
  await page.getByRole("dialog", { name: "Печать сводного листа" }).waitFor();
  await shot(page, "04-print-dialog");
  void api;
});

test("админка, позиции заказов", async ({ page }) => {
  await installSyntheticApi(page);
  await page.goto("/admin");
  await page.getByRole("heading", { name: "Позиции заказов" }).waitFor();
  await shot(page, "05-admin-orders");
});

test("админка, клиенты и таймслоты", async ({ page }) => {
  await installSyntheticApi(page);
  await page.goto("/admin");
  await page.getByRole("heading", { name: "Позиции заказов" }).waitFor();
  await page.getByRole("button", { name: "Клиенты" }).click();
  await page.getByRole("heading", { name: "Клиенты и таймслоты" }).waitFor();
  await shot(page, "06-admin-clients");
});

test("админка, история действий", async ({ page }) => {
  await installSyntheticApi(page);
  await page.goto("/admin");
  await page.getByRole("heading", { name: "Позиции заказов" }).waitFor();
  await page.getByRole("button", { name: "История действий" }).click();
  await page.waitForTimeout(600);
  await shot(page, "07-admin-history");
});

test("админка, инциденты и очередь", async ({ page }) => {
  await installSyntheticApi(page);
  await page.goto("/admin");
  await page.getByRole("heading", { name: "Позиции заказов" }).waitFor();
  // Инциденты вложены в раздел «История действий», его надо сначала раскрыть
  await page.getByRole("button", { name: "История действий" }).click();
  await page.getByRole("button", { name: "Инциденты" }).click();
  await page.getByRole("heading", { name: "Инциденты и очередь" }).waitFor();
  await shot(page, "08-admin-incidents");
});

test("админка, календарь", async ({ page }) => {
  await installSyntheticApi(page);
  await page.goto("/admin");
  await page.getByRole("heading", { name: "Позиции заказов" }).waitFor();
  await page.getByRole("button", { name: "Календарь" }).click();
  await page.waitForTimeout(900);
  await shot(page, "09-admin-calendar");
});

test("админка, Smartup и маршруты", async ({ page }) => {
  await installSyntheticApi(page);
  await page.goto("/admin");
  await page.getByRole("heading", { name: "Позиции заказов" }).waitFor();
  await page.getByRole("button", { name: "Smartup" }).click();
  await page.waitForTimeout(900);
  await shot(page, "10-admin-smartup");
});
