/** The blocklist keeps only digests; they must be those of the desktop list (corpus.blocklist), the codes are not in the corpus */

import { describe, expect, it } from "vitest";

import { BLOCK_REASON, BLOCKED_KIZ_SHA256, blockedKizReason, sha256Hex } from "../logic/blocklist";
import { corpus } from "./support";

const { reason, sha256, sample } = corpus.blocklist;

describe("blocklist", () => {
  it("holds exactly the digests of the desktop list", () => expect([...BLOCKED_KIZ_SHA256].sort()).toEqual(sha256));

  it("keeps the desktop refusal text", () => expect(BLOCK_REASON).toBe(reason));

  it("hashes like hashlib", async () => expect(await sha256Hex(sample.text)).toBe(sample.sha256));

  it("refuses a code whose digest is listed, trimmed or not, and only that code", async () => {
    const listed = new Set([sample.sha256]);
    expect(await blockedKizReason(sample.text, listed)).toBe(reason);
    expect(await blockedKizReason(` ${sample.text}\r\n`, listed)).toBe(reason);
    expect(await blockedKizReason(`${sample.text}X`, listed)).toBe("");
    expect(await blockedKizReason(" \t\r\n", listed)).toBe("");
  });

  it("lets an ordinary code through", async () => expect(await blockedKizReason(sample.text)).toBe(""));
});
