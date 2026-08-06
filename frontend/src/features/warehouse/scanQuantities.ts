/**
 * How many blocks a scanned code represents.
 *
 * An aggregate box carries 50 blocks, a unit code carries one. The rule is
 * authoritative on the server (`backend/app/scan_quantities.py`) and mirrored
 * on the Windows desktop (`src/taksklad/scan_quantities.py`) because the
 * desktop has to show correct progress while offline. The browser now needs the
 * same mirror for the same reason: with no backend to ask, counting queued
 * events as one block each would tell the operator a position still needs 49
 * more blocks than it does.
 *
 * `tests/test_web_offline_queue_contract.py` pins the prefix table and the
 * block quantity against the Python sources so the three copies cannot drift.
 */

import { normalizeKizCode } from "./kizFormat";

export const AGGREGATE_BOX_BLOCK_QUANTITY = 50;

export const SCAN_TYPE_UNIT = "unit";
export const SCAN_TYPE_AGGREGATE_BOX = "aggregate_box";

export type ScanType = typeof SCAN_TYPE_UNIT | typeof SCAN_TYPE_AGGREGATE_BOX;

export const AGGREGATE_BOX_PRODUCT_PREFIXES: Record<string, string> = {
  "0104006396054012": "gold:ssl",
  "0104006396053985": "brown:op",
  "0104006396053954": "red:op",
  "0104006396054074": "brown:ssl",
  "0104006396054043": "red:ssl",
  "0104006396104448": "green:op",
  "0104006396104458": "green:op",
};

export function aggregateBoxProductKey(code: string): string {
  const text = normalizeKizCode(code);
  if (!text) return "";
  for (const [prefix, productKey] of Object.entries(AGGREGATE_BOX_PRODUCT_PREFIXES)) {
    if (text.startsWith(prefix)) return productKey;
  }
  return "";
}

export function scanTypeForCode(code: string): ScanType {
  return aggregateBoxProductKey(code) ? SCAN_TYPE_AGGREGATE_BOX : SCAN_TYPE_UNIT;
}

export function blockQuantityForCode(code: string): number {
  return scanTypeForCode(code) === SCAN_TYPE_AGGREGATE_BOX ? AGGREGATE_BOX_BLOCK_QUANTITY : 1;
}
