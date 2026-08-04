/**
 * The offline queue store against a real IndexedDB in a real browser.
 *
 * Unit tests run the queue contract on the in-memory store, which proves the
 * logic but not the storage. This spec proves the part that only a browser can
 * answer: that a queued scan survives a reload, that `block` moves an event
 * between two object stores atomically, and that a second tab on the same
 * origin sees the same queue.
 *
 * Codes here are synthetic and never real KIZ values.
 */

import { expect, test, type Page } from "@playwright/test";

import { installSyntheticApi } from "./synthetic-api";

const STORE_MODULE = "/src/features/warehouse/offline/queueStore.ts";
const TYPES_MODULE = "/src/features/warehouse/offline/queueTypes.ts";

const UNIT_CODE = "0104006396053947217AAAAAA";
const SECOND_UNIT_CODE = "0104006396053947217BBBBBB";

type QueueScript = string;

/**
 * Runs a snippet inside the page with `store`, `key` and `event` in scope.
 *
 * A fresh store instance per call is deliberate: it is the same situation as a
 * reloaded tab, so nothing can pass by staying in a JavaScript variable.
 */
async function runInQueue<T>(page: Page, dbName: string, script: QueueScript): Promise<T> {
  return page.evaluate<T, [string, string, string, string]>(
    async ([storeModule, typesModule, name, body]) => {
      const storeApi = await import(/* @vite-ignore */ storeModule);
      const typesApi = await import(/* @vite-ignore */ typesModule);
      const store = storeApi.createIndexedDbQueueStore(name, () => "2026-08-04T12:00:00+05:00");
      const key = typesApi.offlineEventKey;
      const event = (overrides: Record<string, unknown> = {}) => ({
        type: "scan",
        orderId: "order-1",
        orderItemId: "item-1",
        code: "0104006396053947217AAAAAA",
        actor: "operator-1",
        workstationId: "web",
        scannedAt: "2026-08-04T10:00:00+05:00",
        createdAt: "2026-08-04T10:00:00+05:00",
        attempts: 0,
        lastError: "",
        ...overrides,
      });
      const run = new Function("store", "key", "event", "storeApi", `return (async () => { ${body} })()`);
      return run(store, key, event, storeApi);
    },
    [STORE_MODULE, TYPES_MODULE, dbName, script],
  );
}

test.beforeEach(async ({ page }) => {
  await installSyntheticApi(page);
  await page.goto("/");
});

test("очередь переживает перезагрузку вкладки", async ({ page }) => {
  const dbName = "taksklad-e2e-reload";

  await runInQueue(page, dbName, `
    await store.enqueue(event({ code: "${UNIT_CODE}" }));
    await store.enqueue(event({ orderItemId: "item-2", code: "${SECOND_UNIT_CODE}" }));
  `);

  const beforeReload = await runInQueue<number>(page, dbName, "return (await store.listPending()).length;");
  expect(beforeReload).toBe(2);

  await page.reload();

  const afterReload = await runInQueue<string[]>(page, dbName, `
    return (await store.listPending()).map((item) => item.code);
  `);
  expect(afterReload).toEqual([UNIT_CODE, SECOND_UNIT_CODE]);
});

test("повторное добавление того же скана не создаёт второй записи", async ({ page }) => {
  const dbName = "taksklad-e2e-dedup";

  const count = await runInQueue<number>(page, dbName, `
    await store.enqueue(event({ code: "${UNIT_CODE}" }));
    await store.enqueue(event({ code: "${UNIT_CODE}", scannedAt: "2026-08-04T10:09:00+05:00" }));
    await store.enqueue(event({ code: "  ${UNIT_CODE}  " }));
    return (await store.listPending()).length;
  `);

  expect(count).toBe(1);
});

test("повторный скан не переставляет событие в конец и не сбрасывает попытки", async ({ page }) => {
  const dbName = "taksklad-e2e-requeue";

  const state = await runInQueue<{ codes: string[]; attempts: number; lastError: string }>(page, dbName, `
    const first = event({ code: "${UNIT_CODE}" });
    await store.enqueue(first);
    await store.enqueue(event({ orderItemId: "item-2", code: "${SECOND_UNIT_CODE}" }));
    await store.update(key(first), { attempts: 4, lastError: "Failed to fetch" });
    await store.enqueue(event({ code: "${UNIT_CODE}", scannedAt: "2026-08-04T11:30:00+05:00" }));
    const pending = await store.listPending();
    return {
      codes: pending.map((item) => item.code),
      attempts: pending[0].attempts,
      lastError: pending[0].lastError,
    };
  `);

  expect(state.codes).toEqual([UNIT_CODE, SECOND_UNIT_CODE]);
  expect(state.attempts).toBe(4);
  expect(state.lastError).toBe("Failed to fetch");
});

test("порядок событий сохраняется после перезагрузки", async ({ page }) => {
  const dbName = "taksklad-e2e-order";

  await runInQueue(page, dbName, `
    for (let index = 0; index < 12; index += 1) {
      await store.enqueue(event({
        orderItemId: "item-" + index,
        code: "010400639605394721" + String(index).padStart(4, "0"),
      }));
    }
  `);

  await page.reload();

  const items = await runInQueue<string[]>(page, dbName, `
    return (await store.listPending()).map((item) => item.orderItemId);
  `);

  expect(items).toEqual(Array.from({ length: 12 }, (_, index) => `item-${index}`));
});

test("счётчик попыток переживает перезагрузку и не портит остальные поля", async ({ page }) => {
  const dbName = "taksklad-e2e-attempts";

  await runInQueue(page, dbName, `
    const item = event({ code: "${UNIT_CODE}" });
    await store.enqueue(item);
    await store.update(key(item), { attempts: 2, lastError: "Failed to fetch" });
  `);

  await page.reload();

  const stored = await runInQueue<{ attempts: number; lastError: string; code: string; actor: string }>(page, dbName, `
    return (await store.listPending())[0];
  `);

  expect(stored.attempts).toBe(2);
  expect(stored.lastError).toBe("Failed to fetch");
  expect(stored.code).toBe(UNIT_CODE);
  expect(stored.actor).toBe("operator-1");
});

test("block переносит событие между хранилищами и переживает перезагрузку", async ({ page }) => {
  const dbName = "taksklad-e2e-block";

  await runInQueue(page, dbName, `
    const item = event({ code: "${UNIT_CODE}" });
    await store.enqueue(item);
    await store.enqueue(event({ orderItemId: "item-2", code: "${SECOND_UNIT_CODE}" }));
    await store.block(key(item), "order_closed", "Позиция закрыта на сервере");
  `);

  await page.reload();

  const state = await runInQueue<{
    pending: string[];
    blocked: { code: string; reasonCode: string; reasonMessage: string; blockedAt: string }[];
  }>(page, dbName, `
    return {
      pending: (await store.listPending()).map((item) => item.code),
      blocked: (await store.listBlocked()).map((item) => ({
        code: item.event.code,
        reasonCode: item.reasonCode,
        reasonMessage: item.reasonMessage,
        blockedAt: item.blockedAt,
      })),
    };
  `);

  expect(state.pending).toEqual([SECOND_UNIT_CODE]);
  expect(state.blocked).toEqual([{
    code: UNIT_CODE,
    reasonCode: "order_closed",
    reasonMessage: "Позиция закрыта на сервере",
    blockedAt: "2026-08-04T12:00:00+05:00",
  }]);
});

test("заблокированный скан снимается только явным действием", async ({ page }) => {
  const dbName = "taksklad-e2e-dismiss";

  const remaining = await runInQueue<number>(page, dbName, `
    const item = event({ code: "${UNIT_CODE}" });
    await store.enqueue(item);
    await store.block(key(item), "scan_product_mismatch", "Товар не совпадает");
    const beforeDismiss = (await store.listBlocked()).length;
    await store.dismissBlocked(key(item));
    return beforeDismiss * 10 + (await store.listBlocked()).length;
  `);

  expect(remaining).toBe(10);
});

test("две вкладки одного origin видят одну очередь", async ({ page, context }) => {
  const dbName = "taksklad-e2e-tabs";

  await runInQueue(page, dbName, `await store.enqueue(event({ code: "${UNIT_CODE}" }));`);

  const second = await context.newPage();
  await installSyntheticApi(second);
  await second.goto("/");

  const seenBySecondTab = await runInQueue<string[]>(second, dbName, `
    return (await store.listPending()).map((item) => item.code);
  `);
  expect(seenBySecondTab).toEqual([UNIT_CODE]);

  await runInQueue(second, dbName, `await store.enqueue(event({ orderItemId: "item-2", code: "${SECOND_UNIT_CODE}" }));`);
  await second.close();

  const seenByFirstTab = await runInQueue<string[]>(page, dbName, `
    return (await store.listPending()).map((item) => item.code);
  `);
  expect(seenByFirstTab).toEqual([UNIT_CODE, SECOND_UNIT_CODE]);
});

test("рабочая смена складывается в очередь и остаётся целой после перезагрузки", async ({ page }) => {
  const dbName = "taksklad-e2e-shift";
  const shiftSize = 350;

  const elapsedMs = await runInQueue<number>(page, dbName, `
    const started = performance.now();
    for (let index = 0; index < ${shiftSize}; index += 1) {
      await store.enqueue(event({
        orderItemId: "item-" + (index % 20),
        code: "010400639605394721" + String(index).padStart(4, "0"),
      }));
    }
    return performance.now() - started;
  `);

  await page.reload();

  const state = await runInQueue<{ pending: number; codes: number; first: string; last: string }>(page, dbName, `
    const pending = await store.listPending();
    return {
      pending: pending.length,
      codes: (await store.listPendingCodes()).size,
      first: pending[0].code,
      last: pending[pending.length - 1].code,
    };
  `);

  expect(state.pending).toBe(shiftSize);
  expect(state.codes).toBe(shiftSize);
  expect(state.first).toBe("0104006396053947210000");
  expect(state.last).toBe(`010400639605394721${String(shiftSize - 1).padStart(4, "0")}`);

  // Складская смена это сотни сканов, запись каждого не должна заметно тормозить сканер
  expect(elapsedMs).toBeLessThan(10_000);
});
