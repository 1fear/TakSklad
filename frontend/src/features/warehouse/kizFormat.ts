/**
 * KIZ code format rules for the browser operator surface.
 *
 * Exact mirror of the Windows desktop guard `validate_kiz_code`
 * (src/taksklad/utils.py) so that retiring the desktop client does not remove a
 * check the warehouse has relied on. Operator-facing wording is kept identical
 * to the desktop messages on purpose — the same text the operators already know.
 *
 * `tests/test_kiz_format_contract.py` pins this file against the desktop rules.
 */

export const KIZ_MIN_LENGTH = 20;
export const KIZ_MAX_LENGTH = 120;
export const KIZ_UNIT_LENGTH = 35;
export const KIZ_BOX_LENGTH = 67;

export type KizFormatRule =
  | "empty"
  | "prefix"
  | "too_short"
  | "too_long"
  | "cyrillic"
  | "whitespace"
  | "charset"
  | "head"
  | "double_mark"
  | "length";

const CYRILLIC = /[а-яА-ЯёЁ]/;
const WHITESPACE = /[ \t\r\n\v\f]/;
// Printable ASCII plus the GS separator (\x1d) that real DataMatrix KIZ codes carry.
// The control character is deliberate and must match the desktop rule exactly.
// eslint-disable-next-line no-control-regex
const ALLOWED_CHARSET = /^[\x1d\x21-\x7E]+$/;
// AI 01 with a 14-digit GTIN. What follows is usually AI 21 (serial), but a box
// may carry AI 10 (batch) first, so only the GTIN is required here.
const GTIN_HEAD = /^01\d{14}/;
// A full serial head, used to spot two marks glued into one code by the scanner.
const MARK_HEAD_GLOBAL = /01\d{14}21/g;
const KNOWN_LENGTHS: number[] = [KIZ_UNIT_LENGTH, KIZ_BOX_LENGTH];

const RULE_MESSAGES: Record<KizFormatRule, string> = {
  empty: "Код пустой",
  prefix: "КИЗ должен начинаться с 01",
  too_short: `Код слишком короткий для КИЗа (минимум ${KIZ_MIN_LENGTH} символов)`,
  too_long: `Код слишком длинный для КИЗа (максимум ${KIZ_MAX_LENGTH} символов)`,
  cyrillic: "Код содержит русские буквы! Используйте только латиницу",
  whitespace: "Код содержит пробелы или переносы",
  charset: "Код содержит недопустимые символы",
  head: "Код не похож на марку: после 01 ожидается GTIN из 14 цифр",
  double_mark: "Считаны две марки сразу! Сканируйте по одной",
  length: `Код не похож на марку (блок ${KIZ_UNIT_LENGTH}, короб ${KIZ_BOX_LENGTH})`,
};

/** Desktop-identical normalization: trim only spaces, tabs and line breaks. */
export function normalizeKizCode(raw: string): string {
  return String(raw ?? "").replace(/^[ \t\r\n]+|[ \t\r\n]+$/g, "");
}

/** First violated rule for a code, or "" when the code is well formed. */
export function kizFormatViolation(raw: string): KizFormatRule | "" {
  const code = normalizeKizCode(raw);
  if (!code) return "empty";
  if (!code.startsWith("01")) return "prefix";
  if (code.length < KIZ_MIN_LENGTH) return "too_short";
  if (code.length > KIZ_MAX_LENGTH) return "too_long";
  if (CYRILLIC.test(code)) return "cyrillic";
  if (WHITESPACE.test(code)) return "whitespace";
  if (!ALLOWED_CHARSET.test(code)) return "charset";
  if (!GTIN_HEAD.test(code)) return "head";
  if (KNOWN_LENGTHS.includes(code.length)) return "";
  // Only codes that already failed the length check are searched for a second
  // mark: a box tail is digits, so the head pattern can appear inside it by chance.
  if ((code.match(MARK_HEAD_GLOBAL) ?? []).length > 1) return "double_mark";
  return "length";
}

export function kizFormatMessage(rule: KizFormatRule, length = 0): string {
  if (rule === "length" && length > 0) {
    return `Код длиной ${length} не похож на марку (блок ${KIZ_UNIT_LENGTH}, короб ${KIZ_BOX_LENGTH})`;
  }
  return RULE_MESSAGES[rule];
}

export const KIZ_FORMAT_RULES = Object.keys(RULE_MESSAGES) as KizFormatRule[];

export function isKizFormatRule(value: string): value is KizFormatRule {
  return (KIZ_FORMAT_RULES as string[]).includes(value);
}
