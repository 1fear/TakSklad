/** The colours of the station equal the desktop palette and the main window colours (corpus.palette) */

import { describe, expect, it } from "vitest";

import * as tokens from "../tokens";
import { corpus, labelled } from "./support";

const { desktop, window, fade } = corpus.palette;

describe("tokens", () => {
  it("are exactly the desktop palette and the window colours", () => {
    const colours = Object.fromEntries(Object.entries(tokens).filter(([, value]) => typeof value === "string"));
    expect(colours).toEqual({ ...desktop, ...window });
  });

  it.each(labelled(fade, (item) => `${item.color} by ${item.amount}`))("fadeHex of %s", (_name, item) => {
    expect(tokens.fadeHex(item.color, item.amount)).toBe(item.result);
    // the hover colour of a button takes the default amount
    if (item.amount === 0.1) expect(tokens.fadeHex(item.color)).toBe(item.result);
  });
});
