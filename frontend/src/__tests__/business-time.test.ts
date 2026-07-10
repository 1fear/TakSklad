import { afterEach, describe, expect, it, vi } from "vitest";

import {
  millisecondsUntilTashkentMidnight,
  scheduleTashkentMidnightRefresh,
  tashkentBusinessDate,
  tashkentBusinessMonth,
} from "../data-flow/business-time";

afterEach(() => {
  vi.useRealTimers();
});

describe("Asia/Tashkent business time", () => {
  it.each([
    ["before local midnight", "2026-02-28T18:59:59.999Z", "2026-02-28"],
    ["local midnight", "2026-02-28T19:00:00.000Z", "2026-03-01"],
    ["local 04:59", "2026-02-28T23:59:59.999Z", "2026-03-01"],
    ["leap day", "2028-02-28T19:00:00.000Z", "2028-02-29"],
    ["after leap day", "2028-02-29T19:00:00.000Z", "2028-03-01"],
    ["year boundary", "2026-12-31T19:00:00.000Z", "2027-01-01"],
  ])("maps %s to one warehouse date", (_name, instant, expected) => {
    expect(tashkentBusinessDate(new Date(instant))).toBe(expected);
  });

  it("derives the warehouse month and rejects invalid dates", () => {
    expect(tashkentBusinessMonth(new Date("2026-12-31T19:00:00.000Z"))).toBe("2027-01");
    expect(() => tashkentBusinessDate(new Date("invalid"))).toThrow(RangeError);
  });

  it.each([
    ["2026-03-01T18:59:59.500Z", 500],
    ["2026-03-01T19:00:00.000Z", 86_400_000],
    ["2028-02-28T18:00:00.000Z", 3_600_000],
  ])("computes the exact next-local-midnight delay at %s", (instant, expected) => {
    expect(millisecondsUntilTashkentMidnight(new Date(instant))).toBe(expected);
  });

  it("refreshes an open page at Tashkent midnight and can be cancelled", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-03-01T18:59:59.500Z"));
    const refresh = vi.fn();
    const cancel = scheduleTashkentMidnightRefresh(refresh);

    vi.advanceTimersByTime(499);
    expect(refresh).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(refresh).toHaveBeenCalledTimes(1);

    cancel();
    vi.advanceTimersByTime(86_400_000);
    expect(refresh).toHaveBeenCalledTimes(1);
  });
});
