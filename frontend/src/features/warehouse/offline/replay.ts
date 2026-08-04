/**
 * Replay of the browser offline queue against the backend.
 *
 * Runs in the page, not in a service worker: writing a scan needs the
 * same-origin session cookie and the CSRF header, both of which live in the
 * tab. A background replay would answer 401 with nobody watching.
 *
 * Safety rests on the backend being idempotent per (order_item_id, code):
 * `create_scan` returns the existing scan instead of creating a second one
 * (`backend/app/orders_service.py:240-241`), so replaying an event the backend
 * already accepted cannot double-count a block.
 *
 * The pass stops at the first `retry` verdict on purpose. Events are ordered,
 * an `order_complete` must never overtake its own scans, and hammering a dead
 * backend with the whole queue helps nobody.
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

export async function replayQueue(store: OfflineQueueStore, deps: ReplayDeps): Promise<ReplaySummary> {
  const pending = await store.listPending();
  let synced = 0;
  let blocked = 0;
  let failed = 0;

  for (const event of pending) {
    const key = offlineEventKey(event);
    try {
      if (event.type === "scan") await deps.sendScan(event);
      else await deps.sendComplete(event);
      await store.remove(key);
      synced += 1;
      continue;
    } catch (error) {
      const verdict = classifyReplayFailure(error);

      if (verdict === "synced") {
        await store.remove(key);
        synced += 1;
        continue;
      }

      if (verdict === "blocked") {
        const reason = blockReason(error);
        await store.block(key, reason.code, reason.message);
        blocked += 1;
        continue;
      }

      failed += 1;
      await store.update(key, {
        attempts: Number(event.attempts ?? 0) + 1,
        lastError: errorMessage(error),
      });
      break;
    }
  }

  return { synced, blocked, failed, remaining: (await store.listPending()).length };
}
