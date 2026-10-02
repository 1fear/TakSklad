/** Product keys, SKU guard, labels and mismatch rules against the desktop (corpus.tables and corpus.products) */

import { describe, expect, it } from "vitest";

import {
  AGGREGATE_BOX_BLOCK_QUANTITY,
  AGGREGATE_BOX_PRODUCT_PREFIXES,
  blockQuantityForCode,
  scanTypeForCode,
} from "../../features/warehouse/scanQuantities";
import {
  aggregateProductMismatch,
  formatProductKeyLabel,
  PRODUCT_KEY_LABELS,
  productKeyFromName,
  scanCodeProductKey,
  scanProductMismatch,
  scanSkuGuardStatus,
  UNIT_PRODUCT_PREFIXES,
} from "../logic/productKeys";
import { corpus, labelled } from "./support";

const { tables, products } = corpus;

describe("tables", () => {
  it("keep the unit prefixes of the desktop", () => expect(UNIT_PRODUCT_PREFIXES).toEqual(tables.unit_prefixes));
  it("keep the box prefixes of the desktop", () => expect(AGGREGATE_BOX_PRODUCT_PREFIXES).toEqual(tables.aggregate_prefixes));
  it("keep the block quantity of a box", () => expect(AGGREGATE_BOX_BLOCK_QUANTITY).toBe(tables.aggregate_block_quantity));
  it("keep the SKU labels of the desktop", () => expect(PRODUCT_KEY_LABELS).toEqual(tables.product_key_labels));
});

describe("product names", () => {
  it.each(labelled(products.names, (item) => JSON.stringify(item.product)))("%s", (_name, item) => {
    const { product_key: productKey, ...guard } = item.guard as { state: string; message: string; product_key?: string };
    expect(productKeyFromName(item.product)).toBe(item.key);
    expect(scanSkuGuardStatus({ product: item.product })).toEqual({ ...guard, productKey });
  });

  it("has no guard without a position", () => expect(scanSkuGuardStatus(null)).toEqual(products.guard_none));
});

describe("codes", () => {
  it.each(labelled(products.code_keys, (item) => JSON.stringify(item.code)))("%s", (_name, item) => {
    expect(scanCodeProductKey(item.code)).toBe(item.key);
    expect(scanTypeForCode(item.code)).toBe(item.scan_type);
    expect(blockQuantityForCode(item.code)).toBe(item.blocks);
  });

  it.each(labelled(products.mismatch, (item) => JSON.stringify([item.code, item.product])))("mismatch %s", (_name, item) => {
    expect(scanProductMismatch(item.code, item.product)).toBe(item.mismatch);
    expect(aggregateProductMismatch(item.code, item.product)).toBe(item.box_mismatch);
  });
});

describe("labels", () => {
  // "constructor" must come back as itself, not as a member of Object.prototype
  it.each(labelled(products.labels, (item) => JSON.stringify(item.key)))("%s", (_name, item) => {
    expect(formatProductKeyLabel(item.key)).toBe(item.label);
  });
});
