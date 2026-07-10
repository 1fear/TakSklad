export const TASHKENT_TIME_ZONE = "Asia/Tashkent";

const TASHKENT_UTC_OFFSET_MS = 5 * 60 * 60 * 1_000;
const ONE_DAY_MS = 24 * 60 * 60 * 1_000;

function shiftedToTashkent(now: Date): Date {
  if (!Number.isFinite(now.getTime())) {
    throw new RangeError("Business time requires a valid Date.");
  }
  return new Date(now.getTime() + TASHKENT_UTC_OFFSET_MS);
}

/** Returns the warehouse business date, independently of the browser time zone. */
export function tashkentBusinessDate(now: Date = new Date()): string {
  return shiftedToTashkent(now).toISOString().slice(0, 10);
}

export function tashkentBusinessMonth(now: Date = new Date()): string {
  return tashkentBusinessDate(now).slice(0, 7);
}

/** Exact delay until the next 00:00 in Asia/Tashkent. */
export function millisecondsUntilTashkentMidnight(now: Date = new Date()): number {
  const shifted = shiftedToTashkent(now);
  const elapsedToday = (
    shifted.getUTCHours() * 60 * 60 * 1_000
    + shifted.getUTCMinutes() * 60 * 1_000
    + shifted.getUTCSeconds() * 1_000
    + shifted.getUTCMilliseconds()
  );
  return ONE_DAY_MS - elapsedToday;
}

export type CancelMidnightRefresh = () => void;

/**
 * Runs at every Tashkent business-day boundary. The clock dependency makes the
 * boundary behavior deterministic in unit tests and avoids a page reload.
 */
export function scheduleTashkentMidnightRefresh(
  callback: () => void,
  now: () => Date = () => new Date(),
): CancelMidnightRefresh {
  let timeout: ReturnType<typeof setTimeout> | undefined;
  let cancelled = false;

  const schedule = () => {
    timeout = setTimeout(() => {
      if (cancelled) return;
      callback();
      schedule();
    }, millisecondsUntilTashkentMidnight(now()));
  };

  schedule();
  return () => {
    cancelled = true;
    if (timeout !== undefined) clearTimeout(timeout);
  };
}
