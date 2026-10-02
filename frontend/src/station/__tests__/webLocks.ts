/** The part of Web Locks the station relies on: ifAvailable, held while the callback's promise is pending. */
export function fakeLocks() {
  const held = new Set<string>();
  const requests: Array<{ name: string; options: unknown }> = [];
  const locks = {
    async request(name: string, options: LockOptions, callback: (lock: Lock | null) => unknown) {
      requests.push({ name, options });
      if (held.has(name)) return callback(null);
      held.add(name);
      try {
        return await callback({ name, mode: "exclusive" } as Lock);
      } finally {
        held.delete(name);
      }
    },
  } as unknown as Pick<LockManager, "request">;
  return { locks, held, requests };
}
