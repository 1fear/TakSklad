/**
 * One station per browser: a Web Lock held until the page goes away.
 *
 * The desktop refuses a second copy of itself (`src/taksklad/main.py:194`). The browser equivalent is one window
 * owning the queue and the print queue; a second window must show "already open" and send nothing.
 */

export const STATION_LOCK_NAME = "taksklad-station";

/**
 * True when this window now owns the station, false when another window already does.
 * A browser without Web Locks (or one that refuses the call) cannot tell, so it counts as available.
 */
export async function acquireStationLock(
  locks: Pick<LockManager, "request"> | undefined = typeof navigator === "undefined" ? undefined : navigator.locks,
): Promise<boolean> {
  if (!locks?.request) return true;
  return new Promise<boolean>((resolve) => {
    locks
      .request(STATION_LOCK_NAME, { ifAvailable: true }, (lock) => {
        resolve(lock !== null);
        // The lock lasts as long as this promise is pending: for the life of the page.
        return lock ? new Promise<void>(() => undefined) : undefined;
      })
      .catch(() => resolve(true));
  });
}
