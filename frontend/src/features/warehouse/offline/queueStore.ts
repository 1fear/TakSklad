/**
 * Durable storage for the browser offline scan queue.
 *
 * Two sections mirror the desktop queue (`src/taksklad/storage.py`):
 * `pending` holds events waiting for the backend, `blocked` holds events the
 * backend refused for good. A blocked scan describes a block that physically
 * left the warehouse, so it must survive a reload and stay visible until the
 * operator dismisses it explicitly.
 *
 * The store is an interface with two implementations on purpose: unit tests run
 * against the in-memory one without pulling an IndexedDB shim into the project,
 * and the IndexedDB one is exercised end to end in a real browser
 * (`frontend/e2e/offline-scan.spec.ts`).
 */

import { normalizeKizCode } from "../kizFormat";
import { offlineEventKey, type OfflineEvent } from "./queueTypes";

export const BLOCKED_LIMIT = 500;

export type BlockedEvent = {
  key: string;
  event: OfflineEvent;
  reasonCode: string;
  reasonMessage: string;
  blockedAt: string;
};

export type OfflineQueueStore = {
  enqueue(event: OfflineEvent): Promise<void>;
  listPending(): Promise<OfflineEvent[]>;
  listPendingCodes(): Promise<Set<string>>;
  listBlocked(): Promise<BlockedEvent[]>;
  remove(key: string): Promise<void>;
  update(key: string, patch: Partial<OfflineEvent>): Promise<void>;
  block(key: string, reasonCode: string, reasonMessage: string): Promise<void>;
  dismissBlocked(key: string): Promise<void>;
};

type Clock = () => string;

function pendingCodes(events: OfflineEvent[]): Set<string> {
  const codes = new Set<string>();
  for (const event of events) {
    if (event.type !== "scan") continue;
    const code = normalizeKizCode(event.code);
    if (code) codes.add(code);
  }
  return codes;
}

export function createMemoryQueueStore(now: Clock = () => new Date().toISOString()): OfflineQueueStore {
  // Map keeps insertion order, which is the replay order the backend must see.
  const pending = new Map<string, OfflineEvent>();
  let blocked: BlockedEvent[] = [];

  return {
    async enqueue(event) {
      const key = offlineEventKey(event);
      if (!pending.has(key)) pending.set(key, event);
    },
    async listPending() {
      return [...pending.values()];
    },
    async listPendingCodes() {
      return pendingCodes([...pending.values()]);
    },
    async listBlocked() {
      return [...blocked];
    },
    async remove(key) {
      pending.delete(key);
    },
    async update(key, patch) {
      const current = pending.get(key);
      if (current) pending.set(key, { ...current, ...patch });
    },
    async block(key, reasonCode, reasonMessage) {
      const event = pending.get(key);
      if (!event) return;
      pending.delete(key);
      blocked = [...blocked, { key, event, reasonCode, reasonMessage, blockedAt: now() }].slice(-BLOCKED_LIMIT);
    },
    async dismissBlocked(key) {
      blocked = blocked.filter((item) => item.key !== key);
    },
  };
}

const DB_VERSION = 1;
const PENDING_STORE = "pending";
const BLOCKED_STORE = "blocked";

type PendingRecord = OfflineEvent & { key: string; sequence: number };
type BlockedRecord = BlockedEvent & { sequence: number };

export class OfflineStorageUnavailableError extends Error {
  constructor(cause?: unknown) {
    super("Локальное хранилище браузера недоступно, офлайн-очередь работать не может");
    this.name = "OfflineStorageUnavailableError";
    this.cause = cause;
  }
}

export function offlineStorageSupported(): boolean {
  return typeof indexedDB !== "undefined";
}

function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
  });
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () => reject(transaction.error ?? new Error("IndexedDB transaction aborted"));
    transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB transaction failed"));
  });
}

export function createIndexedDbQueueStore(
  dbName = "taksklad-warehouse-queue",
  now: Clock = () => new Date().toISOString(),
): OfflineQueueStore {
  let connection: Promise<IDBDatabase> | null = null;

  function db(): Promise<IDBDatabase> {
    if (!offlineStorageSupported()) return Promise.reject(new OfflineStorageUnavailableError());
    if (!connection) {
      connection = new Promise<IDBDatabase>((resolve, reject) => {
        const request = indexedDB.open(dbName, DB_VERSION);
        request.onupgradeneeded = () => {
          const database = request.result;
          if (!database.objectStoreNames.contains(PENDING_STORE)) {
            database.createObjectStore(PENDING_STORE, { keyPath: "key" }).createIndex("sequence", "sequence");
          }
          if (!database.objectStoreNames.contains(BLOCKED_STORE)) {
            database.createObjectStore(BLOCKED_STORE, { keyPath: "key" }).createIndex("sequence", "sequence");
          }
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(new OfflineStorageUnavailableError(request.error));
        request.onblocked = () => reject(new OfflineStorageUnavailableError(new Error("IndexedDB upgrade blocked")));
      }).catch((error) => {
        connection = null;
        throw error instanceof OfflineStorageUnavailableError ? error : new OfflineStorageUnavailableError(error);
      });
    }
    return connection;
  }

  async function readAll<T>(storeName: string): Promise<T[]> {
    const database = await db();
    const transaction = database.transaction(storeName, "readonly");
    const rows = await requestResult<T[]>(transaction.objectStore(storeName).getAll());
    await transactionDone(transaction);
    return rows;
  }

  async function nextSequence(store: IDBObjectStore): Promise<number> {
    const rows = await requestResult<{ sequence?: number }[]>(store.getAll());
    return rows.reduce((max, row) => Math.max(max, Number(row.sequence ?? 0)), 0) + 1;
  }

  async function sortedPending(): Promise<PendingRecord[]> {
    const rows = await readAll<PendingRecord>(PENDING_STORE);
    return rows.sort((left, right) => left.sequence - right.sequence);
  }

  function toEvent(record: PendingRecord): OfflineEvent {
    return {
      type: record.type,
      orderId: record.orderId,
      orderItemId: record.orderItemId,
      code: record.code,
      actor: record.actor,
      workstationId: record.workstationId,
      scannedAt: record.scannedAt,
      createdAt: record.createdAt,
      attempts: record.attempts,
      lastError: record.lastError,
    };
  }

  return {
    async enqueue(event) {
      const key = offlineEventKey(event);
      const database = await db();
      const transaction = database.transaction(PENDING_STORE, "readwrite");
      const store = transaction.objectStore(PENDING_STORE);
      const existing = await requestResult<PendingRecord | undefined>(store.get(key));
      if (!existing) {
        store.put({ ...event, key, sequence: await nextSequence(store) } satisfies PendingRecord);
      }
      await transactionDone(transaction);
    },

    async listPending() {
      return (await sortedPending()).map(toEvent);
    },

    async listPendingCodes() {
      return pendingCodes((await sortedPending()).map(toEvent));
    },

    async listBlocked() {
      const rows = await readAll<BlockedRecord>(BLOCKED_STORE);
      return rows
        .sort((left, right) => left.sequence - right.sequence)
        .map(({ sequence: _sequence, ...blocked }) => blocked);
    },

    async remove(key) {
      const database = await db();
      const transaction = database.transaction(PENDING_STORE, "readwrite");
      transaction.objectStore(PENDING_STORE).delete(key);
      await transactionDone(transaction);
    },

    async update(key, patch) {
      const database = await db();
      const transaction = database.transaction(PENDING_STORE, "readwrite");
      const store = transaction.objectStore(PENDING_STORE);
      const existing = await requestResult<PendingRecord | undefined>(store.get(key));
      if (existing) store.put({ ...existing, ...patch, key, sequence: existing.sequence });
      await transactionDone(transaction);
    },

    async block(key, reasonCode, reasonMessage) {
      const database = await db();
      const transaction = database.transaction([PENDING_STORE, BLOCKED_STORE], "readwrite");
      const pendingStore = transaction.objectStore(PENDING_STORE);
      const blockedStore = transaction.objectStore(BLOCKED_STORE);
      const existing = await requestResult<PendingRecord | undefined>(pendingStore.get(key));
      if (existing) {
        pendingStore.delete(key);
        blockedStore.put({
          key,
          event: toEvent(existing),
          reasonCode,
          reasonMessage,
          blockedAt: now(),
          sequence: await nextSequence(blockedStore),
        } satisfies BlockedRecord);
        const stored = await requestResult<BlockedRecord[]>(blockedStore.getAll());
        const overflow = stored.sort((left, right) => left.sequence - right.sequence).slice(0, -BLOCKED_LIMIT);
        for (const item of overflow) blockedStore.delete(item.key);
      }
      await transactionDone(transaction);
    },

    async dismissBlocked(key) {
      const database = await db();
      const transaction = database.transaction(BLOCKED_STORE, "readwrite");
      transaction.objectStore(BLOCKED_STORE).delete(key);
      await transactionDone(transaction);
    },
  };
}
