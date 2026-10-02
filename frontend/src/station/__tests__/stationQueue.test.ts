import { http as mswHttp, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { activeOrder, orderItem } from "../../__tests__/fixtures";
import type { Order } from "../../api";
import { ApiRequestError } from "../../api/core";
import { createMemoryQueueStore } from "../../features/warehouse/offline/queueStore";
import type { OfflineEvent } from "../../features/warehouse/offline/queueTypes";
import { server } from "../../test/server";
import {
  RELOGIN_MIN_INTERVAL_MS,
  STATION_SYNC_FIRST_RUN_MS,
  STATION_SYNC_INTERVAL_MS,
  createStationQueue,
  type CycleOutcome,
  type StationQueueOptions,
} from "../queue/stationQueue";

const CODE_A = "0104006396053947217AAAAAAAAAA";
const CODE_B = "0104006396053947217BBBBBBBBBB";
const CODE_C = "0104006396053947217CCCCCCCCCC";

function http(status: number, code = "") {
  return new ApiRequestError(status, "", "", code);
}

/** A transport that records what reached the "server" and fails on demand. */
function fakeServer(failWith: (event: OfflineEvent, attempt: number) => unknown = () => null) {
  const sent: string[] = [];
  const attempts = new Map<string, number>();
  async function handle(event: OfflineEvent, label: string) {
    const key = `${label}:${event.orderItemId || event.orderId}:${event.code}`;
    const attempt = (attempts.get(key) ?? 0) + 1;
    attempts.set(key, attempt);
    const failure = failWith(event, attempt);
    if (failure) throw failure;
    sent.push(label === "scan" ? `scan:${event.orderItemId}:${event.code}` : `complete:${event.orderId}`);
  }
  return {
    sent,
    send: {
      sendScan: (event: OfflineEvent) => handle(event, "scan"),
      sendComplete: (event: OfflineEvent) => handle(event, "complete"),
    },
  };
}

function build(overrides: Partial<StationQueueOptions> = {}) {
  const store = createMemoryQueueStore();
  const server = fakeServer();
  const relogin = vi.fn(async () => undefined);
  const queue = createStationQueue({
    getConfig: () => ({ apiUrl: "", token: "", csrfToken: "csrf" }),
    actor: "station",
    workstationId: "web-station",
    relogin,
    store,
    send: server.send,
    ...overrides,
  });
  return { queue, store, server, relogin };
}

function orderWithCodes(itemId: string, codes: string[]): Order {
  return { ...activeOrder, items: [orderItem({ id: itemId, scan_codes: codes })] };
}

/** The memory store and the pass are chains of promises; give them time to finish after a timer fires. */
async function settle() {
  for (let turn = 0; turn < 50; turn += 1) await Promise.resolve();
}

describe("cadence", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("writes a scan to the queue without sending it", async () => {
    const { queue, store, server } = build();

    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    expect(server.sent).toEqual([]);
    expect(await store.listPending()).toHaveLength(1);
  });

  it("runs the first cycle 13 s after start and then every 15 s", async () => {
    const { queue, server } = build();
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    queue.start();
    await vi.advanceTimersByTimeAsync(STATION_SYNC_FIRST_RUN_MS - 1);
    expect(server.sent).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    await settle();
    expect(server.sent).toEqual([`scan:item-1:${CODE_A}`]);

    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_B });
    await vi.advanceTimersByTimeAsync(STATION_SYNC_INTERVAL_MS - 1);
    expect(server.sent).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    await settle();
    expect(server.sent).toEqual([`scan:item-1:${CODE_A}`, `scan:item-1:${CODE_B}`]);

    expect([STATION_SYNC_FIRST_RUN_MS, STATION_SYNC_INTERVAL_MS]).toEqual([13_000, 15_000]);
  });

  it("stops cycling after stop()", async () => {
    const { queue, server } = build();
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    queue.start();
    queue.stop();
    await vi.advanceTimersByTimeAsync(60_000);

    expect(server.sent).toEqual([]);
  });

  it("plans the next cycle when the previous one ends, and skips a tick while a screen pass runs", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const outcomes: CycleOutcome[] = [];
    const server = fakeServer();
    const { queue } = build({
      send: {
        sendScan: async (event) => { await gate; await server.send.sendScan(event); },
        sendComplete: server.send.sendComplete,
      },
      onCycle: (outcome) => outcomes.push(outcome),
    });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    queue.start();
    const screenPass = queue.flushItem("item-1");
    await vi.advanceTimersByTimeAsync(STATION_SYNC_FIRST_RUN_MS);
    expect(outcomes).toHaveLength(0);

    release();
    await screenPass;
    await vi.advanceTimersByTimeAsync(STATION_SYNC_INTERVAL_MS);
    expect(outcomes).toHaveLength(1);
    expect(server.sent).toEqual([`scan:item-1:${CODE_A}`]);
  });

  it("reports each cycle, including a storage failure, and keeps cycling", async () => {
    const outcomes: CycleOutcome[] = [];
    const { queue, store } = build({ onCycle: (outcome) => outcomes.push(outcome) });
    vi.spyOn(store, "listPending").mockRejectedValueOnce(new Error("storage gone"));

    queue.start();
    await vi.advanceTimersByTimeAsync(STATION_SYNC_FIRST_RUN_MS);
    await vi.advanceTimersByTimeAsync(STATION_SYNC_INTERVAL_MS);

    expect(outcomes.map((outcome) => outcome.ok)).toEqual([false, true]);
  });
});

describe("flushItem", () => {
  it("sends only that item's scans and reports what happened", async () => {
    const { queue, store, server } = build();
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-2", code: CODE_B });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_C });
    await queue.enqueueComplete("order-1");

    const result = await queue.flushItem("item-1");

    expect(result).toEqual({ delivered: [CODE_A, CODE_C], blocked: [], pending: 0, networkFailed: false });
    expect(server.sent).toEqual([`scan:item-1:${CODE_A}`, `scan:item-1:${CODE_C}`]);
    expect((await store.listPending()).map((event) => event.type)).toEqual(["scan", "order_complete"]);
  });

  it("counts what is still queued for the item and flags a failed network", async () => {
    const server = fakeServer((event) => (event.code === CODE_B ? http(503) : null));
    const { queue } = build({ send: server.send });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_B });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-2", code: CODE_C });

    const result = await queue.flushItem("item-1");

    expect(result).toEqual({ delivered: [CODE_A], blocked: [], pending: 1, networkFailed: true });
  });

  it("hands back events the server refused for good", async () => {
    const server = fakeServer(() => http(409, "kiz_already_owned"));
    const { queue, store } = build({ send: server.send });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    const result = await queue.flushItem("item-1");

    expect(result.delivered).toEqual([]);
    expect(result.pending).toBe(0);
    expect(result.networkFailed).toBe(false);
    expect(result.blocked).toHaveLength(1);
    expect(result.blocked[0]).toMatchObject({ reasonCode: "kiz_already_owned", event: { code: CODE_A } });
    expect(await store.listBlocked()).toHaveLength(1);
  });

  it("does not report an earlier blocked event as this pass's", async () => {
    let refuse = true;
    const server = fakeServer((event) => (refuse && event.code === CODE_A ? http(409, "kiz_already_owned") : null));
    const { queue } = build({ send: server.send });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    await queue.flushItem("item-1");
    refuse = false;
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_B });

    const result = await queue.flushItem("item-1");

    expect(result).toEqual({ delivered: [CODE_B], blocked: [], pending: 0, networkFailed: false });
  });
});

describe("flushOrder", () => {
  it("sends the order's scans first and then its completion, leaving other orders alone", async () => {
    const { queue, server } = build();
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    await queue.enqueueScan({ orderId: "order-2", orderItemId: "item-9", code: CODE_C });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-2", code: CODE_B });
    await queue.enqueueComplete("order-1");

    const result = await queue.flushOrder("order-1", ["item-1", "item-2"]);

    expect(server.sent).toEqual([`scan:item-1:${CODE_A}`, `scan:item-2:${CODE_B}`, "complete:order-1"]);
    expect(result).toEqual({ delivered: [CODE_A, CODE_B], blocked: [], pending: 0, networkFailed: false });
  });

  it("does not complete the order while one of its scans fails", async () => {
    const server = fakeServer((event) => (event.code === CODE_B ? http(503) : null));
    const { queue } = build({ send: server.send });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-2", code: CODE_B });
    await queue.enqueueComplete("order-1");

    const result = await queue.flushOrder("order-1", ["item-1", "item-2"]);

    expect(server.sent).toEqual([`scan:item-1:${CODE_A}`]);
    expect(result).toEqual({ delivered: [CODE_A], blocked: [], pending: 2, networkFailed: true });
  });

  it("does not complete the order while a scan of an item outside the list still waits", async () => {
    const { queue, server } = build();
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-3", code: CODE_C });
    await queue.enqueueComplete("order-1");

    const result = await queue.flushOrder("order-1", ["item-1"]);

    expect(server.sent).toEqual([`scan:item-1:${CODE_A}`]);
    expect(result.pending).toBe(1);
  });
});

describe("delivered state", () => {
  it("is true for codes the last API load listed on the item", () => {
    const { queue } = build();

    queue.recordLoadedOrders([orderWithCodes("item-1", [CODE_A])]);

    expect(queue.isDelivered("item-1", CODE_A)).toBe(true);
    expect(queue.isDelivered("item-1", ` ${CODE_A}\n`)).toBe(true);
    expect(queue.isDelivered("item-1", CODE_B)).toBe(false);
    expect(queue.isDelivered("item-2", CODE_A)).toBe(false);
  });

  it("is never true for a scan that is only queued, and turns true once a pass synced it", async () => {
    const { queue } = build();
    queue.recordLoadedOrders([orderWithCodes("item-1", [])]);

    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_B });
    expect(queue.isDelivered("item-1", CODE_B)).toBe(false);

    await queue.flushItem("item-1");
    expect(queue.isDelivered("item-1", CODE_B)).toBe(true);
  });

  it("stays false when the server did not take the scan", async () => {
    const network = fakeServer(() => http(503));
    const refusal = fakeServer(() => http(409, "kiz_already_owned"));
    const failing = build({ send: network.send });
    const refused = build({ send: refusal.send });

    for (const { queue } of [failing, refused]) {
      await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_B });
      await queue.flushItem("item-1");
      expect(queue.isDelivered("item-1", CODE_B)).toBe(false);
    }
  });

  it("counts a 409 duplicate acknowledgement as delivered: the server already holds the code", async () => {
    const server = fakeServer(() => http(409, "scan_duplicate_ack"));
    const { queue } = build({ send: server.send });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_B });

    const result = await queue.flushItem("item-1");

    expect(result.delivered).toEqual([CODE_B]);
    expect(queue.isDelivered("item-1", CODE_B)).toBe(true);
  });

  it("is marked by the cycle and the whole-queue pass too", async () => {
    const { queue } = build();
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-2", code: CODE_B });

    await queue.flushAll();

    expect(queue.isDelivered("item-1", CODE_A)).toBe(true);
    expect(queue.isDelivered("item-2", CODE_B)).toBe(true);
  });

  it("starts over with every API load", async () => {
    const { queue } = build();
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_B });
    await queue.flushItem("item-1");

    queue.recordLoadedOrders([orderWithCodes("item-1", [CODE_A])]);

    expect(queue.isDelivered("item-1", CODE_B)).toBe(false);
    expect(queue.isDelivered("item-1", CODE_A)).toBe(true);
  });

  it("withdraws the mark when a code is scanned again, until the server takes it again", async () => {
    const { queue } = build();
    queue.recordLoadedOrders([orderWithCodes("item-1", [CODE_A])]);

    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    expect(queue.isDelivered("item-1", CODE_A)).toBe(false);

    await queue.flushItem("item-1");
    expect(queue.isDelivered("item-1", CODE_A)).toBe(true);
  });
});

describe("re-login on 401", () => {
  function unauthorizedUntilRelogin() {
    let signedIn = false;
    const server = fakeServer(() => (signedIn ? null : http(401)));
    return { server, signIn: () => { signedIn = true; } };
  }

  it("signs in again and retries the same request once", async () => {
    const { server, signIn } = unauthorizedUntilRelogin();
    const relogin = vi.fn(async () => { signIn(); });
    const { queue } = build({ send: server.send, relogin });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    const result = await queue.flushItem("item-1");

    expect(relogin).toHaveBeenCalledTimes(1);
    expect(result).toEqual({ delivered: [CODE_A], blocked: [], pending: 0, networkFailed: false });
  });

  it("retries only once: a second 401 stays a retryable failure", async () => {
    const server = fakeServer(() => http(401));
    const relogin = vi.fn(async () => undefined);
    const { queue, store } = build({ send: server.send, relogin });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    const result = await queue.flushItem("item-1");

    expect(relogin).toHaveBeenCalledTimes(1);
    expect(result).toEqual({ delivered: [], blocked: [], pending: 1, networkFailed: true });
    expect(await store.listBlocked()).toEqual([]);
  });

  it("asks no more than once per 30 s however many requests answer 401", async () => {
    let clock = 1_000_000;
    const server = fakeServer(() => http(401));
    const relogin = vi.fn(async () => undefined);
    const { queue } = build({ send: server.send, relogin, now: () => clock });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-2", code: CODE_B });

    await queue.flushAll();
    expect(relogin).toHaveBeenCalledTimes(1);

    clock += RELOGIN_MIN_INTERVAL_MS - 1;
    await queue.flushAll();
    expect(relogin).toHaveBeenCalledTimes(1);

    clock += 1;
    await queue.flushAll();
    expect(relogin).toHaveBeenCalledTimes(2);
  });

  it("keeps the scan queued when the re-login itself fails", async () => {
    const server = fakeServer(() => http(401));
    const relogin = vi.fn(async () => { throw http(403, "station_network_denied"); });
    const { queue } = build({ send: server.send, relogin });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    const result = await queue.flushItem("item-1");

    expect(result).toEqual({ delivered: [], blocked: [], pending: 1, networkFailed: true });
  });

  it("does not re-login for other failures", async () => {
    const server = fakeServer(() => http(503));
    const relogin = vi.fn(async () => undefined);
    const { queue } = build({ send: server.send, relogin });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    await queue.flushItem("item-1");

    expect(relogin).not.toHaveBeenCalled();
  });

  it("also covers the order completion", async () => {
    const { server, signIn } = unauthorizedUntilRelogin();
    const relogin = vi.fn(async () => { signIn(); });
    const { queue } = build({ send: server.send, relogin });
    await queue.enqueueComplete("order-1");

    const result = await queue.flushOrder("order-1", []);

    expect(server.sent).toEqual(["complete:order-1"]);
    expect(result.pending).toBe(0);
  });
});

describe("re-login on csrf_invalid", () => {
  // The session cookie was replaced under the open window (another window, an /admin sign-in): auth passes, CSRF does not.
  function staleCsrfUntilRelogin() {
    let fresh = false;
    const server = fakeServer(() => (fresh ? null : http(403, "csrf_invalid")));
    return { server, refresh: () => { fresh = true; } };
  }

  it("asks for a fresh session and then delivers the code", async () => {
    const { server, refresh } = staleCsrfUntilRelogin();
    const relogin = vi.fn(async () => { refresh(); });
    const { queue } = build({ send: server.send, relogin });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    const result = await queue.flushItem("item-1");

    expect(relogin).toHaveBeenCalledTimes(1);
    expect(result).toEqual({ delivered: [CODE_A], blocked: [], pending: 0, networkFailed: false });
    expect(queue.isDelivered("item-1", CODE_A)).toBe(true);
  });

  it("also covers the order completion", async () => {
    const { server, refresh } = staleCsrfUntilRelogin();
    const relogin = vi.fn(async () => { refresh(); });
    const { queue } = build({ send: server.send, relogin });
    await queue.enqueueComplete("order-1");

    const result = await queue.flushOrder("order-1", []);

    expect(relogin).toHaveBeenCalledTimes(1);
    expect(server.sent).toEqual(["complete:order-1"]);
    expect(result.pending).toBe(0);
  });

  it("shares the 30 s limit with the 401: a second csrf_invalid does not sign in again and the code stays queued", async () => {
    let clock = 1_000_000;
    const server = fakeServer(() => http(403, "csrf_invalid"));
    const relogin = vi.fn(async () => undefined);
    const { queue, store } = build({ send: server.send, relogin, now: () => clock });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    const first = await queue.flushAll();
    expect(relogin).toHaveBeenCalledTimes(1);
    expect(first).toEqual({ delivered: [], blocked: [], pending: 1, networkFailed: true });

    clock += RELOGIN_MIN_INTERVAL_MS - 1;
    const second = await queue.flushAll();
    expect(relogin).toHaveBeenCalledTimes(1);
    expect(second).toEqual({ delivered: [], blocked: [], pending: 1, networkFailed: true });
    expect(await store.listBlocked()).toEqual([]);

    clock += 1;
    await queue.flushAll();
    expect(relogin).toHaveBeenCalledTimes(2);
  });

  it("counts a 401 and a csrf_invalid against the same 30 s", async () => {
    let clock = 1_000_000;
    // Pass 1: 401, re-login, 401 again. Pass 2, 10 s later: csrf_invalid.
    const answers = [http(401), http(401), http(403, "csrf_invalid"), http(403, "csrf_invalid")];
    const server = fakeServer(() => answers.shift() ?? null);
    const relogin = vi.fn(async () => undefined);
    const { queue } = build({ send: server.send, relogin, now: () => clock });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    await queue.flushAll();
    expect(relogin).toHaveBeenCalledTimes(1);

    clock += 10_000;
    const result = await queue.flushAll();

    expect(relogin).toHaveBeenCalledTimes(1);
    expect(result).toEqual({ delivered: [], blocked: [], pending: 1, networkFailed: true });
  });

  it("does not re-login for a 403 with another code", async () => {
    const server = fakeServer(() => http(403, "origin_denied"));
    const relogin = vi.fn(async () => undefined);
    const { queue } = build({ send: server.send, relogin });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });

    await queue.flushItem("item-1");

    expect(relogin).not.toHaveBeenCalled();
  });
});

describe("one pass at a time", () => {
  it("holds a second pass until the first one has returned", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const calls: string[] = [];
    const { queue } = build({
      send: {
        sendScan: async (event) => {
          calls.push(`start:${event.code}`);
          if (event.code === CODE_A) await gate;
          calls.push(`end:${event.code}`);
        },
        sendComplete: async () => undefined,
      },
    });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-2", code: CODE_B });

    const first = queue.flushItem("item-1");
    await settle();
    const second = queue.flushItem("item-2");
    await settle();
    expect(calls).toEqual([`start:${CODE_A}`]);

    release();
    await first;
    await second;

    expect(calls).toEqual([`start:${CODE_A}`, `end:${CODE_A}`, `start:${CODE_B}`, `end:${CODE_B}`]);
  });
});

describe("default sender", () => {
  const SCANNED_AT = "2026-10-02T08:15:00.000Z";

  function queueWithDefaultSender() {
    const store = createMemoryQueueStore();
    const queue = createStationQueue({
      getConfig: () => ({ apiUrl: "", token: "", csrfToken: "csrf" }),
      actor: "station",
      workstationId: "web-station",
      relogin: async () => undefined,
      store,
      now: () => Date.parse(SCANNED_AT),
    });
    return { queue, store };
  }

  it("posts a scan with the time it was scanned, like the program", async () => {
    let body: unknown;
    let csrf: string | null = null;
    server.use(mswHttp.post("/api/v1/scans", async ({ request }) => {
      body = await request.json();
      csrf = request.headers.get("X-TakSklad-CSRF");
      return HttpResponse.json({});
    }));
    const { queue, store } = queueWithDefaultSender();
    await queue.enqueueScan({ orderId: "order-1", orderItemId: "item-1", code: CODE_A });
    const [queued] = await store.listPending();
    expect(queued.scannedAt).toBe(SCANNED_AT);

    const result = await queue.flushItem("item-1");

    expect(result).toEqual({ delivered: [CODE_A], blocked: [], pending: 0, networkFailed: false });
    expect(csrf).toBe("csrf");
    expect(body).toMatchObject({
      order_item_id: "item-1",
      code: CODE_A,
      workstation_id: "web-station",
      scanned_by: "station",
      scanned_at: queued.scannedAt,
    });
  });

  it("posts the order completion to the order's complete endpoint", async () => {
    const completed: Array<string | null> = [];
    server.use(mswHttp.post("/api/v1/orders/order-1/complete", ({ request }) => {
      completed.push(request.headers.get("X-TakSklad-CSRF"));
      return HttpResponse.json(activeOrder);
    }));
    const { queue } = queueWithDefaultSender();
    await queue.enqueueComplete("order-1");

    const result = await queue.flushOrder("order-1", []);

    expect(completed).toEqual(["csrf"]);
    expect(result).toEqual({ delivered: [], blocked: [], pending: 0, networkFailed: false });
  });
});
