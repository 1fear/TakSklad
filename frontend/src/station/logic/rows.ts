/**
 * One order position as the desktop program sees it, and the conversion from the API order
 * (src/taksklad/backend_client.py backend_order_to_rows, orders.py order_group_key)
 */

import type { Order } from "../../api";
import { blockQuantityForCode } from "../../features/warehouse/scanQuantities";
import { normalizeText, parseDateToStandard, parseIntValue } from "./text";

export type ScanEntry = { code: string; block_quantity: number };

export type StationRow = {
  orderDate: string;
  paymentType: string;
  client: string;
  address: string;
  representative: string;
  product: string;
  planBlocks: number;
  lineTotal: number;
  requestNumber: string;
  requestId: string;
  backendOrderId: string;
  backendOrderItemId: string;
  existingCodes: string[];
  existingEntries: ScanEntry[];
};

export type GroupKey = readonly [requestNumber: string, client: string, paymentType: string, address: string];

export function orderToRows(order: Order): StationRow[] {
  return order.items.map((item) => {
    // an item without recorded entries gets them from its codes: a box code counts for 50 blocks
    const entries = item.scan_entries?.length
      ? item.scan_entries
      : item.scan_codes.map((code) => ({ code: normalizeText(code), block_quantity: blockQuantityForCode(code) }));
    return {
      orderDate: parseDateToStandard(order.order_date) ?? "",
      paymentType: order.payment_type || "",
      client: order.client || "",
      address: order.address || "",
      representative: order.representative || "",
      product: item.product || "",
      planBlocks: parseIntValue(item.quantity_blocks),
      lineTotal: parseIntValue(item.line_total),
      requestNumber: order.skladbot_request_number || "",
      requestId: order.skladbot_request_id || "",
      backendOrderId: order.id || "",
      backendOrderItemId: item.id || "",
      existingCodes: [...item.scan_codes],
      existingEntries: entries.map(({ code, block_quantity }) => ({ code, block_quantity })),
    };
  });
}

/** Positions of one request, client, payment type and address form one card */
export function groupKey(row: StationRow): GroupKey {
  return [
    normalizeText(row.requestNumber),
    normalizeText(row.client) || "Клиент не указан",
    normalizeText(row.paymentType) || "Оплата не указана",
    normalizeText(row.address) || "Адрес не указан",
  ];
}

/** Blocks the given codes add up to: the recorded quantity of a code, else 50 for a box and 1 for a unit */
export function scannedBlocks(row: StationRow, codes: readonly string[]): number {
  const recorded = new Map(
    row.existingEntries.filter((entry) => normalizeText(entry.code)).map((entry) => [normalizeText(entry.code), entry]),
  );
  return codes.reduce((sum, code) => {
    const quantity = Math.trunc(recorded.get(normalizeText(code))?.block_quantity ?? 0);
    return sum + (quantity > 0 ? quantity : blockQuantityForCode(code));
  }, 0);
}
