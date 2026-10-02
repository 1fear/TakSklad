/**
 * Text, number and date helpers that answer exactly like the desktop program
 * (src/taksklad/utils.py, desktop_scan_rules.py). Python and JavaScript differ in
 * whitespace, float syntax, strptime and string ordering, so each helper below
 * replaces the obvious JS one-liner with the behaviour the desktop has
 */

// Python str.strip/split/\s use str.isspace: it has \x1c-\x1f and \x85 but not \ufeff, JS \s is the opposite
const PY_SPACE = "[\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]";
const SPACE_RUN = new RegExp(`${PY_SPACE}+`, "g");
const SPACE_EDGES = new RegExp(`^${PY_SPACE}+|${PY_SPACE}+$`, "g");

export function strip(text: string): string {
  return text.replace(SPACE_EDGES, "");
}

/** Python str.split() without arguments: runs of whitespace, no empty parts */
export function splitWords(text: string): string[] {
  return text.split(SPACE_RUN).filter(Boolean);
}

/** str(value or "").strip(): falsy values, including 0, become "" */
export function normalizeText(value: unknown): string {
  return value ? strip(String(value)) : "";
}

// Python float() syntax: digits may be separated by single underscores
const DIGITS = "\\d+(?:_\\d+)*";
const PY_FLOAT = new RegExp(`^[+-]?(?:${DIGITS}(?:\\.(?:${DIGITS})?)?|\\.${DIGITS})(?:[eE][+-]?${DIGITS})?$`);

/** int(float(text)) with spaces dropped and comma as the decimal mark; anything unparseable or infinite is 0 */
export function parseIntValue(value: unknown): number {
  const text =
    typeof value === "number" ? String(value) : normalizeText(value).replaceAll(" ", "").replaceAll(",", ".");
  if (!PY_FLOAT.test(text)) return 0;
  const number = Number(text.replaceAll("_", ""));
  // "|| 0" turns -0 into 0, Python has no negative zero for ints
  return Number.isFinite(number) ? Math.trunc(number) || 0 : 0;
}

export function normalizeLookupText(value: unknown): string {
  const text = normalizeText(value).toLowerCase().replaceAll("ё", "е").replaceAll("\ufeff", "").replace(/[*:]+/g, "");
  return strip(text.replace(SPACE_RUN, " "));
}

/** Python str.casefold(), enough for the ASCII words the product rules look for (ß becomes ss) */
export function casefold(text: string): string {
  return text.toUpperCase().toLowerCase();
}

/** Python compares strings by code point, JS by UTF-16 unit: they differ above U+FFFF, never use localeCompare */
export function compareText(a: string, b: string): number {
  const left = Array.from(a, (char) => char.codePointAt(0) ?? 0);
  const right = Array.from(b, (char) => char.codePointAt(0) ?? 0);
  const shared = Math.min(left.length, right.length);
  for (let i = 0; i < shared; i += 1) {
    if (left[i] !== right[i]) return left[i] - right[i];
  }
  return left.length - right.length;
}

/** Python tuple ordering over numbers and strings; numbers are compared, never subtracted, because Infinity - Infinity is NaN */
export function compareKeys(a: readonly (number | string)[], b: readonly (number | string)[]): number {
  for (let i = 0; i < a.length; i += 1) {
    const x = a[i];
    const y = b[i];
    if (typeof x === "number" && typeof y === "number") {
      if (x !== y) return x < y ? -1 : 1;
    } else {
      const order = compareText(String(x), String(y));
      if (order !== 0) return order;
    }
  }
  return 0;
}

// strptime directives with the exact ranges Python accepts: %d and %m take one or two digits, %Y four, %y two
const DIRECTIVES: Record<string, string> = {
  d: "(?<d>3[01]|[12]\\d|0[1-9]|[1-9])",
  m: "(?<m>1[0-2]|0[1-9]|[1-9])",
  Y: "(?<Y>\\d{4})",
  y: "(?<y>\\d\\d)",
};

function dateFormat(format: string): RegExp {
  const body = format.replaceAll(".", "\\.").replace(/%([dmYy])/g, (_directive, key: string) => DIRECTIVES[key]);
  return new RegExp(`^${body}$`);
}

const DAY_FIRST = dateFormat("%d.%m.%Y");
const ISO = dateFormat("%Y-%m-%d");
const SORT_FORMATS = [DAY_FIRST, ISO];
// the order is part of the answer: "05/10/2026" is the 5th of October because %d/%m/%Y comes before %m/%d/%Y
const STANDARD_FORMATS = [
  DAY_FIRST,
  ISO,
  dateFormat("%d/%m/%Y"),
  dateFormat("%m/%d/%Y"),
  dateFormat("%d.%m.%y"),
  dateFormat("%Y.%m.%d"),
];

const MS_PER_DAY = 86_400_000;

/** Days since 1970-01-01 of a real calendar date, null for 30 February or year 0 (Python raises ValueError) */
function dayNumber(year: number, month: number, day: number): number | null {
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day); // unlike Date.UTC, years 0-99 stay as given
  const real = year >= 1 && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
  return real ? date.getTime() / MS_PER_DAY : null;
}

type ParsedDate = { year: number; month: number; day: number; days: number };

function parseDate(text: string, formats: readonly RegExp[]): ParsedDate | null {
  for (const format of formats) {
    const groups = format.exec(text)?.groups;
    if (!groups) continue;
    const month = Number(groups.m);
    const day = Number(groups.d);
    // %y pivots like POSIX: 69-99 is 19xx, 00-68 is 20xx
    const year = groups.Y !== undefined ? Number(groups.Y) : Number(groups.y) + (Number(groups.y) <= 68 ? 2000 : 1900);
    const days = dayNumber(year, month, day);
    if (days !== null) return { year, month, day, days };
  }
  return null;
}

function formatDate({ year, month, day }: ParsedDate): string {
  return [String(day).padStart(2, "0"), String(month).padStart(2, "0"), String(year).padStart(4, "0")].join(".");
}

/** str(value) stripped, wrapping quotes removed, cut at the first space; null stays null */
export function cleanDateValue(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  const text = strip(String(value)).replace(/^['"]+|['"]+$/g, "");
  return text.includes(" ") ? (splitWords(text)[0] ?? "") : text;
}

/** DD.MM.YYYY for any of the six accepted formats; an unrecognised text comes back cleaned but unchanged */
export function parseDateToStandard(value: unknown): string | null {
  const cleaned = cleanDateValue(value);
  if (!cleaned) return null;
  const parsed = parseDate(cleaned, STANDARD_FORMATS);
  return parsed ? formatDate(parsed) : cleaned;
}

/** Sort key of a shipment date: day number, +Infinity when it is neither DD.MM.YYYY nor YYYY-MM-DD */
export function dateSortKey(value: unknown): number {
  return parseDate(normalizeText(value), SORT_FORMATS)?.days ?? Infinity;
}

const DAY_PREFIXES = new Map([
  [0, "Сегодня"],
  [1, "Завтра"],
  [-1, "Вчера"],
]);

export function formatOrderDateHeader(value: unknown, today: Date): string {
  const text = parseDateToStandard(value) || normalizeText(value) || "Без даты отгрузки";
  const parsed = parseDate(text, [DAY_FIRST]);
  if (!parsed) return text;
  const todayDays = dayNumber(today.getFullYear(), today.getMonth() + 1, today.getDate());
  const prefix = DAY_PREFIXES.get(parsed.days - (todayDays ?? Infinity));
  return prefix ? `${prefix}, ${formatDate(parsed)}` : formatDate(parsed);
}

/** 1234567 -> "1 234 567" (Python f"{n:,}" with the comma swapped for a plain space) */
export function groupThousands(amount: number): string {
  return String(amount).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

export function formatMoney(value: unknown): string {
  const amount = parseIntValue(value);
  return amount <= 0 ? "сумма не указана" : `${groupThousands(amount)} сум`;
}
