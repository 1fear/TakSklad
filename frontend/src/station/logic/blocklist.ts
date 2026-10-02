/**
 * Codes the warehouse must never ship (src/taksklad/kiz_blocklist.py)
 * Only SHA-256 digests are stored: the browser bundle is public, so the codes themselves must not sit in it
 */

import { normalizeKizCode } from "../../features/warehouse/kizFormat";

export const BLOCK_REASON = "Код маркировки заблокирован. Отгрузка запрещена";

// sha256 of the code with spaces, tabs and line breaks trimmed (the desktop normalization), hex
export const BLOCKED_KIZ_SHA256: ReadonlySet<string> = new Set([
  "cd7dc6aea0b6045ec64ed14b515fab810d50343f97baa36bf4f5d32d5ccb7c0d",
  "ffcdb881f32655b466c89451acdccc09e8c001fa100fd9d37bf557024c1d755c",
]);

export async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** The refusal text for a blocked code, "" for any other; the digests are a parameter so a test can use its own */
export async function blockedKizReason(code: string, blocked: ReadonlySet<string> = BLOCKED_KIZ_SHA256): Promise<string> {
  const normalized = normalizeKizCode(code);
  if (!normalized) return "";
  return blocked.has(await sha256Hex(normalized)) ? BLOCK_REASON : "";
}
