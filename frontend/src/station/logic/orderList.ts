/**
 * The list of order cards, grouped by shipment date (src/taksklad/order_list_models.py)
 * Groups keep first-seen order, then sort stably: dates by day, cards by request number
 */

import { groupKey, type GroupKey, type StationRow } from "./rows";
import {
  compareKeys,
  dateSortKey,
  formatOrderDateHeader,
  normalizeLookupText,
  normalizeText,
  parseDateToStandard,
  parseIntValue,
} from "./text";

export type OrderListRow =
  | { kind: "date"; title: string }
  | { kind: "order"; client: string; meta: string; summary: string; groupKey: GroupKey };

export type OrderListModel = {
  rows: OrderListRow[];
  totalGroups: number;
  visibleCards: number;
  subtitle: string;
  counter: string;
};

type Group = { key: GroupKey; rows: StationRow[]; date: string };

/** "WH-R-12" -> 12: the digits at the end of the request number, 0 when there are none (reports.py) */
function requestNumberValue(requestNumber: string): number {
  const digits = /(\d+)$/.exec(normalizeText(requestNumber));
  return digits ? parseIntValue(digits[1]) : 0;
}

/** Cards with a request number first, by its number, then by the lookup form of all four parts (reports.py) */
function cardSortKey([request, client, payment, address]: GroupKey): (number | string)[] {
  return [
    request ? 0 : 1,
    requestNumberValue(request),
    normalizeLookupText(request),
    normalizeLookupText(client),
    normalizeLookupText(payment),
    normalizeLookupText(address),
  ];
}

function searchArea(row: StationRow, [request, client, payment, address]: GroupKey): string {
  const parts = [request || "Без номера SkladBot", client, payment, address, normalizeText(row.representative), normalizeText(row.product)];
  return parts.join(" ").toLowerCase();
}

export function buildOrderListModel(rows: readonly StationRow[], search: string, today: Date): OrderListModel {
  const query = normalizeText(search).toLowerCase();
  const groups = new Map<string, Group>();
  const visible = new Set<Group>();

  for (const row of rows) {
    const key = groupKey(row);
    const id = JSON.stringify(key);
    // a card takes the date of its first position
    const group = groups.get(id) ?? { key, rows: [], date: parseDateToStandard(row.orderDate) || "Без даты" };
    groups.set(id, group);
    group.rows.push(row);
    if (!query || searchArea(row, key).includes(query)) visible.add(group);
  }

  const byDate = new Map<string, Group[]>();
  for (const group of visible) byDate.set(group.date, [...(byDate.get(group.date) ?? []), group]);

  const listRows: OrderListRow[] = [];
  const dates = [...byDate.keys()].sort((a, b) => compareKeys([dateSortKey(a)], [dateSortKey(b)]));
  for (const date of dates) {
    listRows.push({ kind: "date", title: formatOrderDateHeader(date, today).toUpperCase() });
    const cards = (byDate.get(date) ?? []).sort((a, b) => compareKeys(cardSortKey(a.key), cardSortKey(b.key)));
    for (const { key, rows: positions } of cards) {
      const blocks = positions.reduce((sum, position) => sum + position.planBlocks, 0);
      listRows.push({
        kind: "order",
        client: key[1],
        meta: `${key[0] || "Без номера SkladBot"} · ${formatOrderDateHeader(date, today)}`,
        summary: `${positions.length} SKU · ${blocks} блоков`,
        groupKey: key,
      });
    }
  }

  return {
    rows: listRows,
    totalGroups: groups.size,
    visibleCards: visible.size,
    subtitle: `${groups.size} активных заказов · список листается вниз`,
    counter: `Показаны ${visible.size} из ${groups.size}`,
  };
}
