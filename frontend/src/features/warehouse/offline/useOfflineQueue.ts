/**
 * Offline scan queue wired into the operator surface.
 *
 * Replay runs in the page, never in a service worker: writing a scan needs the
 * same-origin session cookie and the CSRF header, and both live in the tab. A
 * background replay would collect 401 answers with nobody watching.
 *
 * The queue is refilled from durable storage on every mutation instead of being
 * mirrored in React state, so what the operator sees is what survives a reload.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { completeWarehouseOrder, createScan, type ApiConfig } from "../../../api";
import { classifyReplayFailure } from "./errorPolicy";
import {
  createIndexedDbQueueStore,
  type BlockedEvent,
  type OfflineQueueStore,
} from "./queueStore";
import { replayQueue, type ReplaySummary } from "./replay";
import { offlineEventKey, type OfflineEvent } from "./queueTypes";

/** How often a non-empty queue retries by itself while the tab stays open. */
export const AUTO_REPLAY_INTERVAL_MS = 30_000;

const REPLAY_LOCK_NAME = "taksklad-offline-replay";

export type EnqueueScanInput = {
  orderId: string;
  orderItemId: string;
  code: string;
  actor: string;
  workstationId: string;
};

export type OfflineQueue = {
  pending: OfflineEvent[];
  blocked: BlockedEvent[];
  pendingCodes: Set<string>;
  storageReady: boolean;
  storageError: string;
  replaying: boolean;
  enqueueScan(input: EnqueueScanInput): Promise<void>;
  replayNow(): Promise<ReplaySummary | null>;
  dismissBlocked(key: string): Promise<void>;
  pendingScansForOrder(orderId: string): number;
  /** Newest queued scan of an item, the one an undo must take back first. */
  lastPendingScanForItem(orderItemId: string): OfflineEvent | null;
  removePendingScan(event: OfflineEvent): Promise<void>;
  /** True when the live attempt failed for a reason a later replay can fix. */
  shouldQueue(error: unknown): boolean;
};

/**
 * Runs `task` under a cross-tab lock when the browser has the Web Locks API.
 *
 * Two tabs replaying at once is safe for the backend, which is idempotent per
 * (order_item_id, code), but it doubles the requests and the noise.
 */
async function withReplayLock<T>(task: () => Promise<T>): Promise<T> {
  const locks = typeof navigator !== "undefined" ? navigator.locks : undefined;
  if (!locks?.request) return task();
  return locks.request(REPLAY_LOCK_NAME, task) as Promise<T>;
}

export function useOfflineQueue(
  config: ApiConfig,
  options: { onReplayed?: (summary: ReplaySummary) => void } = {},
): OfflineQueue {
  const storeRef = useRef<OfflineQueueStore | null>(null);
  const replayingRef = useRef(false);
  const onReplayedRef = useRef(options.onReplayed);
  const [pending, setPending] = useState<OfflineEvent[]>([]);
  const [blocked, setBlocked] = useState<BlockedEvent[]>([]);
  const [storageError, setStorageError] = useState("");
  const [replaying, setReplaying] = useState(false);

  useEffect(() => {
    onReplayedRef.current = options.onReplayed;
  }, [options.onReplayed]);

  const store = useCallback(() => {
    if (!storeRef.current) storeRef.current = createIndexedDbQueueStore();
    return storeRef.current;
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [nextPending, nextBlocked] = await Promise.all([
        store().listPending(),
        store().listBlocked(),
      ]);
      setPending(nextPending);
      setBlocked(nextBlocked);
      setStorageError("");
      return true;
    } catch (error) {
      setStorageError(error instanceof Error ? error.message : "Локальное хранилище недоступно");
      return false;
    }
  }, [store]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const replayNow = useCallback(async (): Promise<ReplaySummary | null> => {
    if (replayingRef.current) return null;
    replayingRef.current = true;
    setReplaying(true);
    try {
      const summary = await withReplayLock(() => replayQueue(store(), {
        sendScan: async (event) => {
          await createScan(config, {
            order_item_id: event.orderItemId,
            code: event.code,
            workstation_id: event.workstationId,
            scanned_by: event.actor,
          });
        },
        sendComplete: async (event) => {
          await completeWarehouseOrder(config, event.orderId);
        },
      }));
      await refresh();
      if (summary.synced > 0) onReplayedRef.current?.(summary);
      return summary;
    } catch (error) {
      setStorageError(error instanceof Error ? error.message : "Не удалось разобрать очередь");
      return null;
    } finally {
      replayingRef.current = false;
      setReplaying(false);
    }
  }, [config, refresh, store]);

  const enqueueScan = useCallback(async (input: EnqueueScanInput) => {
    const now = new Date().toISOString();
    await store().enqueue({
      type: "scan",
      orderId: input.orderId,
      orderItemId: input.orderItemId,
      code: input.code,
      actor: input.actor,
      workstationId: input.workstationId,
      scannedAt: now,
      createdAt: now,
      attempts: 0,
      lastError: "",
    });
    await refresh();
  }, [refresh, store]);

  const dismissBlocked = useCallback(async (key: string) => {
    await store().dismissBlocked(key);
    await refresh();
  }, [refresh, store]);

  const removePendingScan = useCallback(async (event: OfflineEvent) => {
    await store().remove(offlineEventKey(event));
    await refresh();
  }, [refresh, store]);

  const lastPendingScanForItem = useCallback(
    (orderItemId: string) => {
      const own = pending.filter((event) => event.type === "scan" && event.orderItemId === orderItemId);
      return own.at(-1) ?? null;
    },
    [pending],
  );

  // Auto-replay: connection restored, tab brought back, or a periodic retry
  // while anything is still waiting.
  useEffect(() => {
    if (!pending.length) return;

    const retry = () => { void replayNow(); };
    const onVisible = () => { if (document.visibilityState === "visible") retry(); };

    window.addEventListener("online", retry);
    document.addEventListener("visibilitychange", onVisible);
    const timer = window.setInterval(retry, AUTO_REPLAY_INTERVAL_MS);

    return () => {
      window.removeEventListener("online", retry);
      document.removeEventListener("visibilitychange", onVisible);
      window.clearInterval(timer);
    };
  }, [pending.length, replayNow]);

  const pendingCodes = useMemo(() => {
    const codes = new Set<string>();
    for (const event of pending) {
      if (event.type === "scan" && event.code) codes.add(event.code);
    }
    return codes;
  }, [pending]);

  const pendingScansForOrder = useCallback(
    (orderId: string) => pending.filter(
      (event) => event.type === "scan" && event.orderId === orderId,
    ).length,
    [pending],
  );

  return {
    pending,
    blocked,
    pendingCodes,
    storageReady: !storageError,
    storageError,
    replaying,
    enqueueScan,
    replayNow,
    dismissBlocked,
    pendingScansForOrder,
    lastPendingScanForItem,
    removePendingScan,
    shouldQueue: (error: unknown) => classifyReplayFailure(error) === "retry",
  };
}

export { offlineEventKey };
