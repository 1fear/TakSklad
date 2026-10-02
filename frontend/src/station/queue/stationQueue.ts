/**
 * The station's scan queue: the desktop's backend event queue, in the page.
 *
 * A scan is written to the durable queue first and sent later, by a cycle (every 15 s) or on demand
 * (next position, finish order, manual refresh). Nothing is called "saved" before the server took it:
 * `isDelivered` is the only source for that label.
 */

import { completeWarehouseOrder, createScan, type ApiConfig, type Order } from "../../api";
import { ApiRequestError } from "../../api/core";
import { classifyReplayFailure } from "../../features/warehouse/offline/errorPolicy";
import { normalizeKizCode } from "../../features/warehouse/kizFormat";
import {
  createIndexedDbQueueStore,
  type BlockedEvent,
  type OfflineQueueStore,
} from "../../features/warehouse/offline/queueStore";
import { offlineEventKey, type OfflineEvent } from "../../features/warehouse/offline/queueTypes";
import {
  eventMatchesReplayFilter,
  replayQueue,
  type ReplayDeps,
  type ReplayFilter,
} from "../../features/warehouse/offline/replay";

/** First cycle after start (`src/taksklad/main.py:189`) and the gap between cycles (`src/taksklad/app_data_loading.py:27`). */
export const STATION_SYNC_FIRST_RUN_MS = 13_000;
export const STATION_SYNC_INTERVAL_MS = 15_000;

/** A 401 asks for a new station sign-in no more often than this (spec section 4). */
export const RELOGIN_MIN_INTERVAL_MS = 30_000;

export type FlushResult = {
  /** Codes the server holds after this pass, including ones it already had (409 duplicate ack). */
  delivered: string[];
  /** Events the server refused for good during this pass, now in the blocked section. */
  blocked: BlockedEvent[];
  /** Events of this pass's scope still waiting in the queue. */
  pending: number;
  /** The server could not be reached or could not answer (network, 5xx, 401 after a re-login). */
  networkFailed: boolean;
};

export type CycleOutcome = { ok: true; result: FlushResult } | { ok: false; error: unknown };

export type StationQueueOptions = {
  /** Read on every send: the CSRF token changes after a re-login. */
  getConfig: () => ApiConfig;
  actor: string;
  workstationId: string;
  /** Signs the station in again; called on a 401, not more often than once per 30 s. */
  relogin: () => Promise<void>;
  onCycle?: (outcome: CycleOutcome) => void;
  store?: OfflineQueueStore;
  /** Defaults to the real API; tests pass their own. */
  send?: ReplayDeps;
  now?: () => number;
};

export type StationQueue = ReturnType<typeof createStationQueue>;

export function createStationQueue(options: StationQueueOptions) {
  const { getConfig, actor, workstationId, relogin, onCycle } = options;
  const now = options.now ?? (() => Date.now());
  const store = options.store ?? createIndexedDbQueueStore();
  const send: ReplayDeps = options.send ?? {
    sendScan: async (event) => {
      await createScan(getConfig(), {
        order_item_id: event.orderItemId,
        code: event.code,
        workstation_id: event.workstationId,
        scanned_by: event.actor,
        scanned_at: event.scannedAt,
      });
    },
    sendComplete: async (event) => {
      await completeWarehouseOrder(getConfig(), event.orderId);
    },
  };

  // Delivered: codes the last API load listed on the item, plus codes a pass synced since that load.
  let loadedCodes = new Map<string, Set<string>>();
  let syncedCodes = new Map<string, Set<string>>();

  function remember(into: Map<string, Set<string>>, orderItemId: string, code: string) {
    const codes = into.get(orderItemId) ?? new Set<string>();
    codes.add(normalizeKizCode(code));
    into.set(orderItemId, codes);
  }

  function forget(orderItemId: string, code: string) {
    loadedCodes.get(orderItemId)?.delete(normalizeKizCode(code));
    syncedCodes.get(orderItemId)?.delete(normalizeKizCode(code));
  }

  // 401 -> sign in again -> retry the same request once; a second 401 is left to the queue's own retry.
  let lastReloginAt = Number.NEGATIVE_INFINITY;
  async function withRelogin<T>(call: () => Promise<T>): Promise<T> {
    try {
      return await call();
    } catch (error) {
      if (!(error instanceof ApiRequestError) || error.status !== 401) throw error;
      const at = now();
      if (at - lastReloginAt < RELOGIN_MIN_INTERVAL_MS) throw error;
      lastReloginAt = at;
      try {
        await relogin();
      } catch {
        throw error;
      }
      return call();
    }
  }

  // One pass at a time: a screen action waits for the running pass, the cycle skips instead of waiting.
  let tail: Promise<void> = Promise.resolve();
  let passesInFlight = 0;
  function exclusive<T>(pass: () => Promise<T>): Promise<T> {
    passesInFlight += 1;
    const run = tail.then(pass);
    tail = run.then(() => undefined, () => undefined);
    return run.finally(() => { passesInFlight -= 1; });
  }

  async function runPass(filter?: ReplayFilter): Promise<FlushResult> {
    const delivered: string[] = [];
    const blockedKeys = new Set<string>();

    const deps: ReplayDeps = {
      sendScan: async (event) => {
        try {
          await withRelogin(() => send.sendScan(event));
        } catch (error) {
          const verdict = classifyReplayFailure(error);
          if (verdict === "blocked") blockedKeys.add(offlineEventKey(event));
          if (verdict !== "synced") throw error;
          // 409 duplicate ack: the server already holds the code, which is delivery all the same.
        }
        remember(syncedCodes, event.orderItemId, event.code);
        delivered.push(event.code);
      },
      sendComplete: async (event) => {
        try {
          await withRelogin(() => send.sendComplete(event));
        } catch (error) {
          if (classifyReplayFailure(error) === "blocked") blockedKeys.add(offlineEventKey(event));
          throw error;
        }
      },
    };

    const summary = await replayQueue(store, deps, filter);
    const [stillQueued, allBlocked] = await Promise.all([store.listPending(), store.listBlocked()]);
    const blockedNow = new Map(allBlocked.filter((item) => blockedKeys.has(item.key)).map((item) => [item.key, item]));
    return {
      delivered,
      blocked: [...blockedNow.values()],
      pending: stillQueued.filter((event) => eventMatchesReplayFilter(event, filter)).length,
      networkFailed: summary.failed > 0,
    };
  }

  function flush(filter?: ReplayFilter): Promise<FlushResult> {
    return exclusive(() => runPass(filter));
  }

  // The cycle is a chain, not an interval: the next run is planned when the previous one ends.
  let timer: ReturnType<typeof setTimeout> | undefined;
  let started = false;
  function plan(delayMs: number) {
    clearTimeout(timer);
    timer = started ? setTimeout(() => void cycle(), delayMs) : undefined;
  }
  async function cycle() {
    try {
      if (passesInFlight > 0) return;
      const outcome: CycleOutcome = await flush().then(
        (result) => ({ ok: true, result }),
        (error: unknown) => ({ ok: false, error }),
      );
      onCycle?.(outcome);
    } finally {
      plan(STATION_SYNC_INTERVAL_MS);
    }
  }

  function newEvent(type: OfflineEvent["type"], orderId: string, orderItemId: string, code: string): OfflineEvent {
    const at = new Date(now()).toISOString();
    return {
      type,
      orderId,
      orderItemId,
      code,
      actor,
      workstationId,
      scannedAt: at,
      createdAt: at,
      attempts: 0,
      lastError: "",
    };
  }

  return {
    start() {
      started = true;
      plan(STATION_SYNC_FIRST_RUN_MS);
    },
    stop() {
      started = false;
      clearTimeout(timer);
      timer = undefined;
    },

    /** Durable write only: nothing is sent, nothing is delivered. */
    async enqueueScan(input: { orderId: string; orderItemId: string; code: string }) {
      // A code scanned again has to wait for the server again, whatever it was before.
      forget(input.orderItemId, input.code);
      await store.enqueue(newEvent("scan", input.orderId, input.orderItemId, input.code));
    },
    async enqueueComplete(orderId: string) {
      await store.enqueue(newEvent("order_complete", orderId, "", ""));
    },

    /** Whole queue, on demand (manual refresh); the cycle runs the same pass. */
    flushAll: () => flush(),
    /** Next position: only the scans of this item (`src/taksklad/app_scanning.py:650`). */
    flushItem: (orderItemId: string) => flush({ orderItemIds: new Set([orderItemId]) }),
    /** Finish: the scans of the order's items first, then its order_complete (`src/taksklad/app_finish.py:150`). */
    flushOrder: (orderId: string, orderItemIds: string[]) =>
      flush({ orderItemIds: new Set(orderItemIds), orderIds: new Set([orderId]) }),

    /**
     * Call with every full API load of the order list. The loaded codes become the delivered
     * baseline. A pass that finishes while the load is in flight is forgotten here, which can only
     * show a delivered code as queued, never the reverse.
     */
    recordLoadedOrders(orders: Order[]) {
      loadedCodes = new Map();
      syncedCodes = new Map();
      for (const order of orders) {
        for (const item of order.items) {
          for (const code of item.scan_codes) remember(loadedCodes, item.id, code);
        }
      }
    },
    isDelivered(orderItemId: string, code: string): boolean {
      const normalized = normalizeKizCode(code);
      return Boolean(loadedCodes.get(orderItemId)?.has(normalized) || syncedCodes.get(orderItemId)?.has(normalized));
    },
  };
}
