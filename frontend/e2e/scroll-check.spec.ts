import { expect, test } from "@playwright/test";
import { installSyntheticApi } from "./synthetic-api";

test("широкие таблицы прокручиваются, а не обрезаются", async ({ page }) => {
  await installSyntheticApi(page);
  await page.setViewportSize({ width: 1440, height: 900 });

  const check = async (selector: string, label: string) => {
    const box = page.locator(selector).first();
    await box.waitFor();
    const state = await box.evaluate((el) => ({
      client: el.clientWidth,
      scroll: el.scrollWidth,
      overflowX: getComputedStyle(el).overflowX,
    }));
    const reachable = state.overflowX === "auto" || state.overflowX === "scroll" || state.scroll <= state.client;
    console.log(`  ${label}: client ${state.client}, scroll ${state.scroll}, overflow-x ${state.overflowX} -> ${reachable ? "доступна" : "ОБРЕЗАНА"}`);
    expect(reachable, `${label} должна быть доступна целиком`).toBe(true);
  };

  await page.goto("/admin");
  await page.getByRole("heading", { name: "Позиции заказов" }).waitFor();
  await check(".admin-table-wrap", "заказы");

  await page.getByRole("button", { name: "Клиенты" }).click();
  await page.getByRole("heading", { name: "Клиенты и таймслоты" }).waitFor();
  await check(".client-points-table-wrap", "клиенты");

  await page.getByRole("button", { name: "История действий" }).click();
  await page.getByRole("button", { name: "Инциденты" }).click();
  await page.getByRole("heading", { name: "Инциденты и очередь" }).waitFor();
  await check(".admin-center-table-wrap", "инциденты");
});
