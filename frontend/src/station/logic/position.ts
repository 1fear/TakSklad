/**
 * What the operator sees for the current position and for the whole party
 * (src/taksklad/app_order_display.py load_current_product and update_party_summary_display,
 * desktop_scan_rules.py first_incomplete_order_index)
 */

import { scannedBlocks, type StationRow } from "./rows";
import {
  compareKeys,
  compareText,
  dateSortKey,
  formatMoney,
  formatOrderDateHeader,
  groupThousands,
  normalizeText,
  parseDateToStandard,
} from "./text";

// the desktop default catalog (config.py DEFAULT_PIECES_PER_BLOCK): the browser has no catalog of its own
export const PIECES_PER_BLOCK = 10;

export type ButtonState = "normal" | "disabled";

export type PositionView = {
  info: string;
  client: string;
  product: string;
  position: string;
  progress: string;
  lastCode: string;
  nextState: ButtonState;
  finishState: ButtonState;
};

/**
 * First position that still needs scanning (or has no valid plan); rows.length when all are done
 * Approved deviation: the desktop joins the codes into one text and splits it at every line break including GS,
 * so a KIZ with GS inside counts twice there; the station counts the code list the API gave, as positionView does
 */
export function firstIncompleteIndex(rows: readonly StationRow[]): number {
  const index = rows.findIndex((row) => row.planBlocks <= 0 || scannedBlocks(row, row.existingCodes) < row.planBlocks);
  return index === -1 ? rows.length : index;
}

/** null when index is past the last row, as the desktop leaves the screen untouched then */
export function positionView(rows: readonly StationRow[], index: number): PositionView | null {
  const row = rows[index];
  if (!row) return null;

  const plan = row.planBlocks;
  const scanned = scannedBlocks(row, row.existingCodes);
  const lineTotal = row.lineTotal ? `${groupThousands(row.lineTotal)} сум` : "не указана";
  const info = [
    `№ SkladBot: ${row.requestNumber}`,
    `📅 Дата отгрузки: ${row.orderDate || "не указана"}`,
    `👤 Торг.пред: ${row.representative}`,
    `📍 Адрес: ${row.address}`,
    `💳 Тип оплаты: ${row.paymentType}`,
    `💰 Сумма: ${lineTotal}`,
    `📦 План: ${plan} блоков (1 блок = ${PIECES_PER_BLOCK} ШТ)`,
  ].join("\n");

  const done = plan > 0 && scanned >= plan;
  const isLast = index >= rows.length - 1;
  return {
    info,
    client: `🏢 ${normalizeText(row.client) || "Юр.лицо не указано"}`,
    product: `📦 ${normalizeText(row.product) || "SKU не указан"}`,
    position: `Позиция ${index + 1} из ${rows.length}`,
    progress: `${scanned} / ${plan}`,
    lastCode: row.existingCodes.length ? `Уже записано: ${scanned} блоков, ${row.existingCodes.length} кодов` : "",
    nextState: done && !isLast ? "normal" : "disabled",
    finishState: done && isLast ? "normal" : "disabled",
  };
}

function unique(values: Iterable<string>): string[] {
  return [...new Set(values)];
}

export function partySummaryText(rows: readonly StationRow[], today: Date): string {
  if (!rows.length) return "Партия не выбрана";

  const blocks = rows.reduce((sum, row) => sum + row.planBlocks, 0);
  const total = rows.reduce((sum, row) => sum + row.lineTotal, 0);
  const requests = unique(rows.map((row) => normalizeText(row.requestNumber)).filter(Boolean)).sort(compareText);
  // the desktop sorts a set here, so dates the sort key cannot read (all +Infinity) come out in no fixed order there
  const dates = unique(
    rows.filter((row) => normalizeText(row.orderDate)).map((row) => parseDateToStandard(row.orderDate) || normalizeText(row.orderDate)),
  ).sort((a, b) => compareKeys([dateSortKey(a)], [dateSortKey(b)]));

  const shownRequests = requests.length
    ? requests.slice(0, 2).join(", ") + (requests.length > 2 ? ` +${requests.length - 2}` : "")
    : "без номера SkladBot";
  const shownDates = dates.length ? dates.map((date) => formatOrderDateHeader(date, today)).join(", ") : "дата не указана";
  return `Партия: ${rows.length} поз. · ${blocks} блок. · ${formatMoney(total)}\nДата отгрузки: ${shownDates} · Заявка: ${shownRequests}`;
}
