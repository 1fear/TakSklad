import { describe, expect, it } from "vitest";

import { offlineEventKey, type OfflineEvent } from "../features/warehouse/offline/queueTypes";

export function scanEvent(overrides: Partial<OfflineEvent> = {}): OfflineEvent {
  return {
    type: "scan",
    orderId: "order-1",
    orderItemId: "item-1",
    code: "0104006396053947217ABCDEF",
    actor: "operator-1",
    workstationId: "web",
    scannedAt: "2026-08-04T10:00:00+05:00",
    createdAt: "2026-08-04T10:00:00+05:00",
    attempts: 0,
    lastError: "",
    ...overrides,
  };
}

describe("offlineEventKey", () => {
  it("схлопывает повторный скан того же кода в ту же позицию", () => {
    const first = offlineEventKey(scanEvent());
    const second = offlineEventKey(scanEvent({ scannedAt: "2026-08-04T10:05:00+05:00", attempts: 3 }));
    expect(second).toBe(first);
  });

  it("различает тот же код в другой позиции", () => {
    expect(offlineEventKey(scanEvent({ orderItemId: "item-2" }))).not.toBe(offlineEventKey(scanEvent()));
  });

  it("различает разные коды в одной позиции", () => {
    expect(offlineEventKey(scanEvent({ code: "0104006396053947217BBBBBB" }))).not.toBe(offlineEventKey(scanEvent()));
  });

  it("обрезает пробелы вокруг кода перед построением ключа", () => {
    const spaced = scanEvent({ code: "  0104006396053947217ABCDEF  " });
    expect(offlineEventKey(spaced)).toBe(offlineEventKey(scanEvent()));
  });

  it("не схлопывает коды разного регистра: КИЗ регистрозависим", () => {
    const lower = scanEvent({ code: "0104006396053947217abcdef" });
    expect(offlineEventKey(lower)).not.toBe(offlineEventKey(scanEvent()));
  });

  it("ключ завершения заказа не зависит от позиции и кода", () => {
    const complete = offlineEventKey({ ...scanEvent(), type: "order_complete", orderItemId: "item-9", code: "" });
    expect(complete).toBe("order_complete|order-1");
  });
});
