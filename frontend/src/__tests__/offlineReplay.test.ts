import { describe, expect, it } from "vitest";

import { ApiRequestError } from "../api/core";
import {
  DUPLICATE_SCAN_ACK_CODE,
  NON_RETRYABLE_SCAN_CODES,
  RECOVERABLE_BROWSER_SECURITY_CODES,
  classifyReplayFailure,
} from "../features/warehouse/offline/errorPolicy";
import { createMemoryQueueStore } from "../features/warehouse/offline/queueStore";
import { MAX_CONSECUTIVE_RETRY_FAILURES, replayQueue } from "../features/warehouse/offline/replay";
import { scanEvent } from "./fixtures";

describe("classifyReplayFailure", () => {
  it("дубликат скана считается успехом: backend уже знает этот КИЗ", () => {
    const error = new ApiRequestError(409, "Conflict", "already scanned", DUPLICATE_SCAN_ACK_CODE);
    expect(classifyReplayFailure(error)).toBe("synced");
  });

  it.each(NON_RETRYABLE_SCAN_CODES)("409 %s блокируется навсегда", (code) => {
    expect(classifyReplayFailure(new ApiRequestError(409, "Conflict", "", code))).toBe("blocked");
  });

  it("409 с неизвестным кодом блокируется, как на десктопе", () => {
    expect(classifyReplayFailure(new ApiRequestError(409, "Conflict", "", "something_new"))).toBe("blocked");
  });

  it("422 kiz_format_invalid блокируется, а не крутится вечно", () => {
    const error = new ApiRequestError(422, "Unprocessable Entity", "", "kiz_format_invalid");
    expect(classifyReplayFailure(error)).toBe("blocked");
  });

  it("404 на несуществующей позиции блокируется", () => {
    expect(classifyReplayFailure(new ApiRequestError(404, "Not Found", "Order item not found"))).toBe("blocked");
  });

  it("403 без кода блокируется: прав не прибавится от повтора", () => {
    expect(classifyReplayFailure(new ApiRequestError(403, "Forbidden", ""))).toBe("blocked");
  });

  it.each(RECOVERABLE_BROWSER_SECURITY_CODES)("403 %s повторяется: чинится обновлением сессии", (code) => {
    expect(classifyReplayFailure(new ApiRequestError(403, "Forbidden", "", code))).toBe("retry");
  });

  it("401 повторяется: нужна новая сессия, а не потеря скана", () => {
    expect(classifyReplayFailure(new ApiRequestError(401, "Unauthorized", ""))).toBe("retry");
  });

  it("408 и 429 повторяются", () => {
    expect(classifyReplayFailure(new ApiRequestError(408, "Request Timeout", ""))).toBe("retry");
    expect(classifyReplayFailure(new ApiRequestError(429, "Too Many Requests", ""))).toBe("retry");
  });

  it("5xx повторяется", () => {
    expect(classifyReplayFailure(new ApiRequestError(503, "Service Unavailable", ""))).toBe("retry");
    expect(classifyReplayFailure(new ApiRequestError(502, "Bad Gateway", ""))).toBe("retry");
  });

  it("сетевая ошибка повторяется", () => {
    expect(classifyReplayFailure(new TypeError("Failed to fetch"))).toBe("retry");
  });

  it("таймаут запроса повторяется", () => {
    expect(classifyReplayFailure(new Error("Запрос /api/v1/scans не ответил за 15 сек."))).toBe("retry");
  });
});

describe("replayQueue", () => {
  function okDeps(log: string[] = []) {
    return {
      sendScan: async () => { log.push("scan"); },
      sendComplete: async () => { log.push("complete"); },
    };
  }

  it("успешный повтор убирает событие из очереди", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent());
    const summary = await replayQueue(store, okDeps());
    expect(summary).toEqual({ synced: 1, blocked: 0, failed: 0, remaining: 0 });
    expect(await store.listPending()).toHaveLength(0);
  });

  it("пустая очередь не трогает сеть", async () => {
    const store = createMemoryQueueStore();
    let calls = 0;
    const summary = await replayQueue(store, {
      sendScan: async () => { calls += 1; },
      sendComplete: async () => { calls += 1; },
    });
    expect(calls).toBe(0);
    expect(summary).toEqual({ synced: 0, blocked: 0, failed: 0, remaining: 0 });
  });

  it("дубликат на сервере закрывает событие как успешное", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent());
    const summary = await replayQueue(store, {
      sendScan: async () => {
        throw new ApiRequestError(409, "Conflict", "already scanned", DUPLICATE_SCAN_ACK_CODE);
      },
      sendComplete: async () => {},
    });
    expect(summary.synced).toBe(1);
    expect(await store.listPending()).toHaveLength(0);
    expect(await store.listBlocked()).toHaveLength(0);
  });

  it("неповторяемый конфликт уходит в blocked и остаётся видимым", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent());
    const summary = await replayQueue(store, {
      sendScan: async () => {
        throw new ApiRequestError(409, "Conflict", "Позиция закрыта", "order_closed");
      },
      sendComplete: async () => {},
    });
    expect(summary.blocked).toBe(1);
    expect(await store.listPending()).toHaveLength(0);
    const blocked = await store.listBlocked();
    expect(blocked).toHaveLength(1);
    expect(blocked[0].reasonCode).toBe("order_closed");
    expect(blocked[0].event.code).toBe("0104006396053947217ABCDEF");
  });

  it("блокировка одного события не останавливает остальные", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent({ code: "0104006396053947217AAAAAA" }));
    await store.enqueue(scanEvent({ code: "0104006396053947217BBBBBB" }));
    const summary = await replayQueue(store, {
      sendScan: async (event) => {
        if (event.code === "0104006396053947217AAAAAA") {
          throw new ApiRequestError(409, "Conflict", "", "scan_product_mismatch");
        }
      },
      sendComplete: async () => {},
    });
    expect(summary).toEqual({ synced: 1, blocked: 1, failed: 0, remaining: 0 });
  });

  it("сетевая ошибка оставляет событие в очереди и считает попытку", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent());
    const summary = await replayQueue(store, {
      sendScan: async () => { throw new TypeError("Failed to fetch"); },
      sendComplete: async () => {},
    });
    expect(summary).toEqual({ synced: 0, blocked: 0, failed: 1, remaining: 1 });
    const pending = await store.listPending();
    expect(pending[0].attempts).toBe(1);
    expect(pending[0].lastError).toContain("Failed to fetch");
  });

  it("подряд идущие сетевые отказы останавливают проход, чтобы не молотить оффлайн", async () => {
    const store = createMemoryQueueStore();
    for (let index = 0; index < 10; index += 1) {
      await store.enqueue(scanEvent({ code: `010400639605394721${String(index).padStart(4, "0")}` }));
    }
    let calls = 0;
    await replayQueue(store, {
      sendScan: async () => { calls += 1; throw new TypeError("Failed to fetch"); },
      sendComplete: async () => {},
    });
    expect(calls).toBe(MAX_CONSECUTIVE_RETRY_FAILURES);
    expect(await store.listPending()).toHaveLength(10);
  });

  it("постоянно падающее событие не запирает остальные навсегда", async () => {
    const store = createMemoryQueueStore();
    const poison = scanEvent({ code: "0104006396053947217AAAAAA" });
    const healthy = scanEvent({ orderItemId: "item-2", code: "0104006396053947217BBBBBB" });
    await store.enqueue(poison);
    await store.enqueue(healthy);
    const summary = await replayQueue(store, {
      sendScan: async (event) => {
        if (event.code === "0104006396053947217AAAAAA") {
          throw new ApiRequestError(500, "Internal Server Error", "boom");
        }
      },
      sendComplete: async () => {},
    });
    expect(summary).toEqual({ synced: 1, blocked: 0, failed: 1, remaining: 1 });
    const pending = await store.listPending();
    expect(pending).toHaveLength(1);
    expect(pending[0].code).toBe("0104006396053947217AAAAAA");
  });

  it("успех между отказами сбрасывает счётчик подряд идущих отказов", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent({ orderItemId: "item-1", code: "0104006396053947217AAAAAA" }));
    await store.enqueue(scanEvent({ orderItemId: "item-2", code: "0104006396053947217BBBBBB" }));
    await store.enqueue(scanEvent({ orderItemId: "item-3", code: "0104006396053947217CCCCCC" }));
    await store.enqueue(scanEvent({ orderItemId: "item-4", code: "0104006396053947217DDDDDD" }));
    const seen: string[] = [];
    await replayQueue(store, {
      sendScan: async (event) => {
        seen.push(event.orderItemId);
        if (event.orderItemId !== "item-2") throw new ApiRequestError(500, "Internal Server Error", "boom");
      },
      sendComplete: async () => {},
    });
    expect(seen).toEqual(["item-1", "item-2", "item-3", "item-4"]);
  });

  it("завершение чужого заказа уходит, даже если застрял скан другого", async () => {
    const store = createMemoryQueueStore();
    const log: string[] = [];
    await store.enqueue(scanEvent({ orderId: "order-1", orderItemId: "item-1" }));
    await store.enqueue({ ...scanEvent(), orderId: "order-2", type: "order_complete", code: "" });
    await replayQueue(store, {
      sendScan: async () => { log.push("scan-1"); throw new ApiRequestError(500, "Internal Server Error", "boom"); },
      sendComplete: async (event) => { log.push(`complete-${event.orderId}`); },
    });
    expect(log).toEqual(["scan-1", "complete-order-2"]);
  });

  it("завершение заказа ждёт свой скан, даже когда проход продолжается", async () => {
    const store = createMemoryQueueStore();
    const log: string[] = [];
    await store.enqueue(scanEvent({ orderId: "order-1", orderItemId: "item-1" }));
    await store.enqueue({ ...scanEvent(), orderId: "order-1", type: "order_complete", code: "" });
    await store.enqueue(scanEvent({ orderId: "order-3", orderItemId: "item-3", code: "0104006396053947217CCCCCC" }));
    await replayQueue(store, {
      sendScan: async (event) => {
        log.push(`scan-${event.orderItemId}`);
        if (event.orderItemId === "item-1") throw new ApiRequestError(500, "Internal Server Error", "boom");
      },
      sendComplete: async (event) => { log.push(`complete-${event.orderId}`); },
    });
    expect(log).toEqual(["scan-item-1", "scan-item-3"]);
    expect((await store.listPending()).map((event) => event.type)).toEqual(["scan", "order_complete"]);
  });

  it("повторный проход после сетевого сбоя копит попытки, а не сбрасывает их", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent());
    const failing = { sendScan: async () => { throw new TypeError("Failed to fetch"); }, sendComplete: async () => {} };
    await replayQueue(store, failing);
    await replayQueue(store, failing);
    expect((await store.listPending())[0].attempts).toBe(2);
  });

  it("order_complete уходит после своих сканов", async () => {
    const store = createMemoryQueueStore();
    const log: string[] = [];
    await store.enqueue(scanEvent());
    await store.enqueue({ ...scanEvent(), type: "order_complete", code: "" });
    await replayQueue(store, okDeps(log));
    expect(log).toEqual(["scan", "complete"]);
  });

  it("завершение заказа не отправляется, если его скан застрял в очереди", async () => {
    const store = createMemoryQueueStore();
    const log: string[] = [];
    await store.enqueue(scanEvent());
    await store.enqueue({ ...scanEvent(), type: "order_complete", code: "" });
    await replayQueue(store, {
      sendScan: async () => { log.push("scan"); throw new TypeError("Failed to fetch"); },
      sendComplete: async () => { log.push("complete"); },
    });
    expect(log).toEqual(["scan"]);
    expect(await store.listPending()).toHaveLength(2);
  });
});
