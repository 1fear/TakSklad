/** The order list against build_order_list_model of the desktop (corpus.order_list): grouping, search, order of dates and cards */

import { describe, expect, it } from "vitest";

import { buildOrderListModel } from "../logic/orderList";
import { corpus, labelled, rowFromSpec, TODAY } from "./support";

describe("order list", () => {
  it.each(labelled(corpus.order_list, (item) => item.name))("%s", (_name, { input, output }) => {
    const model = buildOrderListModel(input.rows.map(rowFromSpec), input.search, TODAY);
    expect(model.rows).toEqual(
      output.rows.map((item) =>
        item.kind === "date"
          ? { kind: "date", title: item.title }
          : { kind: "order", client: item.client, meta: item.meta, summary: item.summary, groupKey: item.group_key },
      ),
    );
    expect(model.subtitle).toBe(output.subtitle);
    expect(model.counter).toBe(output.counter);
    // the two counts behind the texts: cards shown, and "из N" at the end of the counter
    expect(model.visibleCards).toBe(output.rows.filter((item) => item.kind === "order").length);
    expect(`Показаны ${model.visibleCards} из ${model.totalGroups}`).toBe(output.counter);
  });
});
