import { describe, expect, it } from "vitest";

import { STATION_LOCK_NAME, acquireStationLock } from "../queue/stationLock";
import { fakeLocks } from "./webLocks";

describe("acquireStationLock", () => {
  it("takes the lock when it is free and holds it for the life of the page", async () => {
    const { locks, held, requests } = fakeLocks();

    await expect(acquireStationLock(locks)).resolves.toBe(true);

    expect(requests).toEqual([{ name: STATION_LOCK_NAME, options: { ifAvailable: true } }]);
    expect(held.has(STATION_LOCK_NAME)).toBe(true);
  });

  it("reports a second window as not owning the station", async () => {
    const { locks } = fakeLocks();

    await expect(acquireStationLock(locks)).resolves.toBe(true);
    await expect(acquireStationLock(locks)).resolves.toBe(false);
  });

  it("treats a browser without Web Locks as available", async () => {
    await expect(acquireStationLock(undefined)).resolves.toBe(true);
    await expect(acquireStationLock({} as Pick<LockManager, "request">)).resolves.toBe(true);
  });

  it("treats a refused lock request as available instead of blocking the station", async () => {
    const refusing = { request: () => Promise.reject(new DOMException("denied", "SecurityError")) };

    await expect(acquireStationLock(refusing as unknown as Pick<LockManager, "request">)).resolves.toBe(true);
  });

  it("uses navigator.locks by default and counts jsdom, which has none, as available", async () => {
    expect("locks" in navigator).toBe(false);

    await expect(acquireStationLock()).resolves.toBe(true);
  });
});
