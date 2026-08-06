import { describe, expect, it } from "vitest";

import {
  AGGREGATE_BOX_BLOCK_QUANTITY,
  AGGREGATE_BOX_PRODUCT_PREFIXES,
  blockQuantityForCode,
  scanTypeForCode,
} from "../features/warehouse/scanQuantities";

describe("blockQuantityForCode", () => {
  it("обычный КИЗ это один блок", () => {
    expect(blockQuantityForCode("0104006396053947217ABCDEF")).toBe(1);
  });

  it.each(Object.keys(AGGREGATE_BOX_PRODUCT_PREFIXES))("короб %s это 50 блоков", (prefix) => {
    expect(blockQuantityForCode(`${prefix}217ABCDEF`)).toBe(AGGREGATE_BOX_BLOCK_QUANTITY);
  });

  it("неизвестный префикс считается штучным кодом", () => {
    expect(blockQuantityForCode("0199999999999999999ABCDEF")).toBe(1);
  });

  it("пустой код не роняет расчёт", () => {
    expect(blockQuantityForCode("")).toBe(1);
  });

  it("пробелы вокруг кода не мешают распознать короб", () => {
    expect(blockQuantityForCode("  0104006396054012217ABCDEF  ")).toBe(AGGREGATE_BOX_BLOCK_QUANTITY);
  });

  it("тип скана различает короб и штуку", () => {
    expect(scanTypeForCode("0104006396054012217ABCDEF")).toBe("aggregate_box");
    expect(scanTypeForCode("0104006396053947217ABCDEF")).toBe("unit");
  });
});
