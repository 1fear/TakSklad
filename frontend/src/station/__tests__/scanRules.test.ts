/**
 * evaluateScan, findCodeOwner and the duplicate message against what the desktop answered (corpus.scan,
 * corpus.owners, corpus.duplicate_messages); every case is the desktop on_scan run on the stated screen state
 * Two approved deviations: the SKU mismatch message names the site, and a code with GS inside is found by the station
 */

import { describe, expect, it, vi } from "vitest";

import { evaluateScan, findCodeOwner, formatDuplicateScanMessage, type ScanDeps, type ScanInput } from "../logic/scanRules";
import { corpus, labelled, rowFromSpec, type RowSpec } from "./support";

const VERSION = "2.0.56";

type ScanCase = {
  name: string;
  input: {
    raw: string;
    rows: RowSpec[];
    scanned: string[];
    booked: string[];
    owner_rows: RowSpec[];
    completed: string[];
    availability: null | "error" | { available: boolean; latest_movement_type?: string };
    blocked: boolean;
    update_required: boolean;
  };
  output: {
    state: string;
    message: string;
    scanned_codes_after: string[];
    queued: number;
    progress_text: string;
    last_code_text: string;
    status_text: string;
    next_enabled: string;
    finish_enabled: string;
    bell: number;
    release_prompt: number;
  };
};

const cases = corpus.scan as unknown as ScanCase[];

function scanInput(input: ScanCase["input"], extra: Partial<ScanInput> = {}): ScanInput {
  return {
    raw: input.raw,
    version: VERSION,
    updateRequired: input.update_required,
    busy: false,
    rows: input.rows.map(rowFromSpec),
    index: 0,
    scannedCodes: input.scanned,
    existingCodes: new Set(input.booked),
    completedCodes: new Set(input.completed),
    ownerRows: input.owner_rows.map(rowFromSpec),
    ...extra,
  };
}

function scanDeps(input: ScanCase["input"], extra: Partial<ScanDeps> = {}): ScanDeps {
  return {
    blockReason: async () => (input.blocked ? corpus.blocklist.reason : ""),
    availability: async () => {
      if (input.availability === null || input.availability === "error") throw new Error("backend unavailable");
      return input.availability;
    },
    ...extra,
  };
}

describe("scan answers", () => {
  it.each(labelled(cases, (item) => item.name))("%s", async (name, { input, output }) => {
    const result = await evaluateScan(scanInput(input), scanDeps(input));

    const lines = result.message.split("\n");
    if (output.message.startsWith("КИЗ не соответствует товару")) {
      // approved deviation: the last two lines name the site, not the desktop program
      expect(lines.slice(0, 5)).toEqual(output.message.split("\n").slice(0, 5));
      expect(lines.slice(5)).toEqual([`Версия сайта: ${VERSION}`, "Если SKU на блоке верный, обновите страницу (F5)."]);
    } else {
      expect(result.message).toBe(output.message);
    }
    expect({
      state: result.state,
      scannedCodesAfter: result.scannedCodesAfter,
      queued: result.queued,
      progressText: result.progressText,
      lastCodeText: result.lastCodeText,
      statusText: result.statusText,
      nextState: result.nextState,
      finishState: result.finishState,
      bell: result.bell,
      releasePrompt: result.releasePrompt,
    }).toEqual({
      state: output.state,
      scannedCodesAfter: output.scanned_codes_after,
      queued: output.queued > 0,
      progressText: output.progress_text,
      lastCodeText: output.last_code_text,
      statusText: output.status_text,
      nextState: output.next_enabled,
      finishState: output.finish_enabled,
      bell: output.bell > 0,
      releasePrompt: output.release_prompt > 0,
    });
  });
});

describe("what only the page does", () => {
  const byName = (name: string) => cases.find((item) => item.name === name)?.input as ScanCase["input"];

  // the desktop shows a busy dialog while an operation runs; the page has no such operation, only this answer
  it("answers busy without a bell or a refusal", async () => {
    const input = byName("accept_unit");
    const outcome = await evaluateScan(scanInput(input, { busy: true }), scanDeps(input));
    expect(outcome).toMatchObject({ state: "busy", message: "Дождитесь завершения текущей операции", bell: false, queued: false });
  });

  it("asks the backend about this code and this position", async () => {
    const input = byName("duplicate_other_order_backend_refuses");
    const availability = vi.fn(async () => ({ available: false }));
    await evaluateScan(scanInput(input), scanDeps(input, { availability }));
    expect(availability).toHaveBeenCalledExactlyOnceWith(input.raw, input.rows[0].item);
  });

  it("leaves the given screen state untouched", async () => {
    const input = byName("accept_unit");
    const scannedCodes = ["a"];
    const outcome = await evaluateScan(scanInput(input, { scannedCodes }), scanDeps(input));
    expect(scannedCodes).toEqual(["a"]);
    expect(outcome.scannedCodesAfter).toEqual(["a", input.raw]);
    expect(outcome.scannedCodesAfter).not.toBe(scannedCodes);
  });
});

describe("code owner", () => {
  type Held = { client: string; date: string; product: string; request: string };

  it.each(labelled(corpus.owners as unknown as { name: string; input: { code: string; rows: RowSpec[] }; output: Held | null }[], (item) => item.name))(
    "%s",
    (_name, { input, output }) => {
      expect(findCodeOwner(input.code, input.rows.map(rowFromSpec))).toEqual(
        output && { client: output.client, orderDate: output.date, product: output.product, requestNumber: output.request },
      );
    },
  );
});

describe("duplicate message", () => {
  it.each(labelled(corpus.duplicate_messages, (item) => JSON.stringify([item.owner, item.status])))("%s", (_name, item) => {
    const owner = item.owner as { client?: string; order_date_display?: string; product?: string; skladbot_request_number?: string };
    const status = item.status as { checked?: boolean; available?: boolean; latest_movement_type?: string; reason?: string };
    expect(
      formatDuplicateScanMessage(
        item.code,
        owner.client === undefined
          ? null
          : {
              client: owner.client,
              orderDate: owner.order_date_display ?? "",
              product: owner.product ?? "",
              requestNumber: owner.skladbot_request_number ?? "",
            },
        status.checked === undefined
          ? null
          : {
              checked: status.checked,
              available: Boolean(status.available),
              latestMovementType: status.latest_movement_type ?? "",
              reason: status.reason ?? "",
            },
      ),
    ).toBe(item.text);
  });
});
