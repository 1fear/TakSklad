/**
 * Durable offline queue events for the browser operator surface.
 *
 * Mirrors the Windows desktop queue contract (`src/taksklad/backend_events.py`):
 * an event id is derived from the tuple that identifies the operation, never
 * from time or attempt count, so re-scanning the same code onto the same order
 * item collapses into a single queued event instead of producing duplicates.
 *
 * The desktop hashes the tuple (`make_backend_event_id`). The browser keeps the
 * tuple readable instead: no collisions and the key stays greppable in
 * diagnostics.
 */

import { normalizeKizCode } from "../kizFormat";

export type OfflineEventType = "scan" | "order_complete";

export type OfflineEvent = {
  type: OfflineEventType;
  orderId: string;
  orderItemId: string;
  code: string;
  actor: string;
  workstationId: string;
  scannedAt: string;
  createdAt: string;
  attempts: number;
  lastError: string;
};

/**
 * Stable identity of a queued event.
 *
 * Codes are compared after desktop-identical normalization, which trims spaces
 * only. KIZ codes are case sensitive, so codes differing in case are different
 * codes and must not collapse.
 */
export function offlineEventKey(event: OfflineEvent): string {
  if (event.type === "order_complete") return `order_complete|${event.orderId}`;
  return `scan|${event.orderItemId}|${normalizeKizCode(event.code)}`;
}
