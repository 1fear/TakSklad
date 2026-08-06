/**
 * How the browser offline queue reads a backend answer during replay.
 *
 * Three outcomes, same as the desktop queue (`src/taksklad/backend_events.py`):
 *
 * - `synced`  the backend already holds this scan, a 409 `scan_duplicate_ack`
 *             is an acknowledgement and not a failure;
 * - `blocked` the backend refused on the merits, repeating cannot help, the
 *             event must stop retrying and become visible to the operator;
 * - `retry`   the backend was not reached or could not answer yet.
 *
 * Deliberate difference from the desktop: the desktop only treats HTTP 409 as
 * non-retryable, so any other 4xx retries forever. The backend raises
 * `kiz_format_invalid` with HTTP 422 (`backend/app/orders_service.py:215`),
 * which a desktop queue would spin on indefinitely. The browser blocks every
 * 4xx except the three that genuinely change on retry: 401 needs a fresh
 * session, 408 is a timeout, 429 is rate limiting. A scan is never dropped by
 * this rule, blocking only moves it to a durable, operator-visible section.
 *
 * `tests/test_web_offline_queue_contract.py` pins the code list below against
 * the desktop list so the two clients cannot disagree about which conflicts are
 * final.
 */

import { ApiRequestError } from "../../../api/core";

export const DUPLICATE_SCAN_ACK_CODE = "scan_duplicate_ack";

/** Mirror of `KNOWN_NON_RETRYABLE_SCAN_ERROR_CODES` in the desktop queue. */
export const NON_RETRYABLE_SCAN_CODES = [
  "kiz_format_invalid",
  "kiz_already_owned",
  "order_item_fully_scanned_new_code",
  "order_closed",
  "transfer_order_irreversible",
  "legal_entity_unresolved",
  "aggregate_box_product_mismatch",
  "aggregate_box_exceeds_plan",
  "scan_product_mismatch",
  "shipment_manifest_mismatch",
] as const;

/** 4xx answers that a later attempt can genuinely resolve. */
const RETRYABLE_CLIENT_STATUSES = new Set([401, 408, 429]);

/**
 * Browser-security rejections the backend raises before any warehouse work
 * happens (`backend/app/main.py::require_browser_request_security`).
 *
 * They arrive as HTTP 403 but describe a stale tab, not a refused operation:
 * the operator surface already treats them as "refresh and sign in again"
 * (`frontend/src/workspace/OperatorWorkspace.tsx`). Blocking a queued scan on
 * one of these would strand a KIZ the backend never even looked at.
 */
export const RECOVERABLE_BROWSER_SECURITY_CODES = ["csrf_invalid", "origin_denied"] as const;

const RECOVERABLE_CODES = new Set<string>(RECOVERABLE_BROWSER_SECURITY_CODES);

export type ReplayVerdict = "retry" | "blocked" | "synced";

export function classifyReplayFailure(error: unknown): ReplayVerdict {
  if (!(error instanceof ApiRequestError)) return "retry";
  if (error.status === 409 && error.code === DUPLICATE_SCAN_ACK_CODE) return "synced";
  if (RECOVERABLE_CODES.has(error.code)) return "retry";
  if (error.status >= 400 && error.status < 500 && !RETRYABLE_CLIENT_STATUSES.has(error.status)) return "blocked";
  return "retry";
}
