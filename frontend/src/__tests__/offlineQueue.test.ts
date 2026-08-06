import { describe, expect, it } from "vitest";

import { BLOCKED_LIMIT, createMemoryQueueStore } from "../features/warehouse/offline/queueStore";
import { offlineEventKey } from "../features/warehouse/offline/queueTypes";
import { scanEvent } from "./fixtures";

describe("offlineEventKey", () => {
  it("схлопывает повторный скан того же кода в ту же позицию", () => {
    const first = offlineEventKey(scanEvent());
    const second = offlineEventKey(scanEvent({ scannedAt: "2026-08-04T10:05:00+05:00", attempts: 3 }));
    expect(second).toBe(first);
  });

  it("различает тот же код в другой позиции", () => {
    expect(offlineEventKey(scanEvent({ orderItemId: "item-2" }))).not.toBe(offlineEventKey(scanEvent()));
  });

  it("различает разные коды в одной позиции", () => {
    expect(offlineEventKey(scanEvent({ code: "0104006396053947217BBBBBB" }))).not.toBe(offlineEventKey(scanEvent()));
  });

  it("обрезает пробелы вокруг кода перед построением ключа", () => {
    const spaced = scanEvent({ code: "  0104006396053947217ABCDEF  " });
    expect(offlineEventKey(spaced)).toBe(offlineEventKey(scanEvent()));
  });

  it("не схлопывает коды разного регистра: КИЗ регистрозависим", () => {
    const lower = scanEvent({ code: "0104006396053947217abcdef" });
    expect(offlineEventKey(lower)).not.toBe(offlineEventKey(scanEvent()));
  });

  it("ключ завершения заказа не зависит от позиции и кода", () => {
    const complete = offlineEventKey({ ...scanEvent(), type: "order_complete", orderItemId: "item-9", code: "" });
    expect(complete).toBe("order_complete|order-1");
  });
});

describe("createMemoryQueueStore", () => {
  it("повторное добавление того же скана не создаёт второй записи", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent());
    await store.enqueue(scanEvent({ scannedAt: "2026-08-04T10:05:00+05:00" }));
    expect(await store.listPending()).toHaveLength(1);
  });

  it("сохраняет порядок добавления событий", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent({ code: "0104006396053947217AAAAAA" }));
    await store.enqueue(scanEvent({ code: "0104006396053947217BBBBBB" }));
    await store.enqueue({ ...scanEvent(), type: "order_complete", code: "" });
    expect((await store.listPending()).map((event) => event.type)).toEqual(["scan", "scan", "order_complete"]);
  });

  it("отдаёт нормализованные коды очереди для локальной дедупликации", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent({ code: "  0104006396053947217ABCDEF  " }));
    await store.enqueue({ ...scanEvent(), type: "order_complete", code: "" });
    expect(await store.listPendingCodes()).toEqual(new Set(["0104006396053947217ABCDEF"]));
  });

  it("update увеличивает счётчик попыток, не трогая остальные поля", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent());
    await store.update(offlineEventKey(scanEvent()), { attempts: 1, lastError: "Failed to fetch" });
    const [event] = await store.listPending();
    expect(event.attempts).toBe(1);
    expect(event.lastError).toBe("Failed to fetch");
    expect(event.code).toBe("0104006396053947217ABCDEF");
  });

  it("remove убирает событие из очереди", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent());
    await store.remove(offlineEventKey(scanEvent()));
    expect(await store.listPending()).toHaveLength(0);
  });

  it("block переносит событие из pending в blocked и сохраняет причину", async () => {
    const store = createMemoryQueueStore(() => "2026-08-04T11:00:00+05:00");
    await store.enqueue(scanEvent());
    await store.block(offlineEventKey(scanEvent()), "scan_product_mismatch", "Товар не совпадает");
    expect(await store.listPending()).toHaveLength(0);
    const blocked = await store.listBlocked();
    expect(blocked).toHaveLength(1);
    expect(blocked[0].reasonCode).toBe("scan_product_mismatch");
    expect(blocked[0].reasonMessage).toBe("Товар не совпадает");
    expect(blocked[0].blockedAt).toBe("2026-08-04T11:00:00+05:00");
    expect(blocked[0].event.code).toBe("0104006396053947217ABCDEF");
  });

  it("block по неизвестному ключу ничего не портит", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent());
    await store.block("scan|item-404|0104006396053947217ZZZZZZ", "order_closed", "Позиция закрыта");
    expect(await store.listPending()).toHaveLength(1);
    expect(await store.listBlocked()).toHaveLength(0);
  });

  it("dismissBlocked снимает запись только по явному ключу", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent());
    await store.block(offlineEventKey(scanEvent()), "order_closed", "Позиция закрыта");
    await store.dismissBlocked(offlineEventKey(scanEvent()));
    expect(await store.listBlocked()).toHaveLength(0);
  });

  it("blocked хранит не больше лимита и вытесняет старые записи", async () => {
    const store = createMemoryQueueStore();
    for (let index = 0; index < BLOCKED_LIMIT + 5; index += 1) {
      const event = scanEvent({ code: `010400639605394721${String(index).padStart(4, "0")}` });
      await store.enqueue(event);
      await store.block(offlineEventKey(event), "order_closed", "Позиция закрыта");
    }
    const blocked = await store.listBlocked();
    expect(blocked).toHaveLength(BLOCKED_LIMIT);
    expect(blocked[blocked.length - 1].event.code).toBe(`010400639605394721${String(BLOCKED_LIMIT + 4).padStart(4, "0")}`);
  });
});

describe("повторное добавление уже стоящего в очереди события", () => {
  it("не переставляет его в конец и не сбрасывает состояние попыток", async () => {
    const store = createMemoryQueueStore();
    const first = scanEvent({ code: "0104006396053947217AAAAAA" });
    await store.enqueue(first);
    await store.enqueue(scanEvent({ orderItemId: "item-2", code: "0104006396053947217BBBBBB" }));
    await store.update(offlineEventKey(first), { attempts: 4, lastError: "Failed to fetch" });

    await store.enqueue(scanEvent({ code: "0104006396053947217AAAAAA", scannedAt: "2026-08-04T11:30:00+05:00" }));

    const pending = await store.listPending();
    expect(pending.map((event) => event.code)).toEqual([
      "0104006396053947217AAAAAA",
      "0104006396053947217BBBBBB",
    ]);
    expect(pending[0].attempts).toBe(4);
    expect(pending[0].lastError).toBe("Failed to fetch");
  });
});
