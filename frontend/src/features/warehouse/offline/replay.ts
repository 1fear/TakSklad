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
 *
 * An optional filter narrows a pass to part of the queue (the station sends one
 * position, or one order, at a time). Without it the pass covers everything.
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

/**
 * Which queued events a pass may send. Same predicate as the desktop
 * (`backend_event_matches_filter`, `src/taksklad/backend_events.py:427`): a scan
 * is chosen by its order item, an `order_complete` by its order. A set that is
 * missing selects nothing of that kind.
 */
export type ReplayFilter = {
  orderItemIds?: Set<string>;
  orderIds?: Set<string>;
};

/** No filter means every event; with a filter only the chosen ones. */
export function eventMatchesReplayFilter(event: OfflineEvent, filter?: ReplayFilter): boolean {
  if (!filter) return true;
  if (event.type === "scan") return filter.orderItemIds?.has(event.orderItemId) ?? false;
  return filter.orderIds?.has(event.orderId) ?? false;
}

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

export async function replayQueue(
  store: OfflineQueueStore,
  deps: ReplayDeps,
  filter?: ReplayFilter,
): Promise<ReplaySummary> {
  let synced = 0;
  let blocked = 0;
  let failed = 0;
  let consecutiveFailures = 0;

  // Orders whose scans did not all leave the queue during this pass. Completing
  // such an order would tell the backend the order is done while a physically
  // scanned block is still waiting to be sent.
  const ordersWithWaitingScans = new Set<string>();

  // A scan the filter keeps out of this pass is waiting too.
  const pending: OfflineEvent[] = [];
  for (const event of await store.listPending()) {
    if (eventMatchesReplayFilter(event, filter)) pending.push(event);
    else if (event.type === "scan") ordersWithWaitingScans.add(event.orderId);
  }

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
