/** Rows built from API orders and block counting against the desktop (corpus.rows, corpus.blocks) */

import { describe, expect, it } from "vitest";

import type { Order } from "../../api";
import { orderToRows, scannedBlocks } from "../logic/rows";
import { corpus, labelled, rowFromSpec, type RowSpec } from "./support";

describe("orderToRows", () => {
  it.each(labelled(corpus.rows, (item) => item.name))("%s", (_name, { input, output }) => {
    expect(orderToRows(input as unknown as Order)).toEqual((output as RowSpec[]).map(rowFromSpec));
  });

  it("does not share the code list with the order", () => {
    const order = structuredClone(corpus.rows[0].input) as unknown as Order;
    const [first] = orderToRows(order);
    first.existingCodes.push("x");
    expect(order.items[0].scan_codes).toEqual(corpus.rows[0].input.items[0].scan_codes);
  });
});

describe("scannedBlocks", () => {
  it.each(labelled(corpus.blocks, (item) => item.name))("%s", (_name, { input, output }) => {
    expect(scannedBlocks(rowFromSpec(input.row as RowSpec), input.codes)).toBe(output);
  });
});
