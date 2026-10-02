/**
 * What every station test shares: the corpus, the two clocks and the conversion of a corpus position into a row
 * The corpus is the only source of expected values (tools/generate_station_parity_corpus.py), tests read nothing else
 */

import raw from "../__fixtures__/parity-corpus.json";
import type { StationRow } from "../logic/rows";

export const corpus = raw;
export const TODAY = new Date(corpus.today);
export const LATE = new Date(corpus.today_late);

/** One order position as the generator spells it out, every field present */
export type RowSpec = {
  date: string;
  payment: string;
  client: string;
  address: string;
  representative: string;
  request: string;
  request_id: string;
  order: string;
  item: string;
  product: string;
  plan: number;
  total: number;
  codes: string[];
  entries: { code: string; block_quantity: number }[];
};

export function rowFromSpec(spec: RowSpec): StationRow {
  return {
    orderDate: spec.date,
    paymentType: spec.payment,
    client: spec.client,
    address: spec.address,
    representative: spec.representative,
    product: spec.product,
    planBlocks: spec.plan,
    lineTotal: spec.total,
    requestNumber: spec.request,
    requestId: spec.request_id,
    backendOrderId: spec.order,
    backendOrderItemId: spec.item,
    existingCodes: spec.codes,
    existingEntries: spec.entries,
  };
}

/** `it.each` rows from corpus items; an empty list would run no test and pass, so it is refused */
export function labelled<T>(items: readonly T[], label: (item: T) => string): [string, T][] {
  if (items.length === 0) throw new Error("the corpus section is empty");
  return items.map((item) => [label(item), item]);
}

/** JSON has no NaN or Infinity: the generator sends them as {"float": "NaN"} */
export function decodeFloat(value: unknown): unknown {
  return value !== null && typeof value === "object" ? Number((value as { float: string }).float) : value;
}
