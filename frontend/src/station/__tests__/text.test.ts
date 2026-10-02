/**
 * Text, date and number helpers against what the desktop answered (corpus.text)
 * Cases are where JavaScript would answer differently from Python: float syntax, strptime, whitespace, string order
 */

import { describe, expect, it } from "vitest";

import {
  compareText,
  dateSortKey,
  formatMoney,
  formatOrderDateHeader,
  normalizeLookupText,
  normalizeText,
  parseDateToStandard,
  parseIntValue,
} from "../logic/text";
import { corpus, decodeFloat, labelled, LATE, TODAY } from "./support";

const { dates, ints, money, strip, lookup, sorted } = corpus.text;

describe("dates", () => {
  it.each(labelled(dates, (item) => JSON.stringify(item.value)))("%s", (_name, item) => {
    expect(parseDateToStandard(item.value)).toBe(item.standard);
    expect(formatOrderDateHeader(item.value, TODAY)).toBe(item.header);
    // the day is named by the calendar, not by the hour
    expect(formatOrderDateHeader(item.value, LATE)).toBe(item.header_late);
    expect(dateSortKey(item.value)).toBe(item.sort_day ?? Infinity);
  });
});

describe("numbers", () => {
  // toBe compares with Object.is, so a negative zero where Python has 0 fails here too
  it.each(labelled(ints, (item) => JSON.stringify(item.value)))("parseIntValue(%s)", (_name, item) => {
    expect(parseIntValue(decodeFloat(item.value))).toBe(item.result);
  });

  it.each(labelled(money, (item) => JSON.stringify(item.value)))("formatMoney(%s)", (_name, item) => {
    expect(formatMoney(item.value)).toBe(item.text);
  });
});

describe("whitespace", () => {
  // Python str.strip drops \x1c-\x1f and \x85 but keeps the byte order mark, the other way round from JS trim
  it.each(labelled(strip, (item) => JSON.stringify(item.value)))("normalizeText(%s)", (_name, item) => {
    expect(normalizeText(item.value)).toBe(item.result);
  });

  it.each(labelled(lookup, (item) => JSON.stringify(item.value)))("normalizeLookupText(%s)", (_name, item) => {
    expect(normalizeLookupText(item.value)).toBe(item.result);
  });
});

describe("ordering", () => {
  it("sorts by code point like Python, which the default JS sort does not", () => {
    expect([...sorted.values].sort(compareText)).toEqual(sorted.result);
    expect([...sorted.values].sort()).not.toEqual(sorted.result);
  });
});
