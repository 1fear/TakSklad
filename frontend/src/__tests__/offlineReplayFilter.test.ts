import { describe, expect, it } from "vitest";

import { ApiRequestError } from "../api/core";
import { createMemoryQueueStore } from "../features/warehouse/offline/queueStore";
import { eventMatchesReplayFilter, replayQueue } from "../features/warehouse/offline/replay";
import type { OfflineEvent } from "../features/warehouse/offline/queueTypes";
import { scanEvent } from "./fixtures";

function completeEvent(orderId: string): OfflineEvent {
  return scanEvent({ type: "order_complete", orderId, orderItemId: "", code: "" });
}

function recorder(failOn: (event: OfflineEvent) => boolean = () => false) {
  const sent: string[] = [];
  return {
    sent,
    deps: {
      async sendScan(event: OfflineEvent) {
        if (failOn(event)) throw new ApiRequestError(503, "Service Unavailable", "");
        sent.push(`scan:${event.orderItemId}`);
      },
      async sendComplete(event: OfflineEvent) {
        sent.push(`complete:${event.orderId}`);
      },
    },
  };
}

async function queueOf(...events: OfflineEvent[]) {
  const store = createMemoryQueueStore();
  for (const event of events) await store.enqueue(event);
  return store;
}

describe("eventMatchesReplayFilter", () => {
  it("matches a scan by its item and a completion by its order, like the desktop", () => {
    const scan = scanEvent({ orderId: "o1", orderItemId: "i1" });
    const complete = completeEvent("o1");

    expect(eventMatchesReplayFilter(scan, { orderItemIds: new Set(["i1"]) })).toBe(true);
    expect(eventMatchesReplayFilter(scan, { orderIds: new Set(["o1"]) })).toBe(false);
    expect(eventMatchesReplayFilter(complete, { orderIds: new Set(["o1"]) })).toBe(true);
    expect(eventMatchesReplayFilter(complete, { orderItemIds: new Set(["i1"]) })).toBe(false);
  });

  it("matches everything without a filter and nothing for empty sets", () => {
    const scan = scanEvent();

    expect(eventMatchesReplayFilter(scan)).toBe(true);
    expect(eventMatchesReplayFilter(scan, {})).toBe(false);
    expect(eventMatchesReplayFilter(scan, { orderItemIds: new Set(), orderIds: new Set() })).toBe(false);
  });
});

describe("replayQueue with a filter", () => {
  it("sends only the chosen item's scans and leaves the rest queued", async () => {
    const store = await queueOf(
      scanEvent({ orderItemId: "i1", code: "A" }),
      scanEvent({ orderItemId: "i2", code: "B" }),
      completeEvent("order-1"),
    );
    const { sent, deps } = recorder();

    const summary = await replayQueue(store, deps, { orderItemIds: new Set(["i1"]) });

    expect(sent).toEqual(["scan:i1"]);
    expect(summary).toEqual({ synced: 1, blocked: 0, failed: 0, remaining: 2 });
  });

  it("sends only completions for orderIds alone, and no scans", async () => {
    const store = await queueOf(scanEvent({ orderId: "o1", orderItemId: "i1" }), completeEvent("o2"));
    const { sent, deps } = recorder();

    await replayQueue(store, deps, { orderIds: new Set(["o2"]) });

    expect(sent).toEqual(["complete:o2"]);
  });

  it("sends scans before the completion of the same order", async () => {
    const store = await queueOf(
      scanEvent({ orderId: "o1", orderItemId: "i1", code: "A" }),
      scanEvent({ orderId: "o1", orderItemId: "i2", code: "B" }),
      completeEvent("o1"),
    );
    const { sent, deps } = recorder();

    await replayQueue(store, deps, { orderItemIds: new Set(["i1", "i2"]), orderIds: new Set(["o1"]) });

    expect(sent).toEqual(["scan:i1", "scan:i2", "complete:o1"]);
  });

  it("holds a completion while a scan of its order is kept out of the pass", async () => {
    const store = await queueOf(
      scanEvent({ orderId: "o1", orderItemId: "i1", code: "A" }),
      scanEvent({ orderId: "o1", orderItemId: "i3", code: "C" }),
      completeEvent("o1"),
    );
    const { sent, deps } = recorder();

    const summary = await replayQueue(store, deps, { orderItemIds: new Set(["i1"]), orderIds: new Set(["o1"]) });

    expect(sent).toEqual(["scan:i1"]);
    expect(summary.remaining).toBe(2);
  });

  it("holds a completion while a scan of its order fails in the pass", async () => {
    const store = await queueOf(scanEvent({ orderId: "o1", orderItemId: "i1" }), completeEvent("o1"));
    const { sent, deps } = recorder((event) => event.orderItemId === "i1");

    await replayQueue(store, deps, { orderItemIds: new Set(["i1"]), orderIds: new Set(["o1"]) });

    expect(sent).toEqual([]);
  });
});
