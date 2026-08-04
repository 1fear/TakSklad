# Durable офлайн-очередь сканов в web-панели, план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** оператор продолжает сканировать КИЗы в браузере при недоступном backend,
каждый физически отсканированный код переживает перезагрузку вкладки и уходит
в PostgreSQL после восстановления связи, потери скана без видимого следа не бывает

**Architecture:** очередь живёт в IndexedDB и повторяется **из страницы**, а не из
service worker, потому что запись требует cookie-сессии и CSRF-токена, которые
живут во вкладке
Backend не меняется: `create_scan` уже идемпотентен по паре
`(order_item_id, code)` и на повтор возвращает существующий скан
(`backend/app/orders_service.py:240-241`), это и есть основание для безопасного replay
Контракт очереди повторяет проверенный десктопный: детерминированный id события,
дедупликация, отдельная durable-секция для событий, которые backend отверг навсегда

**Tech Stack:** TypeScript 5.9, React 19, Vite 8, vitest, Playwright, IndexedDB
Новых runtime-зависимостей не добавляется

**Основание:** [паритет web и desktop](../../web-desktop-parity.md), раздел
«Офлайн-работа склада (P0)»
Десктопный образец: `src/taksklad/backend_events.py`, `src/taksklad/storage.py`

## Что уже есть и что переносится

В web офлайн-очереди нет: в `frontend/src` нет ни IndexedDB, ни service worker,
ни обработки `navigator.onLine`, `frontend/public` содержит только иконку

На десктопе очередь есть и работает годами, переносим именно её смысл:

| Десктоп | Что делает | Web-аналог в этом плане |
|---|---|---|
| `pending_backend_events` | durable-очередь сканов и завершений | store `pending` в IndexedDB |
| `blocked_backend_events` | события, которые backend отверг навсегда | store `blocked` в IndexedDB |
| `make_backend_event_id` | детерминированный id, дубли схлопываются | составной ключ без хеша |
| `KNOWN_NON_RETRYABLE_SCAN_ERROR_CODES` | список неповторяемых ошибок | `errorPolicy.ts` + contract-тест |
| `is_duplicate_scan_ack` | 409 `scan_duplicate_ack` считается успехом | та же ветка в replay |
| `get_pending_backend_codes` | локальная дедупликация до отправки | `listPendingCodes` |

Открытый P2 десктопа исправляем сразу: заблокированные события **показываются
оператору**, а не только сохраняются

## Global Constraints

- Frontend-проверки: `npm --prefix frontend run lint` (0 warnings),
  `npm --prefix frontend run typecheck`, `npm --prefix frontend run test`,
  `npm --prefix frontend run a11y`, `npm --prefix frontend run build`,
  `npm --prefix frontend run e2e`
- Backend-тесты: `PYTHONPATH=. python -m unittest discover -s tests`
- Все коммиты только в `main`, локальные хуки блокируют коммит из другой ветки
- Backend, миграции и API-контракт в фазах 1-3 **не меняются**
- Client-facing изменения (тексты, счётчики, новые элементы операторского экрана)
  требуют отдельного approval по контракту `CLAUDE.md`, до approval задачи фазы 2
  не выкатываются
- В очередь не попадает код, не прошедший формат-гард `kizFormat.ts`:
  офлайн некому отфильтровать мусор
- Реальные КИЗы, имена клиентов и chat ID в тестах и фикстурах не используются
- Очередь не очищается при logout: физически отсканированный блок уже уехал
  со склада, его след стирать нельзя

## Фазы и порядок

| Фаза | Содержание | Гейт на выходе |
|---|---|---|
| 0 | Решение владельца и канонический документ | Антон выбрал вариант «реализуем очередь», запись в KB переведена из `not-confirmed` |
| 1 | Ядро очереди, чистая логика, UI не меняется | Task 1-5, зелёные unit-тесты, ничего не видно оператору |
| 2 | Подключение к экрану и операторский UI | Task 6-8, **approval client-facing** до выката |
| 3 | Оболочка приложения офлайн (service worker) | Task 9, отдельное решение, kill switch обязателен |
| 4 | Физическая приёмка и параллельный прогон | `web-warehouse-acceptance.md` раздел B, сценарий с выдернутым сетевым кабелем |

Фаза 1 самостоятельно ценна: она не меняет поведение и её можно вести в `main`
без риска для склада

## Статус выполнения

Фаза 1 выполнена 2026-08-04, Task 1-5, коммиты `979db83`, `7497145`, `23f70d5`,
`7c26f38`, `7945899`

Гейты на момент завершения: `lint` 0 warnings, `typecheck` чисто,
`npm run test` 209 passed, `a11y` 6 passed, `build` ok,
backend `1743 tests, OK (skipped=87, expected failures=4)`
Операторская панель не импортирует новые модули, поведение не изменилось

Два отступления от исходного плана, оба в сторону безопасности:

1. **КИЗ регистрозависим** Черновик плана требовал теста на регистронезависимость
   ключа Проверка `normalizeKizCode` (`frontend/src/features/warehouse/kizFormat.ts:42`)
   и десктопного `normalize_kiz_code` (`src/taksklad/utils.py:92`) показала, что
   нормализация только обрезает пробелы Приведение регистра меняло бы отправляемый
   код, тест переписан на обратное утверждение
2. **Классификация 4xx строже десктопной**, причина в блоке Task 3 ниже

### Независимое ревью и исправления

Фаза 1 после написания прошла ревью вторым агентом (Codex), задача была найти
механизмы потери, задвоения и вечного зависания скана
Учётного задвоения не найдено, найдено четыре дефекта, все подтверждены чтением
кода и исправлены

| Дефект | Чем грозил | Исправление |
|---|---|---|
| `403 csrf_invalid` и `origin_denied` классифицировались как `blocked` | Backend отдаёт их до складской операции, это устаревшая вкладка Физически отсканированный КИЗ уезжал в `blocked`, откуда повтор его больше не берёт | Оба кода повторяются, список закреплён contract-тестом против `backend/app/csrf.py` Коммит `54ace2a` |
| Безусловный `break` после первой retry-ошибки | Одно застрявшее событие оставляло весь хвост очереди без единой попытки, даже сканы других заказов | Событие пропускается, проход останавливается после трёх отказов подряд, `order_complete` по-прежнему не обгоняет свои сканы Коммит `2076858` |
| Проекция считала события, а не блоки | Агрегатный короб весит 50 блоков (`backend/app/scan_quantities.py:3`), а показывался как один Оператор досканировал бы лишнее и получил `order_item_fully_scanned_new_code` | Правило веса перенесено в браузер, три копии закреплены contract-тестом Коммит `e9f718c` |
| Потерянный ответ на успешную запись считался дважды | Событие остаётся в очереди, сервер уже отдаёт `scanned_blocks`, проекция складывала одно и то же | Дедуп по `scan_codes` позиции Коммит `2327f1f` |

Отдельно проверено и признано корректным: транзакции IndexedDB не рвутся
посторонним `await`, пересекающиеся `readwrite` сериализуются самим IndexedDB,
ошибка внутри `block` откатывает и удаление из `pending`, параллельные вкладки
не дают учётного задвоения из-за идемпотентности `create_scan`

Ограничение доказательства: реального browser-теста IndexedDB ещё нет, он в
Task 8, до него IndexedDB-реализация проверена только чтением

Дальше по плану гейт approval перед Task 6

## Структура файлов

| Файл | Ответственность |
|------|-----------------|
| `frontend/src/features/warehouse/offline/queueTypes.ts` | новый, типы события и составной ключ |
| `frontend/src/features/warehouse/offline/queueStore.ts` | новый, интерфейс хранилища, IndexedDB и in-memory реализации |
| `frontend/src/features/warehouse/offline/errorPolicy.ts` | новый, классификация ответа backend |
| `frontend/src/features/warehouse/offline/replay.ts` | новый, движок повтора очереди |
| `frontend/src/features/warehouse/offline/projection.ts` | новый, локальный прогресс позиции с учётом очереди |
| `frontend/src/features/warehouse/offline/useOfflineQueue.ts` | новый, React-хук: состояние очереди, автоповтор, блокировка вкладок |
| `frontend/src/features/warehouse/OfflineQueuePanel.tsx` | новый, счётчик и список заблокированных для оператора |
| `frontend/src/features/warehouse/WarehousePanel.tsx` | подключение очереди к `submitScan` и `removeLastCode` |
| `frontend/src/__tests__/offlineQueue.test.ts` | новый, ядро очереди |
| `frontend/src/__tests__/offlineReplay.test.ts` | новый, повтор и классификация ошибок |
| `frontend/e2e/offline-scan.spec.ts` | новый, реальный IndexedDB и реальный обрыв сети |
| `tests/test_web_offline_queue_contract.py` | новый, список неповторяемых кодов не расходится с десктопом |

---

### Task 1: Тип события и детерминированный ключ

**Files:**
- Create: `frontend/src/features/warehouse/offline/queueTypes.ts`
- Test: `frontend/src/__tests__/offlineQueue.test.ts`

**Interfaces:**
- Consumes: `normalizeKizCode` из `frontend/src/features/warehouse/kizFormat.ts`
- Produces: тип `OfflineEvent`, функция `offlineEventKey(event): string`

Десктоп хеширует кортеж `{type, order_item_id, order_id, code}`
(`src/taksklad/backend_events.py:141`)
В web берём тот же кортеж, но как читаемую строку: коллизий нет, ключ виден
в диагностике без расшифровки

- [ ] **Step 1: Написать падающий тест**

```ts
import { describe, expect, it } from "vitest";
import { offlineEventKey, type OfflineEvent } from "../features/warehouse/offline/queueTypes";

function scanEvent(overrides: Partial<OfflineEvent> = {}): OfflineEvent {
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

  it("обрезает пробелы вокруг кода перед построением ключа", () => {
    const spaced = scanEvent({ code: "  0104006396053947217ABCDEF  " });
    expect(offlineEventKey(spaced)).toBe(offlineEventKey(scanEvent()));
  });

  it("не схлопывает коды разного регистра: КИЗ регистрозависим", () => {
    const lower = scanEvent({ code: "0104006396053947217abcdef" });
    expect(offlineEventKey(lower)).not.toBe(offlineEventKey(scanEvent()));
  });

  it("ключ завершения заказа не зависит от позиции", () => {
    const complete = offlineEventKey({ ...scanEvent(), type: "order_complete", orderItemId: "item-9", code: "" });
    expect(complete).toBe("order_complete|order-1");
  });
});
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `npm --prefix frontend run test -- offlineQueue`
Expected: FAIL, модуль `queueTypes` не найден

- [ ] **Step 3: Написать минимальную реализацию**

```ts
import { normalizeKizCode } from "../kizFormat";

export type OfflineEventType = "scan" | "order_complete";

export type OfflineEvent = {
  type: OfflineEventType;
  orderId: string;
  orderItemId: string;
  code: string;
  actor: string;
  workstationId: string;
  scannedAt: string;
  createdAt: string;
  attempts: number;
  lastError: string;
};

export function offlineEventKey(event: OfflineEvent): string {
  if (event.type === "order_complete") return `order_complete|${event.orderId}`;
  return `scan|${event.orderItemId}|${normalizeKizCode(event.code)}`;
}
```

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

Run: `npm --prefix frontend run test -- offlineQueue`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add frontend/src/features/warehouse/offline/queueTypes.ts frontend/src/__tests__/offlineQueue.test.ts
git commit -m "feat(web-offline): тип события очереди и детерминированный ключ"
```

---

### Task 2: Хранилище очереди, интерфейс и две реализации

**Files:**
- Create: `frontend/src/features/warehouse/offline/queueStore.ts`
- Test: `frontend/src/__tests__/offlineQueue.test.ts` (дописать)

**Interfaces:**
- Consumes: `OfflineEvent`, `offlineEventKey` из Task 1
- Produces: тип `OfflineQueueStore` с методами `enqueue`, `listPending`,
  `listBlocked`, `remove`, `block`, `listPendingCodes`, `update`, `dismissBlocked`;
  фабрики `createMemoryQueueStore()` и `createIndexedDbQueueStore(dbName?)`;
  класс `OfflineStorageUnavailableError` и предикат `offlineStorageSupported()`

Интерфейс нужен, чтобы вся логика очереди тестировалась в vitest без IndexedDB
и без новых зависимостей
Реальный IndexedDB проверяется в Playwright (Task 8)

- [ ] **Step 1: Написать падающий тест**

```ts
import { describe, expect, it } from "vitest";
import { createMemoryQueueStore } from "../features/warehouse/offline/queueStore";

describe("queueStore", () => {
  it("повторное добавление того же скана не создаёт второй записи", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent());
    await store.enqueue(scanEvent({ scannedAt: "2026-08-04T10:05:00+05:00" }));
    expect(await store.listPending()).toHaveLength(1);
  });

  it("отдаёт нормализованные коды очереди для локальной дедупликации", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent());
    expect(await store.listPendingCodes()).toEqual(new Set(["0104006396053947217ABCDEF"]));
  });

  it("block переносит событие из pending в blocked и сохраняет причину", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent());
    await store.block(offlineEventKey(scanEvent()), "scan_product_mismatch", "Товар не совпадает");
    expect(await store.listPending()).toHaveLength(0);
    const blocked = await store.listBlocked();
    expect(blocked).toHaveLength(1);
    expect(blocked[0].reasonCode).toBe("scan_product_mismatch");
    expect(blocked[0].event.code).toBe("0104006396053947217ABCDEF");
  });

  it("blocked хранит не больше 500 записей и вытесняет старые", async () => {
    const store = createMemoryQueueStore();
    for (let index = 0; index < 505; index += 1) {
      const event = scanEvent({ code: `010400639605394721${String(index).padStart(4, "0")}` });
      await store.enqueue(event);
      await store.block(offlineEventKey(event), "order_closed", "Позиция закрыта");
    }
    expect(await store.listBlocked()).toHaveLength(500);
  });
});
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `npm --prefix frontend run test -- offlineQueue`
Expected: FAIL, `createMemoryQueueStore` не экспортируется

- [ ] **Step 3: Написать минимальную реализацию**

```ts
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
};

export function createMemoryQueueStore(now = () => new Date().toISOString()): OfflineQueueStore {
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
      const codes = new Set<string>();
      for (const event of pending.values()) {
        if (event.type !== "scan") continue;
        const code = normalizeKizCode(event.code);
        if (code) codes.add(code);
      }
      return codes;
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
  };
}
```

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

Run: `npm --prefix frontend run test -- offlineQueue`
Expected: PASS

- [ ] **Step 5: Добавить IndexedDB-реализацию поверх того же интерфейса**

```ts
const DB_VERSION = 1;
const PENDING_STORE = "pending";
const BLOCKED_STORE = "blocked";

function openDb(dbName: string): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(dbName, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(PENDING_STORE)) db.createObjectStore(PENDING_STORE, { keyPath: "key" });
      if (!db.objectStoreNames.contains(BLOCKED_STORE)) db.createObjectStore(BLOCKED_STORE, { keyPath: "key" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB недоступен"));
  });
}

export function createIndexedDbQueueStore(dbName = "taksklad-warehouse-queue"): OfflineQueueStore {
  // тот же контракт, что и у memory-стора, транзакции readwrite на PENDING_STORE и BLOCKED_STORE
  // ...
}
```

Реализация обязана: писать `{key, ...event}` одной транзакцией, при
`enqueue` использовать `add` и глотать `ConstraintError` как признак дубля,
при недоступном IndexedDB (приватный режим, отключённое хранилище) бросать
ошибку, а не молча деградировать в память

- [ ] **Step 6: Запустить полный набор и коммит**

Run: `npm --prefix frontend run test && npm --prefix frontend run typecheck && npm --prefix frontend run lint`
Expected: PASS

```bash
git add frontend/src/features/warehouse/offline/queueStore.ts frontend/src/__tests__/offlineQueue.test.ts
git commit -m "feat(web-offline): durable-хранилище очереди сканов"
```

---

### Task 3: Классификация ответа backend

**Files:**
- Create: `frontend/src/features/warehouse/offline/errorPolicy.ts`
- Test: `frontend/src/__tests__/offlineReplay.test.ts`
- Test: `tests/test_web_offline_queue_contract.py`

**Interfaces:**
- Consumes: `ApiRequestError` из `frontend/src/api/core.ts` (поля `status`, `code`)
- Produces: `classifyReplayFailure(error): "retry" | "blocked" | "synced"`,
  константа `NON_RETRYABLE_SCAN_CODES: readonly string[]`

Три исхода ровно как на десктопе:
`synced` это 409 `scan_duplicate_ack`, backend уже знает этот скан;
`blocked` это 409 с известным кодом, повторять бессмысленно;
`retry` это сеть, 5xx и всё неизвестное

- [ ] **Step 1: Написать падающий тест**

```ts
import { describe, expect, it } from "vitest";
import { ApiRequestError } from "../api/core";
import { classifyReplayFailure, NON_RETRYABLE_SCAN_CODES } from "../features/warehouse/offline/errorPolicy";

describe("classifyReplayFailure", () => {
  it("дубликат скана считается успехом", () => {
    expect(classifyReplayFailure(new ApiRequestError(409, "Conflict", "", "scan_duplicate_ack"))).toBe("synced");
  });

  it.each(NON_RETRYABLE_SCAN_CODES)("409 %s блокируется навсегда", (code) => {
    expect(classifyReplayFailure(new ApiRequestError(409, "Conflict", "", code))).toBe("blocked");
  });

  it("409 с неизвестным кодом блокируется, как на десктопе", () => {
    expect(classifyReplayFailure(new ApiRequestError(409, "Conflict", "", "something_new"))).toBe("blocked");
  });

  it("сетевая ошибка повторяется", () => {
    expect(classifyReplayFailure(new TypeError("Failed to fetch"))).toBe("retry");
  });

  it("5xx повторяется", () => {
    expect(classifyReplayFailure(new ApiRequestError(503, "Service Unavailable", "", ""))).toBe("retry");
  });

  it("401 повторяется: нужна новая сессия, а не потеря скана", () => {
    expect(classifyReplayFailure(new ApiRequestError(401, "Unauthorized", "", ""))).toBe("retry");
  });
});
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `npm --prefix frontend run test -- offlineReplay`
Expected: FAIL, модуль `errorPolicy` не найден

- [ ] **Step 3: Написать минимальную реализацию**

```ts
import { ApiRequestError } from "../../../api/core";

export const DUPLICATE_SCAN_ACK_CODE = "scan_duplicate_ack";

export const NON_RETRYABLE_SCAN_CODES = [
  "kiz_format_invalid",
  "kiz_already_owned",
  "order_item_fully_scanned_new_code",
  "order_closed",
  "transfer_order_irreversible",
  "legal_entity_unresolved",
  "aggregate_box_product_mismatch",
  "aggregate_box_exceeds_plan",
  "scan_product_mismatch",
  "shipment_manifest_mismatch",
] as const;

/** 4xx-ответы, которые следующая попытка действительно может исправить */
const RETRYABLE_CLIENT_STATUSES = new Set([401, 408, 429]);

export type ReplayVerdict = "retry" | "blocked" | "synced";

export function classifyReplayFailure(error: unknown): ReplayVerdict {
  if (!(error instanceof ApiRequestError)) return "retry";
  if (error.status === 409 && error.code === DUPLICATE_SCAN_ACK_CODE) return "synced";
  if (error.status >= 400 && error.status < 500 && !RETRYABLE_CLIENT_STATUSES.has(error.status)) return "blocked";
  return "retry";
}
```

Осознанное расхождение с десктопом: десктоп считает неповторяемым только
HTTP 409, поэтому любой другой 4xx крутится у него вечно
Backend отдаёт `kiz_format_invalid` со статусом 422
(`backend/app/orders_service.py:215`), и такое событие десктопная очередь
не закроет никогда
Браузер блокирует любой 4xx кроме трёх, которые реально меняются от повтора:
401 (нужна новая сессия), 408 (таймаут), 429 (rate limit)
Скан при этом не теряется, блокировка это перевод в durable-секцию,
видимую оператору

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

Run: `npm --prefix frontend run test -- offlineReplay`
Expected: PASS

- [ ] **Step 5: Написать contract-тест на стороне Python**

По образцу `tests/test_kiz_format_contract.py`

```python
"""Списки неповторяемых ошибок скана в браузере и на десктопе не должны разойтись.

Десктопная очередь роняет событие в blocked по
`KNOWN_NON_RETRYABLE_SCAN_ERROR_CODES`. Браузерная очередь обязана вести себя
так же, иначе один клиент будет вечно ретраить то, что другой давно признал
безнадёжным.
"""

from pathlib import Path
import re
import unittest

from taksklad.backend_events import KNOWN_NON_RETRYABLE_SCAN_ERROR_CODES

ROOT = Path(__file__).resolve().parents[1]
ERROR_POLICY_TS = ROOT / "frontend/src/features/warehouse/offline/errorPolicy.ts"


class WebOfflineQueueContractTest(unittest.TestCase):
    def test_non_retryable_codes_match_desktop(self):
        source = ERROR_POLICY_TS.read_text(encoding="utf-8")
        block = re.search(r"NON_RETRYABLE_SCAN_CODES\s*=\s*\[(.*?)\]", source, re.S)
        self.assertIsNotNone(block, "не найден список NON_RETRYABLE_SCAN_CODES")
        web_codes = set(re.findall(r'"([a-z_]+)"', block.group(1)))
        self.assertEqual(web_codes, set(KNOWN_NON_RETRYABLE_SCAN_ERROR_CODES))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 6: Запустить оба набора и коммит**

Run: `PYTHONPATH=. python -m unittest tests.test_web_offline_queue_contract -v`
Expected: PASS

```bash
git add frontend/src/features/warehouse/offline/errorPolicy.ts frontend/src/__tests__/offlineReplay.test.ts tests/test_web_offline_queue_contract.py
git commit -m "feat(web-offline): классификация ответа backend и contract-тест с десктопом"
```

---

### Task 4: Движок повтора очереди

**Files:**
- Create: `frontend/src/features/warehouse/offline/replay.ts`
- Test: `frontend/src/__tests__/offlineReplay.test.ts` (дописать)

**Interfaces:**
- Consumes: `OfflineQueueStore` (Task 2), `classifyReplayFailure` (Task 3)
- Produces: `replayQueue(store, deps): Promise<ReplaySummary>`,
  тип `ReplaySummary = { synced: number; blocked: number; failed: number; remaining: number }`

`deps` принимает `sendScan` и `sendComplete` явными функциями, чтобы тест не
трогал сеть, а в приложении туда передаются `createScan` и `completeWarehouseOrder`
из `frontend/src/api.ts`

Порядок обязателен: события повторяются в порядке добавления, `order_complete`
никогда не уходит раньше своих сканов

- [ ] **Step 1: Написать падающий тест**

```ts
describe("replayQueue", () => {
  it("успешный повтор убирает событие из очереди", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent());
    const summary = await replayQueue(store, { sendScan: async () => {}, sendComplete: async () => {} });
    expect(summary).toMatchObject({ synced: 1, blocked: 0, failed: 0, remaining: 0 });
  });

  it("дубликат на сервере закрывает событие как успешное", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent());
    const summary = await replayQueue(store, {
      sendScan: async () => { throw new ApiRequestError(409, "Conflict", "", "scan_duplicate_ack"); },
      sendComplete: async () => {},
    });
    expect(summary.synced).toBe(1);
    expect(await store.listPending()).toHaveLength(0);
  });

  it("неповторяемый конфликт уходит в blocked и остаётся видимым", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent());
    const summary = await replayQueue(store, {
      sendScan: async () => { throw new ApiRequestError(409, "Conflict", "Позиция закрыта", "order_closed"); },
      sendComplete: async () => {},
    });
    expect(summary.blocked).toBe(1);
    expect(await store.listPending()).toHaveLength(0);
    expect(await store.listBlocked()).toHaveLength(1);
  });

  it("сетевая ошибка оставляет событие в очереди и считает попытку", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent());
    await replayQueue(store, {
      sendScan: async () => { throw new TypeError("Failed to fetch"); },
      sendComplete: async () => {},
    });
    const pending = await store.listPending();
    expect(pending).toHaveLength(1);
    expect(pending[0].attempts).toBe(1);
    expect(pending[0].lastError).toContain("Failed to fetch");
  });

  it("первая же сетевая ошибка останавливает проход, чтобы не молотить оффлайн", async () => {
    const store = createMemoryQueueStore();
    await store.enqueue(scanEvent({ code: "0104006396053947217AAAAAA" }));
    await store.enqueue(scanEvent({ code: "0104006396053947217BBBBBB" }));
    let calls = 0;
    await replayQueue(store, {
      sendScan: async () => { calls += 1; throw new TypeError("Failed to fetch"); },
      sendComplete: async () => {},
    });
    expect(calls).toBe(1);
    expect(await store.listPending()).toHaveLength(2);
  });

  it("order_complete уходит после своих сканов", async () => {
    const store = createMemoryQueueStore();
    const order: string[] = [];
    await store.enqueue(scanEvent());
    await store.enqueue({ ...scanEvent(), type: "order_complete", code: "" });
    await replayQueue(store, {
      sendScan: async () => { order.push("scan"); },
      sendComplete: async () => { order.push("complete"); },
    });
    expect(order).toEqual(["scan", "complete"]);
  });
});
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `npm --prefix frontend run test -- offlineReplay`
Expected: FAIL, `replayQueue` не найден

- [ ] **Step 3: Написать минимальную реализацию**

```ts
import { classifyReplayFailure } from "./errorPolicy";
import { offlineEventKey, type OfflineEvent } from "./queueTypes";
import type { OfflineQueueStore } from "./queueStore";

export type ReplayDeps = {
  sendScan(event: OfflineEvent): Promise<void>;
  sendComplete(event: OfflineEvent): Promise<void>;
};

export type ReplaySummary = { synced: number; blocked: number; failed: number; remaining: number };

export async function replayQueue(store: OfflineQueueStore, deps: ReplayDeps): Promise<ReplaySummary> {
  const pending = await store.listPending();
  let synced = 0;
  let blocked = 0;
  let failed = 0;

  for (const event of pending) {
    const key = offlineEventKey(event);
    try {
      if (event.type === "scan") await deps.sendScan(event);
      else await deps.sendComplete(event);
      await store.remove(key);
      synced += 1;
    } catch (error) {
      const verdict = classifyReplayFailure(error);
      if (verdict === "synced") {
        await store.remove(key);
        synced += 1;
        continue;
      }
      if (verdict === "blocked") {
        const apiError = error as { code?: string; message?: string };
        await store.block(key, apiError.code ?? "", apiError.message ?? "");
        blocked += 1;
        continue;
      }
      failed += 1;
      await store.update(key, {
        attempts: (event.attempts ?? 0) + 1,
        lastError: error instanceof Error ? error.message : String(error),
      });
      break;
    }
  }

  return { synced, blocked, failed, remaining: (await store.listPending()).length };
}
```

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

Run: `npm --prefix frontend run test -- offlineReplay`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add frontend/src/features/warehouse/offline/replay.ts frontend/src/__tests__/offlineReplay.test.ts
git commit -m "feat(web-offline): движок повтора очереди сканов"
```

---

### Task 5: Локальный прогресс позиции с учётом очереди

**Files:**
- Create: `frontend/src/features/warehouse/offline/projection.ts`
- Test: `frontend/src/__tests__/offlineQueue.test.ts` (дописать)

**Interfaces:**
- Consumes: `OfflineEvent`, тип позиции заказа из `frontend/src/api.ts`
- Produces: `projectItemProgress(item, pending): { scannedBlocks: number; pendingBlocks: number; complete: boolean }`

Без этого оператор офлайн сканирует вслепую: сервер прогресс не подтвердит,
а счётчик позиции останется прежним

- [ ] **Step 1: Написать падающий тест**

```ts
describe("projectItemProgress", () => {
  const item = { id: "item-1", quantity_blocks: 3, scanned_blocks: 1 };

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
    expect(projectItemProgress(item, pending).scannedBlocks).toBe(3);
  });
});
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `npm --prefix frontend run test -- offlineQueue`
Expected: FAIL, `projectItemProgress` не найден

- [ ] **Step 3: Написать минимальную реализацию**

```ts
import type { OfflineEvent } from "./queueTypes";

export type ItemProgressInput = { id: string; quantity_blocks: number; scanned_blocks: number };

export function projectItemProgress(item: ItemProgressInput, pending: OfflineEvent[]) {
  const pendingBlocks = pending.filter(
    (event) => event.type === "scan" && event.orderItemId === item.id,
  ).length;
  const planned = Number(item.quantity_blocks || 0);
  const raw = Number(item.scanned_blocks || 0) + pendingBlocks;
  const scannedBlocks = planned > 0 ? Math.min(raw, planned) : raw;
  return { scannedBlocks, pendingBlocks, complete: planned > 0 && scannedBlocks >= planned };
}
```

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

Run: `npm --prefix frontend run test -- offlineQueue`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add frontend/src/features/warehouse/offline/projection.ts frontend/src/__tests__/offlineQueue.test.ts
git commit -m "feat(web-offline): локальный прогресс позиции с учётом очереди"
```

---

## Гейт approval перед фазой 2

Task 6-8 меняют операторский экран: появляется новое состояние «сохранено локально»,
счётчик очереди и список заблокированных сканов
Это client-facing изменение по контракту `CLAUDE.md`

До начала Task 6 подготовить и показать Антону:

1. exact before/after для каждого текста, который увидит оператор;
2. что происходит с прежним поведением: сейчас при отказе backend скан
   завершается ошибкой и код остаётся в поле, после изменения он будет принят
   локально, и это надо назвать вслух;
3. как оператор поймёт, что часть сканов ещё не в PostgreSQL.

Ниже тексты предлагаются как черновик, финальные согласовываются отдельно

---

### Task 6: Подключение очереди к сканированию

**Files:**
- Create: `frontend/src/features/warehouse/offline/useOfflineQueue.ts`
- Modify: `frontend/src/features/warehouse/WarehousePanel.tsx:146-228` (`submitScan`),
  `:230-257` (`removeLastCode`)
- Test: `frontend/src/__tests__/WarehousePanel.test.tsx` (дописать)

**Interfaces:**
- Consumes: `replayQueue`, `createIndexedDbQueueStore`, `projectItemProgress`
- Produces: хук `useOfflineQueue(config, actor)` возвращает
  `{ pending, blocked, enqueueScan, replayNow, dismissBlocked, status }`

Правила поведения, они же тестовые утверждения:

- формат-гард `kizFormatViolation` работает **до** очереди и офлайн тоже:
  невалидный код в очередь не попадает;
- preflight `lookupKizAvailability` офлайн пропускается, он и так подсказка,
  авторитет только у `POST /scans`;
- код, уже лежащий в очереди, второй раз не принимается: сообщение
  «Этот КИЗ уже в очереди на отправку»;
- при успешной онлайн-записи очередь не задействуется, поведение не меняется;
- при сетевой ошибке скан уходит в очередь, поле очищается, фокус возвращается
  в сканер, как при успешной записи;
- при 409 с известным кодом скан в очередь **не** уходит: это отказ по существу,
  поведение остаётся прежним, код остаётся выделенным в поле;
- автоповтор запускается на событие `online`, на возврат вкладки в фокус
  и по таймеру раз в 30 секунд, пока очередь непуста;
- одновременный повтор в двух вкладках гасится через `navigator.locks.request`
  с именем `taksklad-offline-replay`.

- [ ] **Step 1: Написать падающие тесты в `WarehousePanel.test.tsx`**

```tsx
it("при недоступном backend скан уходит в очередь, поле очищается", async () => {
  server.use(http.post("/api/v1/scans", () => HttpResponse.error()));
  renderPanel();
  await scanCode("0104006396053947217ABCDEF");
  expect(await screen.findByText(/сохранено локально/i)).toBeInTheDocument();
  expect(screen.getByLabelText(/сканер/i)).toHaveValue("");
  expect(await screen.findByText(/в очереди: 1/i)).toBeInTheDocument();
});

it("невалидный код офлайн в очередь не попадает", async () => {
  server.use(http.post("/api/v1/scans", () => HttpResponse.error()));
  renderPanel();
  await scanCode("ПРИВЕТ");
  expect(screen.queryByText(/сохранено локально/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/в очереди/i)).not.toBeInTheDocument();
});

it("повторный скан кода из очереди отклоняется", async () => {
  server.use(http.post("/api/v1/scans", () => HttpResponse.error()));
  renderPanel();
  await scanCode("0104006396053947217ABCDEF");
  await scanCode("0104006396053947217ABCDEF");
  expect(await screen.findByText(/уже в очереди на отправку/i)).toBeInTheDocument();
  expect(await screen.findByText(/в очереди: 1/i)).toBeInTheDocument();
});

it("409 по существу в очередь не уходит", async () => {
  server.use(http.post("/api/v1/scans", () => HttpResponse.json(
    { detail: { code: "scan_product_mismatch", message: "Товар не совпадает" } }, { status: 409 })));
  renderPanel();
  await scanCode("0104006396053947217ABCDEF");
  expect(screen.queryByText(/в очереди/i)).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `npm --prefix frontend run test -- WarehousePanel`
Expected: FAIL, текста «сохранено локально» нет

- [ ] **Step 3: Реализовать хук и подключить к `submitScan`**

Ключевая правка внутри `catch` в `submitScan`: разделить сетевой отказ и отказ
по существу, первый отправить в `enqueueScan`, второй оставить как есть

- [ ] **Step 4: Запустить тесты и убедиться, что они проходят**

Run: `npm --prefix frontend run test -- WarehousePanel`
Expected: PASS

- [ ] **Step 5: Прогнать полный фронтовый набор**

Run: `npm --prefix frontend run lint && npm --prefix frontend run typecheck && npm --prefix frontend run test && npm --prefix frontend run a11y`
Expected: PASS

- [ ] **Step 6: Коммит**

```bash
git add frontend/src/features/warehouse/offline/useOfflineQueue.ts frontend/src/features/warehouse/WarehousePanel.tsx frontend/src/__tests__/WarehousePanel.test.tsx
git commit -m "feat(web-offline): скан уходит в очередь при недоступном backend"
```

---

### Task 7: Операторский вид очереди и заблокированных сканов

**Files:**
- Create: `frontend/src/features/warehouse/OfflineQueuePanel.tsx`
- Modify: `frontend/src/features/warehouse/WarehousePanel.tsx` (вставка панели),
  `frontend/src/features/warehouse/WarehousePanel.css`
- Test: `frontend/src/__tests__/WarehousePanel.test.tsx` (дописать)

**Interfaces:**
- Consumes: `useOfflineQueue` из Task 6
- Produces: компонент `OfflineQueuePanel`, отдельного API не даёт

Здесь закрывается открытый P2 десктопа: заблокированное событие обязано быть
видно оператору независимо от того, какая позиция сейчас открыта

Требования:

- счётчик «В очереди: N» виден всегда, когда `N > 0`, и не прячется при смене позиции;
- кнопка «Отправить сейчас» вызывает `replayNow`;
- заблокированные сканы показываются списком: короткий код, товар, причина
  словами, время;
- каждый заблокированный скан снимается только явным действием оператора,
  сам по себе не исчезает;
- панель доступна с клавиатуры и проходит `npm --prefix frontend run a11y`;
- при непустой очереди завершение заказа недоступно с пояснением
  «Сначала отправьте очередь», иначе backend получит `order_complete` раньше сканов

- [ ] **Step 1: Написать падающие тесты**

```tsx
it("заблокированный скан виден после переключения позиции", async () => { /* ... */ });
it("кнопка Отправить сейчас повторяет очередь", async () => { /* ... */ });
it("завершение заказа заблокировано, пока очередь непуста", async () => { /* ... */ });
it("заблокированный скан снимается только явным действием", async () => { /* ... */ });
```

- [ ] **Step 2: Запустить и убедиться, что падают**

Run: `npm --prefix frontend run test -- WarehousePanel`
Expected: FAIL

- [ ] **Step 3: Реализовать компонент и вставить в панель**

- [ ] **Step 4: Запустить тесты и a11y**

Run: `npm --prefix frontend run test && npm --prefix frontend run a11y`
Expected: PASS

- [ ] **Step 5: Коммит**

```bash
git add frontend/src/features/warehouse/OfflineQueuePanel.tsx frontend/src/features/warehouse/WarehousePanel.tsx frontend/src/features/warehouse/WarehousePanel.css frontend/src/__tests__/WarehousePanel.test.tsx
git commit -m "feat(web-offline): операторский вид очереди и заблокированных сканов"
```

---

### Task 8: Playwright-сценарий на реальном IndexedDB

> **Выполнена частично 2026-08-04, коммит `5fcadab`.**
> Сделан уровень хранилища: `frontend/e2e/offline-queue-store.spec.ts`, девять
> сценариев против реального IndexedDB в Chromium
> Он закрывает ограничение «IndexedDB проверен только чтением», ради которого
> задача и бралась вперёд очереди
>
> Сценарий уровня интерфейса (`offline-scan.spec.ts` из описания ниже) требует
> Task 6 и остаётся в фазе 2: без подключения очереди к панели сканировать в
> браузере нечего
>
> Мутационная проверка обязательна и здесь: первая версия теста дедупликации
> проходила даже со снятой защитой, потому что в IndexedDB дедуп держится на
> `keyPath` Настоящий риск это сброс `attempts` и перестановка события в конец
> очереди, он закрыт отдельным тестом

**Files:**
- Create: `frontend/e2e/offline-scan.spec.ts`
- Modify: `frontend/e2e/synthetic-api.ts` (переключатель отказа сети)

**Interfaces:**
- Consumes: собранное приложение и synthetic API, внешних запросов нет

Unit-тесты идут на memory-сторе, поэтому реальный IndexedDB проверяется только здесь

Сценарий:

1. открыть `/`, выбрать заказ и позицию;
2. `page.route` рубит `POST /api/v1/scans` в сетевую ошибку;
3. отсканировать два валидных кода, увидеть «В очереди: 2»;
4. `page.reload()`, счётчик остался «В очереди: 2», это и есть durable;
5. снять блокировку сети, дождаться автоповтора, счётчик ушёл в ноль;
6. проверить, что synthetic API получил ровно два `POST /scans` с ожидаемыми кодами;
7. отдельный прогон: сервер отвечает 409 `order_closed`, скан уходит в blocked
   и остаётся виден после reload

- [ ] **Step 1: Написать сценарий**
- [ ] **Step 2: Запустить и убедиться, что падает на текущем коде**

Run: `npm --prefix frontend run e2e -- offline-scan`
Expected: FAIL

- [ ] **Step 3: Довести реализацию до зелёного**

Run: `npm --prefix frontend run e2e`
Expected: PASS, внешних сетевых запросов нет

- [ ] **Step 4: Коммит**

```bash
git add frontend/e2e/offline-scan.spec.ts frontend/e2e/synthetic-api.ts
git commit -m "test(web-offline): e2e сценарий очереди на реальном IndexedDB"
```

---

### Task 9: Оболочка приложения офлайн, отдельное решение

**Files:**
- Create: `frontend/public/sw.js`, `frontend/src/registerServiceWorker.ts`
- Modify: `frontend/nginx.conf.template` (заголовки кеша для `sw.js`)

Task 1-8 спасают сканы, пока вкладка открыта
Если оператор перезагрузит страницу при мёртвой сети, приложение не загрузится:
очередь в IndexedDB уцелеет, но работать будет не в чем

Service worker это отдельное решение с своей ценой:

- SW залипает у клиента, кривая версия ломает панель до ручного сброса;
- обязателен kill switch: пустой `sw.js`, снимающий регистрацию, и проверка,
  что он раскатывается;
- `sw.js` отдаётся с `Cache-Control: no-cache`, иначе обновление не доедет;
- кешируется только оболочка, никаких ответов API.

Делать эту задачу только после того, как фазы 1-2 отработали на складе

- [ ] **Step 1: Отдельное согласование необходимости**
- [ ] **Step 2: Kill switch и его проверка в e2e до самого кеширования**
- [ ] **Step 3: Кеширование оболочки, версия в имени кеша**
- [ ] **Step 4: e2e: reload при мёртвой сети открывает панель и показывает очередь**

---

## Фаза 4, приёмка

Автотесты не доказывают складскую работу
После фазы 2 нужен прогон по `docs/web-warehouse-acceptance.md`, раздел B,
с добавленным офлайн-сценарием:

1. реальный сканер, реальный заказ в тестовом окне;
2. физически отключить сеть на рабочей станции;
3. отсканировать несколько блоков, убедиться, что поле очищается и счётчик растёт;
4. вернуть сеть, дождаться автоповтора;
5. сверить в `/admin` и в аудите, что записаны ровно те коды и ровно один раз;
6. повторить с перезагрузкой вкладки в середине офлайн-отрезка

Stop condition: любой дубль КИЗа, потерянный код, расхождение аудита

## Риски, которые план не закрывает

| Риск | Суть | Что делаем |
|---|---|---|
| Сессия истекает | web-сессия живёт `TAKSKLAD_WEB_SESSION_TTL_SECONDS`, по умолчанию 24 часа (`backend/app/settings.py:186`) | Очередь переживает logout и истечение, повтор просит войти заново, 401 классифицируется как `retry`, а не как потеря |
| Конфликты всплывают поздно | Офлайн нет preflight, занятый КИЗ выяснится только при повторе | Заблокированные события видны оператору, Task 7 |
| Гонки при двух клиентах | Отложенный web-скан приезжает в backend позже десктопного завершения заказа, это те же P0-гонки из паритета | План их не чинит, они закрываются отдельно в `orders_service.py` и `order_actions_service.py` |
| IndexedDB недоступен | Приватный режим, политика браузера, переполнение квоты | Панель обязана показать явную ошибку и вернуться к прежнему поведению, а не делать вид, что скан сохранён |
| Две вкладки | Параллельный повтор шлёт дубли | `navigator.locks`, плюс backend идемпотентен, дубль безопасен |
| Печать | Офлайн-печать сводного листа в этот план не входит | Отдельный P1 и отдельный план |
