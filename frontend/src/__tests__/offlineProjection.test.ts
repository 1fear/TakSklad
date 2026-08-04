import { describe, expect, it } from "vitest";

import { projectItemProgress } from "../features/warehouse/offline/projection";
import { scanEvent } from "./fixtures";

const item = { id: "item-1", quantity_blocks: 3, scanned_blocks: 1 };

describe("projectItemProgress", () => {
  it("без очереди возвращает серверный прогресс", () => {
    expect(projectItemProgress(item, [])).toEqual({ scannedBlocks: 1, pendingBlocks: 0, complete: false });
  });

  it("считает офлайн-сканы своей позиции", () => {
    const pending = [scanEvent({ orderItemId: "item-1", code: "0104006396053947217AAAAAA" })];
    expect(projectItemProgress(item, pending)).toEqual({ scannedBlocks: 2, pendingBlocks: 1, complete: false });
  });

  it("не считает сканы чужой позиции", () => {
    const pending = [scanEvent({ orderItemId: "item-2" })];
    expect(projectItemProgress(item, pending)).toEqual({ scannedBlocks: 1, pendingBlocks: 0, complete: false });
  });

  it("не считает события завершения заказа", () => {
    const pending = [{ ...scanEvent({ orderItemId: "item-1" }), type: "order_complete" as const, code: "" }];
    expect(projectItemProgress(item, pending)).toEqual({ scannedBlocks: 1, pendingBlocks: 0, complete: false });
  });

  it("закрывает позицию, когда очередь добивает план", () => {
    const pending = [
      scanEvent({ orderItemId: "item-1", code: "0104006396053947217AAAAAA" }),
      scanEvent({ orderItemId: "item-1", code: "0104006396053947217BBBBBB" }),
    ];
    expect(projectItemProgress(item, pending)).toEqual({ scannedBlocks: 3, pendingBlocks: 2, complete: true });
  });

  it("не превышает план, даже если очередь длиннее", () => {
    const pending = Array.from({ length: 5 }, (_, index) =>
      scanEvent({ orderItemId: "item-1", code: `010400639605394721${String(index).padStart(4, "0")}` }));
    expect(projectItemProgress(item, pending)).toEqual({ scannedBlocks: 3, pendingBlocks: 5, complete: true });
  });

  it("позиция без плана блоков не считается закрытой", () => {
    const openEnded = { id: "item-1", quantity_blocks: 0, scanned_blocks: 0 };
    const pending = [scanEvent({ orderItemId: "item-1" })];
    expect(projectItemProgress(openEnded, pending)).toEqual({ scannedBlocks: 1, pendingBlocks: 1, complete: false });
  });

  it("терпит отсутствующие числа в позиции", () => {
    const broken = { id: "item-1" } as { id: string; quantity_blocks: number; scanned_blocks: number };
    expect(projectItemProgress(broken, [])).toEqual({ scannedBlocks: 0, pendingBlocks: 0, complete: false });
  });
});
