/**
 * Colours of the desktop program, so the station looks the same to the warehouse
 * The first block is src/taksklad/config.py, the second is the main window (app_layout.py, order_list_widgets.py);
 * the corpus (tools/generate_station_parity_corpus.py) records both and tokens.test.ts requires equality
 */

/** ui_widgets.fade_hex: the colour moved `amount` of the way to white, the hover colour of a button (default 0.10) */
export function fadeHex(color: string, amount = 0.1): string {
  const hex = color.replace(/^#+/, "");
  // the desktop also reads exotic forms like "+f0000"; colours here are always #rrggbb
  if (!/^[0-9a-fA-F]{6}$/.test(hex)) return `#${hex}`;
  const share = Math.max(0, Math.min(1, amount));
  const channel = (start: number) => {
    const value = parseInt(hex.slice(start, start + 2), 16);
    return Math.min(255, Math.floor(value + (255 - value) * share));
  };
  return `#${[0, 2, 4].map((start) => channel(start).toString(16).padStart(2, "0")).join("")}`;
}

export const BG_MAIN = "#f4f1e8";
export const BG_CARD = "#fffdf7";
export const FG_TEXT = "#2e2c28";
export const FG_MUTED = "#777066";
export const ACCENT = "#b28224";
export const SUCCESS = "#2f8a4a";
export const INFO = "#3a6b8f";
export const WARNING = "#d8b64c";
export const DANGER = "#b7483c";
export const ERROR_BG = "#f8ded9";
export const ERROR_FG = "#b7483c";
export const BORDER = "#d8d0bf";
export const DISABLED_BG = "#e0d7b9";
export const DISABLED_FG = "#8f8878";

export const LIST_SURFACE_BG = "#fffaf2";
export const SELECTED_CARD_BG = "#fff6df";
export const PLACEHOLDER_FG = "#a7a095";
export const PRODUCT_PHOTO_BG = "#fffaf0";
export const PRODUCT_PHOTO_SHELL_BG = "#f3ead8";
export const GTIN_BADGE_FG = "#fff7df";
export const SCROLLBAR_BG = "#e5dcc8";
export const SCROLLBAR_THUMB = "#c4ad7a";
export const SCROLLBAR_THUMB_HOVER = fadeHex(SCROLLBAR_THUMB, 0.12);
