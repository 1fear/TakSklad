/**
 * The position screen, the party summary and the first position to scan against the desktop
 * (corpus.position, corpus.first_incomplete)
 */

import { describe, expect, it } from "vitest";

import { firstIncompleteIndex, partySummaryText, positionView } from "../logic/position";
import { corpus, labelled, rowFromSpec, TODAY } from "./support";

describe("position screen and party summary", () => {
  it.each(labelled(corpus.position, (item) => item.name))("%s", (_name, { input, output }) => {
    const rows = input.rows.map(rowFromSpec);
    const expected = {
      info: output.info,
      client: output.client,
      product: output.product,
      position: output.position,
      progress: output.progress,
      lastCode: output.last_code,
      nextState: output.next_state,
      finishState: output.finish_state,
    };
    const view = positionView(rows, input.index);
    if (view === null) {
      // past the last position the desktop leaves the screen as it was, here untouched labels
      expect(Object.values(expected).every((text) => text === "")).toBe(true);
    } else {
      expect(view).toEqual(expected);
    }
    expect(partySummaryText(rows, TODAY)).toBe(output.party);
  });
});

describe("first incomplete position", () => {
  it.each(labelled(corpus.first_incomplete, (item) => item.name))("%s", (_name, { input, output }) => {
    expect(firstIncompleteIndex(input.rows.map(rowFromSpec))).toBe(output.index);
  });
});
