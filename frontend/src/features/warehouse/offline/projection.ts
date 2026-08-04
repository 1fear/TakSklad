/**
 * Local progress of an order item, server state plus what is still queued.
 *
 * Offline the backend confirms nothing, so without this projection the operator
 * scans blind: the counter would stay at the last server value while blocks
 * keep leaving the warehouse.
 *
 * `scannedBlocks` never exceeds the plan, because the operator reads it as
 * "how much of this position is done". `pendingBlocks` is the raw queue depth
 * for this item and is reported separately so an over-scan stays visible.
 */

import type { OfflineEvent } from "./queueTypes";

export type ItemProgressInput = {
  id: string;
  quantity_blocks: number;
  scanned_blocks: number;
};

export type ItemProgress = {
  scannedBlocks: number;
  pendingBlocks: number;
  complete: boolean;
};

export function projectItemProgress(item: ItemProgressInput, pending: OfflineEvent[]): ItemProgress {
  const pendingBlocks = pending.filter(
    (event) => event.type === "scan" && event.orderItemId === item.id,
  ).length;

  const planned = Number(item.quantity_blocks ?? 0) || 0;
  const confirmed = Number(item.scanned_blocks ?? 0) || 0;
  const raw = confirmed + pendingBlocks;
  const scannedBlocks = planned > 0 ? Math.min(raw, planned) : raw;

  return { scannedBlocks, pendingBlocks, complete: planned > 0 && scannedBlocks >= planned };
}
