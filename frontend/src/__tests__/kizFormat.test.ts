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
  { code: "0104006396053947217ABCDEF93GHIJKLMN", rule: "" },
  { code: "010400639605401221UZ1112022525522513824013040046110ZIG1218229310000", rule: "" },
  { code: "0104006396053947217ABC\x1dDEF93GHIJKLM", rule: "" },
  { code: "  0104006396053947217ABCDEF93GHIJKLMN  ", rule: "" },
  { code: "", rule: "empty" },
  { code: "   ", rule: "empty" },
  { code: "1234567890123456789012", rule: "prefix" },
  { code: "010123456789", rule: "too_short" },
  { code: `01${"x".repeat(KIZ_MAX_LENGTH)}`, rule: "too_long" },
  { code: "0104006396053947217ПРИВЕТ", rule: "cyrillic" },
  { code: "0104006396053947 217ABCDEF", rule: "whitespace" },
  { code: "0104006396053947217ABCé", rule: "charset" },
  // Scanner artifacts seen in production, see backend/app/kiz_format.py.
  { code: "010400639605394A217ABCDEF93GHIJKLMN", rule: "head" },
  { code: "0104006396053947217ABCDEF93GHIJKLMN0104006396053947217ZZZZZZ93QQQQQQQQ", rule: "double_mark" },
  { code: "0104006396053947217ABCDEF93GHIJKLMNWH-R-214126", rule: "length" },
  { code: "0104006396053947217ABCDEF", rule: "length" },
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
    expect(kizFormatViolation("0104006396053947217ABC\x1dDEF93GHIJKLM")).toBe("");
  });

  it("refuses the scanner artifacts that reached production", () => {
    // Two marks glued into one code: the second block would ship unrecorded.
    expect(
      kizFormatViolation("01040063960539472176,0g?X934hak2Kus0104006396053947217AUBn4g93e88UGqAL"),
    ).toBe("double_mark");
    // A mark with the request number appended by the scanner.
    expect(kizFormatViolation("01040063960539782172,X,sI93Y29dNx3AWH-R-214126")).toBe("length");
    // A truncated box code.
    expect(kizFormatViolation("010400639605395421UZ1112042612019561324013040029310ZIG12315793100")).toBe("length");
  });

  it("keeps a real box code valid even though its digit tail contains a mark head", () => {
    expect(kizFormatViolation("010400639605404321UZ1112022612500181524013040046210ZIG1231589310000")).toBe("");
  });

  it("uses the exact desktop operator wording", () => {
    expect(kizFormatMessage("prefix")).toBe("КИЗ должен начинаться с 01");
    expect(kizFormatMessage("cyrillic")).toBe("Код содержит русские буквы! Используйте только латиницу");
    expect(kizFormatMessage("too_short")).toBe("Код слишком короткий для КИЗа (минимум 20 символов)");
    expect(kizFormatMessage("too_long")).toBe("Код слишком длинный для КИЗа (максимум 120 символов)");
    expect(kizFormatMessage("whitespace")).toBe("Код содержит пробелы или переносы");
    expect(kizFormatMessage("charset")).toBe("Код содержит недопустимые символы");
    expect(kizFormatMessage("empty")).toBe("Код пустой");
    expect(kizFormatMessage("head")).toBe("Код не похож на марку: после 01 ожидается GTIN из 14 цифр");
    expect(kizFormatMessage("double_mark")).toBe("Считаны две марки сразу! Сканируйте по одной");
    expect(kizFormatMessage("length", 46)).toBe("Код длиной 46 не похож на марку (блок 35, короб 67)");
  });

  it("recognises only real rule ids so backend reasons keep their own messages", () => {
    expect(isKizFormatRule("prefix")).toBe(true);
    expect(isKizFormatRule("same_order_item_scan")).toBe(false);
    expect(isKizFormatRule("saved")).toBe(false);
  });
});
