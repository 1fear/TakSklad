import { describe, expect, it } from "vitest";

import {
  KIZ_MAX_LENGTH,
  KIZ_MIN_LENGTH,
  isKizFormatRule,
  kizFormatMessage,
  kizFormatViolation,
  normalizeKizCode,
} from "../features/warehouse/kizFormat";

// Shared corpus. tests/test_kiz_format_contract.py runs the SAME cases through
// the Windows desktop validator and asserts identical verdicts, so the browser
// guard can never drift away from the desktop guard it replaces.
export const KIZ_FORMAT_CORPUS: Array<{ code: string; rule: string }> = [
  { code: "0104006396053947217ABCDEF", rule: "" },
  { code: "010400639605401221UZ1112022525522513824013040046110ZIG1218229310000", rule: "" },
  { code: "0104006396053947217ABC\x1dDEF", rule: "" },
  { code: "  0104006396053947217ABCDEF  ", rule: "" },
  { code: "", rule: "empty" },
  { code: "   ", rule: "empty" },
  { code: "1234567890123456789012", rule: "prefix" },
  { code: "010123456789", rule: "too_short" },
  { code: `01${"x".repeat(KIZ_MAX_LENGTH)}`, rule: "too_long" },
  { code: "0104006396053947217ПРИВЕТ", rule: "cyrillic" },
  { code: "0104006396053947 217ABCDEF", rule: "whitespace" },
  { code: "0104006396053947217ABCé", rule: "charset" },
];

describe("KIZ format guard mirrors the desktop scanner rules", () => {
  it("pins the desktop length bounds", () => {
    expect(KIZ_MIN_LENGTH).toBe(20);
    expect(KIZ_MAX_LENGTH).toBe(120);
  });

  it("trims only spaces, tabs and line breaks like the desktop client", () => {
    expect(normalizeKizCode("  0104006396053947217ABCDEF \t\r\n")).toBe("0104006396053947217ABCDEF");
    expect(normalizeKizCode("0104006396053947217ABCDEF")).toBe("0104006396053947217ABCDEF");
  });

  it.each(KIZ_FORMAT_CORPUS)("classifies $code", ({ code, rule }) => {
    expect(kizFormatViolation(code)).toBe(rule);
  });

  it("accepts a real DataMatrix code that carries a GS separator", () => {
    expect(kizFormatViolation("0104006396053947217ABC\x1dDEF")).toBe("");
  });

  it("uses the exact desktop operator wording", () => {
    expect(kizFormatMessage("prefix")).toBe("КИЗ должен начинаться с 01");
    expect(kizFormatMessage("cyrillic")).toBe("Код содержит русские буквы! Используйте только латиницу");
    expect(kizFormatMessage("too_short")).toBe("Код слишком короткий для КИЗа (минимум 20 символов)");
    expect(kizFormatMessage("too_long")).toBe("Код слишком длинный для КИЗа (максимум 120 символов)");
    expect(kizFormatMessage("whitespace")).toBe("Код содержит пробелы или переносы");
    expect(kizFormatMessage("charset")).toBe("Код содержит недопустимые символы");
    expect(kizFormatMessage("empty")).toBe("Код пустой");
  });

  it("recognises only real rule ids so backend reasons keep their own messages", () => {
    expect(isKizFormatRule("prefix")).toBe(true);
    expect(isKizFormatRule("same_order_item_scan")).toBe(false);
    expect(isKizFormatRule("saved")).toBe(false);
  });
});
