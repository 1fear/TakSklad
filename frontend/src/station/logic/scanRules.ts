/**
 * The answer to one scanned code, step by step as the desktop gives it
 * (src/taksklad/app_scanning.py on_scan). The function is pure: it reads the screen state it
 * is given, asks the two services it is given, and returns what the screen must show
 */

import { kizFormatMessage, kizFormatViolation, normalizeKizCode } from "../../features/warehouse/kizFormat";
import { blockQuantityForCode, scanTypeForCode, SCAN_TYPE_AGGREGATE_BOX } from "../../features/warehouse/scanQuantities";
import type { ButtonState } from "./position";
import {
  aggregateProductMismatch,
  formatProductKeyLabel,
  productKeyFromName,
  scanCodeProductKey,
  scanProductMismatch,
} from "./productKeys";
import { scannedBlocks, type StationRow } from "./rows";
import { normalizeText } from "./text";

export type CodeOwner = { client: string; orderDate: string; product: string; requestNumber: string };

/** What the backend said about reusing a code that is already booked elsewhere */
export type ReuseStatus = { checked: boolean; available: boolean; latestMovementType: string; reason: string };

export type ScanDeps = {
  /** The raw backend answer for a code against an order item; rejects when the backend cannot be asked */
  availability: (code: string, orderItemId: string) => Promise<{ available: boolean; latest_movement_type?: string }>;
  /** The refusal text of a blocked code, "" for any other */
  blockReason: (code: string) => Promise<string>;
};

export type ScanInput = {
  raw: string;
  /** The site version shown in the SKU mismatch message */
  version: string;
  updateRequired: boolean;
  busy: boolean;
  rows: readonly StationRow[];
  index: number;
  scannedCodes: readonly string[];
  /** Codes already booked to any order today */
  existingCodes: ReadonlySet<string>;
  /** Codes of the orders finished in this session */
  completedCodes: ReadonlySet<string>;
  /** Rows searched for the owner of a duplicate code */
  ownerRows: readonly StationRow[];
};

export type ScanOutcome = {
  state: "accepted" | "rejected" | "ignored" | "busy";
  message: string;
  scannedCodesAfter: string[];
  /** True when the code must go to the backend queue */
  queued: boolean;
  progressText: string;
  lastCodeText: string;
  statusText: string;
  /** "" leaves the button as it is */
  nextState: ButtonState | "";
  finishState: ButtonState | "";
  bell: boolean;
  /** True when the operator is offered to release the code */
  releasePrompt: boolean;
};

// movements after which the backend lets a code be scanned again (backend_flow.py REUSABLE_KIZ_MOVEMENTS)
const REUSABLE_MOVEMENTS = new Set(["return", "undo", "reset"]);
// the desktop appends the running operation and its seconds, the page has no such state
const BUSY_MESSAGE = "Дождитесь завершения текущей операции";

/** Approved deviation: the desktop searches the split code text and misses a code with GS inside, the station searches the list */
export function findCodeOwner(code: string, rows: readonly StationRow[]): CodeOwner | null {
  const target = normalizeKizCode(code);
  const row = target ? rows.find((candidate) => candidate.existingCodes.some((held) => normalizeKizCode(held) === target)) : undefined;
  return row ? { client: row.client, orderDate: row.orderDate, product: row.product, requestNumber: row.requestNumber } : null;
}

function backendLine(status: ReuseStatus): string {
  const reason = normalizeText(status.reason);
  const movement = normalizeText(status.latestMovementType);
  if (!status.checked) return reason ? `Backend-проверка повтора недоступна: ${reason}` : "";
  if (status.available) return `Backend разрешил повтор: ${reason || movement || "КИЗ возвращён"}`;
  if (movement) return `Backend не разрешил повтор. Последнее движение: ${movement}`;
  return reason ? `Backend не разрешил повтор: ${reason}` : "";
}

export function formatDuplicateScanMessage(code: string, owner: CodeOwner | null, status: ReuseStatus | null): string {
  const normalized = normalizeKizCode(code);
  const owned: [string, string][] = owner
    ? [["Заказ", owner.client], ["Дата отгрузки", owner.orderDate], ["Товар", owner.product], ["SkladBot", owner.requestNumber]]
    : [];
  return [
    "КИЗ уже отсканирован в другом заказе.",
    owner ? "" : "Владелец в локальном списке не найден.",
    ...owned.map(([label, value]) => (normalizeText(value) ? `${label}: ${normalizeText(value)}` : "")),
    status ? backendLine(status) : "",
    normalized ? `Код: ${normalized}` : "",
    "Сканируйте другой КИЗ.",
  ]
    .filter(Boolean)
    .join("\n");
}

/** Lines 6 and 7 deliberately differ from the desktop (version of the site, F5 instead of restarting the program) */
export function formatScanProductMismatchMessage(code: string, product: string, version: string): string {
  const normalized = normalizeKizCode(code);
  return [
    "КИЗ не соответствует товару текущей позиции.",
    `Позиция: ${normalizeText(product) || "товар не указан"}`,
    `Ожидалось: ${formatProductKeyLabel(productKeyFromName(product))}`,
    `КИЗ распознан как: ${formatProductKeyLabel(scanCodeProductKey(normalized))}`,
    `Префикс КИЗа: ${normalized.slice(0, 18)}${normalized.length > 18 ? "..." : ""}`,
    `Версия сайта: ${version}`,
    "Если SKU на блоке верный, обновите страницу (F5).",
  ].join("\n");
}

/** The refusal text for a code that cannot go into this position, "" when it can */
function positionProblem(row: StationRow, code: string, scannedCodes: readonly string[], version: string): string {
  const plan = row.planBlocks;
  if (plan <= 0) return "В заказе не указано корректное 'Кол-во блок'";
  const before = scannedBlocks(row, scannedCodes);
  if (before >= plan) return `План выполнен! Нельзя сканировать больше ${plan} блоков`;
  if (scanProductMismatch(code, row.product)) return formatScanProductMismatchMessage(code, row.product, version);
  if (scanTypeForCode(code) === SCAN_TYPE_AGGREGATE_BOX) {
    if (aggregateProductMismatch(code, row.product)) return "Код короба не соответствует товару текущей позиции";
    const remaining = plan - before;
    const blocks = blockQuantityForCode(code);
    if (blocks > remaining) return `Короб +${blocks} блоков превышает остаток позиции: осталось ${remaining}`;
  }
  return scannedCodes.includes(code) ? "Код уже отсканирован в этой позиции" : "";
}

/** Backend verdict on reusing a code (backend_flow.py backend_duplicate_scan_reuse_status) */
async function reuseStatus(row: StationRow, code: string, deps: ScanDeps): Promise<ReuseStatus> {
  const itemId = normalizeText(row.backendOrderItemId);
  if (!itemId) {
    return { checked: false, available: false, latestMovementType: "", reason: "backend path is unavailable for this position" };
  }
  try {
    const answer = await deps.availability(code, itemId);
    const movement = normalizeText(answer.latest_movement_type).toLowerCase();
    return {
      checked: true,
      available: Boolean(answer.available) && REUSABLE_MOVEMENTS.has(movement),
      latestMovementType: movement,
      reason: movement ? `latest movement is ${movement}` : "",
    };
  } catch {
    return { checked: false, available: false, latestMovementType: "", reason: "backend availability check failed" };
  }
}

/** The refusal text for a code booked elsewhere that the backend does not release, "" otherwise */
async function duplicateProblem(input: ScanInput, row: StationRow, code: string, deps: ScanDeps): Promise<string> {
  const booked = input.existingCodes.has(code);
  if (!booked && !input.completedCodes.has(code)) return "";
  const status = await reuseStatus(row, code, deps);
  if (status.available) return "";
  return booked
    ? formatDuplicateScanMessage(code, findCodeOwner(code, input.ownerRows), status)
    : "Код уже использован в другом задании сегодня";
}

function accepted(input: ScanInput, row: StationRow, code: string): ScanOutcome {
  const scannedCodesAfter = [...input.scannedCodes, code];
  const scanned = scannedBlocks(row, scannedCodesAfter);
  const plan = row.planBlocks;
  const blocks = blockQuantityForCode(code);
  const isBox = scanTypeForCode(code) === SCAN_TYPE_AGGREGATE_BOX;
  const message = isBox ? `Отсканирован короб +${blocks} (${scanned}/${plan})` : `Отсканирован код (${scanned}/${plan})`;
  const shown = code.slice(0, 40);
  const outcome: ScanOutcome = {
    state: "accepted",
    message,
    scannedCodesAfter,
    queued: true,
    progressText: `${scanned} / ${plan}`,
    lastCodeText: isBox ? `Последний код: короб +${blocks}: ${shown}...` : `Последний код: ${shown}...`,
    statusText: `✅ ${message}`,
    nextState: "",
    finishState: "",
    bell: false,
    releasePrompt: false,
  };
  if (scanned < plan) return outcome;
  if (input.index >= input.rows.length - 1) {
    return { ...outcome, statusText: "🎯 Заказ выполнен! Нажмите 'ЗАВЕРШИТЬ ЗАКАЗ'", nextState: "disabled", finishState: "normal" };
  }
  return { ...outcome, statusText: "🎯 Позиция выполнена! Нажмите 'Следующая позиция'", nextState: "normal", finishState: "disabled" };
}

export async function evaluateScan(input: ScanInput, deps: ScanDeps): Promise<ScanOutcome> {
  const nothing: ScanOutcome = {
    state: "ignored",
    message: "",
    scannedCodesAfter: [...input.scannedCodes],
    queued: false,
    progressText: "",
    lastCodeText: "",
    statusText: "",
    nextState: "",
    finishState: "",
    bell: false,
    releasePrompt: false,
  };
  const rejected = (message: string, releasePrompt = false): ScanOutcome => ({
    ...nothing,
    state: "rejected",
    message,
    bell: true,
    releasePrompt,
  });

  if (input.updateRequired) return rejected("Требуется обновить приложение перед сканированием");
  if (input.busy) return { ...nothing, state: "busy", message: BUSY_MESSAGE };
  const row = input.rows[input.index];
  if (!row) return rejected("Сначала выберите заказ");

  const code = normalizeKizCode(input.raw);
  if (!code) return nothing;
  const rule = kizFormatViolation(code);
  if (rule) return rejected(kizFormatMessage(rule, code.length));
  const blockReason = await deps.blockReason(code);
  if (blockReason) return rejected(`🚫 ${blockReason}`);

  const problem = positionProblem(row, code, input.scannedCodes, input.version);
  if (problem) return rejected(problem);
  const duplicate = await duplicateProblem(input, row, code, deps);
  if (duplicate) return rejected(duplicate, true);
  if (!normalizeText(row.backendOrderItemId)) return rejected("Позиция не связана с backend. Сканирование заблокировано");
  return accepted(input, row, code);
}
