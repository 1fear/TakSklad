/**
 * Which SKU a KIZ code or a product name stands for, and the SKU guard built on it
 * (src/taksklad/scan_quantities.py, desktop_scan_rules.py). The box prefixes and the
 * block quantity already live in features/warehouse/scanQuantities.ts and are reused
 */

import { aggregateBoxProductKey, scanTypeForCode, SCAN_TYPE_AGGREGATE_BOX } from "../../features/warehouse/scanQuantities";
import { casefold, normalizeText, splitWords } from "./text";

export const UNIT_PRODUCT_PREFIXES: Readonly<Record<string, string>> = {
  "0104006396054005": "gold:ssl",
  "0104006396053978": "brown:op",
  "0104006396053947": "red:op",
  "0104006396054067": "brown:ssl",
  "0104006396054036": "red:ssl",
  "0104006396104441": "green:op",
  "0104006396104199": "brown:kssl",
  "0104006396104229": "green:kssl",
};

const PRODUCT_COLORS = ["brown", "red", "gold", "green"];
const PRODUCT_FORMATS = ["op", "ssl", "kssl"];

/** "Chapman RED OP 20" -> "red:op": a color and a format as words, or glued together like "brownop" */
export function productKeyFromName(product: unknown): string {
  const tokens = splitWords(casefold(normalizeText(product)).replace(/[`"']/g, " "));
  const compact = tokens.join("");
  const color = PRODUCT_COLORS.find((item) => tokens.includes(item) || compact.includes(item)) ?? "";
  const format = PRODUCT_FORMATS.find((item) => tokens.includes(item) || (color && compact.includes(color + item))) ?? "";
  return color && format ? `${color}:${format}` : "";
}

function unitProductKey(code: string): string {
  const text = normalizeText(code);
  const prefix = Object.keys(UNIT_PRODUCT_PREFIXES).find((item) => text.startsWith(item));
  return prefix ? UNIT_PRODUCT_PREFIXES[prefix] : "";
}

export function scanCodeProductKey(code: string): string {
  return aggregateBoxProductKey(code) || unitProductKey(code);
}

/** A named product that the code's SKU does not match; a product the rules cannot name never mismatches */
export function scanProductMismatch(code: string, product: unknown): boolean {
  const productKey = productKeyFromName(product);
  if (!productKey) return false;
  const codeKey = scanCodeProductKey(code);
  return !codeKey || productKey !== codeKey;
}

/** Only for box codes: a box of one SKU must not close a position of another, or of an unnamed one */
export function aggregateProductMismatch(code: string, product: unknown): boolean {
  if (scanTypeForCode(code) !== SCAN_TYPE_AGGREGATE_BOX) return false;
  const productKey = productKeyFromName(product);
  return !productKey || productKey !== aggregateBoxProductKey(code);
}

export const PRODUCT_KEY_LABELS: Readonly<Record<string, string>> = {
  "brown:op": "Brown OP",
  "red:op": "RED OP",
  "gold:ssl": "Gold SSL",
  "brown:ssl": "Brown SSL",
  "red:ssl": "RED SSL",
  "green:op": "Green OP",
  "brown:kssl": "Brown KSSL",
  "green:kssl": "Green KSSL",
};

export function formatProductKeyLabel(productKey: unknown): string {
  const key = normalizeText(productKey);
  if (!key) return "не распознан";
  return Object.hasOwn(PRODUCT_KEY_LABELS, key) ? PRODUCT_KEY_LABELS[key] : key;
}

export type SkuGuardStatus = {
  state: "unavailable" | "unknown" | "active";
  message: string;
  productKey?: string;
};

export function scanSkuGuardStatus(row: { product: string } | null): SkuGuardStatus {
  if (!row) return { state: "unavailable", message: "SKU-защита недоступна: выберите позицию." };
  const product = normalizeText(row.product);
  if (!product) return { state: "unknown", message: "SKU-защита не активна: товар не указан." };
  const productKey = productKeyFromName(product);
  if (!productKey) return { state: "unknown", message: `SKU-защита не активна: товар не распознан (${product}).` };
  return { state: "active", message: `SKU-защита активна: ${formatProductKeyLabel(productKey)}.`, productKey };
}
