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

  it("агрегатный короб считается как 50 блоков, а не как один скан", () => {
    const bigItem = { id: "item-1", quantity_blocks: 100, scanned_blocks: 0 };
    const pending = [scanEvent({ orderItemId: "item-1", code: "0104006396054012217ABCDEF" })];
    expect(projectItemProgress(bigItem, pending)).toEqual({
      scannedBlocks: 50,
      pendingBlocks: 50,
      complete: false,
    });
  });

  it("две коробки закрывают позицию на сто блоков", () => {
    const bigItem = { id: "item-1", quantity_blocks: 100, scanned_blocks: 0 };
    const pending = [
      scanEvent({ orderItemId: "item-1", code: "0104006396054012217AAAAAA" }),
      scanEvent({ orderItemId: "item-1", code: "0104006396053985217BBBBBB" }),
    ];
    expect(projectItemProgress(bigItem, pending)).toEqual({
      scannedBlocks: 100,
      pendingBlocks: 100,
      complete: true,
    });
  });

  it("не считает второй раз код, который сервер уже подтвердил", () => {
    const confirmed = { id: "item-1", quantity_blocks: 2, scanned_blocks: 1, scan_codes: ["0104006396053947217ABCDEF"] };
    const pending = [scanEvent({ orderItemId: "item-1", code: "0104006396053947217ABCDEF" })];
    expect(projectItemProgress(confirmed, pending)).toEqual({
      scannedBlocks: 1,
      pendingBlocks: 0,
      complete: false,
    });
  });

  it("подтверждённый код исключается по обрезанному значению", () => {
    const confirmed = { id: "item-1", quantity_blocks: 2, scanned_blocks: 1, scan_codes: ["0104006396053947217ABCDEF"] };
    const pending = [scanEvent({ orderItemId: "item-1", code: "  0104006396053947217ABCDEF  " })];
    expect(projectItemProgress(confirmed, pending).pendingBlocks).toBe(0);
  });

  it("другой код той же позиции считается как обычно", () => {
    const confirmed = { id: "item-1", quantity_blocks: 3, scanned_blocks: 1, scan_codes: ["0104006396053947217ABCDEF"] };
    const pending = [scanEvent({ orderItemId: "item-1", code: "0104006396053947217BBBBBB" })];
    expect(projectItemProgress(confirmed, pending)).toEqual({
      scannedBlocks: 2,
      pendingBlocks: 1,
      complete: false,
    });
  });

  it("короб и штучный код складываются по своим весам", () => {
    const bigItem = { id: "item-1", quantity_blocks: 100, scanned_blocks: 0 };
    const pending = [
      scanEvent({ orderItemId: "item-1", code: "0104006396054012217AAAAAA" }),
      scanEvent({ orderItemId: "item-1", code: "0104006396053947217BBBBBB" }),
    ];
    expect(projectItemProgress(bigItem, pending)).toMatchObject({ scannedBlocks: 51, pendingBlocks: 51 });
  });
});
