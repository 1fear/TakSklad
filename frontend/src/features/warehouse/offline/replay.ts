/**
 * Replay of the browser offline queue against the backend.
 *
 * Runs in the page, not in a service worker: writing a scan needs the
 * same-origin session cookie and the CSRF header, both of which live in the
 * tab. A background replay would answer 401 with nobody watching.
 *
 * Safety rests on the backend being idempotent per (order_item_id, code):
 * `create_scan` returns the existing scan instead of creating a second one
 * (`backend/app/orders_service.py:435-436`), so replaying an event the backend
 * already accepted cannot double-count a block.
 *
 * Two rules keep the queue draining without hammering a dead backend:
 *
 * - a single event that keeps failing is skipped, not allowed to seal the rest
 *   of the queue behind it, because it may belong to a different order
 *   entirely;
 * - `MAX_CONSECUTIVE_RETRY_FAILURES` failures in a row end the pass, which is
 *   what an unreachable backend looks like.
 *
 * Ordering still holds where it matters: `order_complete` is never sent while
 * the same order still has a scan waiting in the queue, whatever the reason it
 * is waiting.
 */

import { classifyReplayFailure } from "./errorPolicy";
import type { OfflineQueueStore } from "./queueStore";
import { offlineEventKey, type OfflineEvent } from "./queueTypes";

export type ReplayDeps = {
  sendScan(event: OfflineEvent): Promise<void>;
  sendComplete(event: OfflineEvent): Promise<void>;
};

export type ReplaySummary = {
  synced: number;
  blocked: number;
  failed: number;
  remaining: number;
};

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function blockReason(error: unknown): { code: string; message: string } {
  const candidate = error as { code?: unknown; message?: unknown };
  return {
    code: typeof candidate?.code === "string" ? candidate.code : "",
    message: errorMessage(error),
  };
}

/** Consecutive retryable failures that mean the backend itself is unreachable. */
export const MAX_CONSECUTIVE_RETRY_FAILURES = 3;

export async function replayQueue(store: OfflineQueueStore, deps: ReplayDeps): Promise<ReplaySummary> {
  const pending = await store.listPending();
  let synced = 0;
  let blocked = 0;
  let failed = 0;
  let consecutiveFailures = 0;

  // Orders whose scans did not all leave the queue during this pass. Completing
  // such an order would tell the backend the order is done while a physically
  // scanned block is still waiting to be sent.
  const ordersWithWaitingScans = new Set<string>();

  for (const event of pending) {
    const key = offlineEventKey(event);

    if (event.type === "order_complete" && ordersWithWaitingScans.has(event.orderId)) {
      continue;
    }

    try {
      if (event.type === "scan") await deps.sendScan(event);
      else await deps.sendComplete(event);
      await store.remove(key);
      synced += 1;
      consecutiveFailures = 0;
      continue;
    } catch (error) {
      const verdict = classifyReplayFailure(error);

      if (verdict === "synced") {
        await store.remove(key);
        synced += 1;
        consecutiveFailures = 0;
        continue;
      }

      if (verdict === "blocked") {
        const reason = blockReason(error);
        await store.block(key, reason.code, reason.message);
        blocked += 1;
        consecutiveFailures = 0;
        continue;
      }

      failed += 1;
      consecutiveFailures += 1;
      if (event.type === "scan") ordersWithWaitingScans.add(event.orderId);
      await store.update(key, {
        attempts: Number(event.attempts ?? 0) + 1,
        lastError: errorMessage(error),
      });
      if (consecutiveFailures >= MAX_CONSECUTIVE_RETRY_FAILURES) break;
    }
  }

  return { synced, blocked, failed, remaining: (await store.listPending()).length };
}
