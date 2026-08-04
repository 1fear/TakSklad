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

import { normalizeKizCode } from "../kizFormat";
import { blockQuantityForCode } from "../scanQuantities";
import type { OfflineEvent } from "./queueTypes";

export type ItemProgressInput = {
  id: string;
  quantity_blocks: number;
  scanned_blocks: number;
  /**
   * Codes the backend already holds for this item.
   *
   * A queued event survives until the replay confirms it, and an acknowledged
   * write can still lose its answer (tab closed, connection cut after the row
   * was committed). Without this list the same physical scan would be counted
   * twice: once in `scanned_blocks` and again as a pending event.
   */
  scan_codes?: string[];
};

export type ItemProgress = {
  scannedBlocks: number;
  pendingBlocks: number;
  complete: boolean;
};

export function projectItemProgress(item: ItemProgressInput, pending: OfflineEvent[]): ItemProgress {
  const confirmedCodes = new Set((item.scan_codes ?? []).map(normalizeKizCode).filter(Boolean));

  const pendingBlocks = pending
    .filter((event) => event.type === "scan"
      && event.orderItemId === item.id
      && !confirmedCodes.has(normalizeKizCode(event.code)))
    .reduce((total, event) => total + blockQuantityForCode(event.code), 0);

  const planned = Number(item.quantity_blocks ?? 0) || 0;
  const confirmed = Number(item.scanned_blocks ?? 0) || 0;
  const raw = confirmed + pendingBlocks;
  const scannedBlocks = planned > 0 ? Math.min(raw, planned) : raw;

  return { scannedBlocks, pendingBlocks, complete: planned > 0 && scannedBlocks >= planned };
}
