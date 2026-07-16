# Smartup fulfillment saga

## Назначение и граница безопасности

Production-safe режим — `SMARTUP_AUTO_IMPORT_SAGA_MODE=enforced`. Он запрещает считать слот завершённым, пока для каждого подтверждённого Smartup deal не выполнено одно из условий:

- канонический TakSklad-заказ уже связан с заявкой SkladBot;
- для него существует ровно один durable `skladbot_request_create` intent.

Флаги независимы:

- `SMARTUP_AUTO_IMPORT_BACKEND_IMPORT_ENABLED` — локальный импорт;
- `SMARTUP_AUTO_IMPORT_CHANGE_STATUS_ENABLED` — Smartup status write;
- `SMARTUP_AUTO_IMPORT_SAGA_MODE=disabled|shadow|enforced` — legacy, наблюдение или владение workflow;
- `SMARTUP_AUTO_IMPORT_PROCESS_SKLADBOT_NOW=false` — обязательное значение для saga: внешний POST выполняет отдельный worker.

Default и быстрый rollback — `disabled`. Переключение режима не удаляет fulfillment/event/order данные. После rollback незавершённые durable записи остаются для расследования.

## Business identity и данные

Authoritative таблицы:

- `smartup_fulfillments` — один workflow на `source_scope + deal_id + request_type + revision`;
- `smartup_fulfillment_orders` — 1:N связь workflow с каноническими `Order.id`, собственным state, SkladBot create-event и remote request ID;
- `pending_events` типа `smartup_deal_saga` — переходный outbox/compatibility слой.

Workflow key не зависит от даты запуска, слота, Excel-файла, номера части и retry ImportJob. Payload hash фиксирует deal, целевой статус, дату отгрузки и канонические Order ID. Изменение payload в той же revision переводит workflow в `payload_mismatch`, а не создаёт второй внешний процесс.

Duplicate-only retry обязан вернуть `ImportResult.resolved_order_ids`; SkladBot queue строится по этим Order ID, а не по новому пустому ImportJob.

## State machine

| State | Durable факт | Следующий безопасный шаг |
|---|---|---|
| `local_ready` | Order, mapping и intent сохранены | Зафиксировать `smartup_write_started`, затем write |
| `smartup_write_started` | Write мог дойти до Smartup | Только exact deal read-back |
| `smartup_ambiguous` | Результат не доказан | Backoff + read-back; без blind retry |
| `smartup_confirmed` | Ответ или read-back подтвердил `B#W` | Создать/найти SkladBot intent по Order ID |
| `skladbot_create_queued` | Durable create intent существует | SkladBot worker выполняет POST |
| `skladbot_post_started` | POST мог дойти до SkladBot | Exact marker reconciliation |
| `skladbot_ambiguous` | Результат POST не доказан | Manual review или exact marker match; без повторного POST |
| `skladbot_created` | Canonical WH-R/ID сохранён | Terminal success |
| `blocked_stock` | SkladBot подтвердил shortage 4xx | Сохранить Order, incident/manual review |
| `payload_mismatch`, `blocked_validation`, `manual_review` | Автоматическое продолжение небезопасно | Решение оператора |

Consistency sweeper запускается каждым циклом Smartup worker в `enforced` и восстанавливает незавершённые workflow независимо от исходного слота.

## Smartup retry rule

Перед повтором после `smartup_write_started`/ошибки выполняется read-only `order$export` по точному `deal_id`:

- `B#W` — write не повторять, перейти в `smartup_confirmed`;
- подтверждённый исходный статус — разрешён один новый write после durable transition;
- пустой/неоднозначный ответ — оставить workflow ambiguous с backoff/manual review.

## SkladBot retry rule

В comment каждого create payload добавляется `TakSklad ref: TSF-<24 hex>`. Timeout, network и 5xx считаются ambiguous. После них разрешён только поиск exact marker; blind POST запрещён. Текущий list API не доказывает исчерпывающую пагинацию, поэтому отсутствие marker не является доказательством отсутствия заявки и требует manual review.

Stock shortage признаётся только по детерминированному 4xx. Order/OrderItem/Google-строка не удаляются; ставится `blocked_stock`, incident и уведомление.

## Google → backend circuit breaker

- Неизменившиеся KИЗ отбрасываются до advisory lock.
- Новые коды обрабатываются стабильными партиями максимум 32 с commit/audit checkpoint.
- `out of shared memory/max_locks_per_transaction` открывает circuit на минимум 900 секунд.
- Circuit хранится в audit log, переживает restart и после cooldown допускает только один half-open probe под advisory lease; закрывается только успешным sync.
- Outbound Google export продолжает работать; reverse sync возвращает `paused`.
- `/ready` показывает `google_backend_sync.circuit_open=true` как optional degraded.

## Telegram routing и доставка логистики

В production Smartup worker работает fail-closed:

- `SMARTUP_AUTO_IMPORT_CLIENT_CHAT_ID` и `SMARTUP_AUTO_IMPORT_LOGISTICS_CHAT_ID` — numeric group-like targets; они могут совпадать;
- `TAKSKLAD_AUTOMATION_ALERT_CHAT_ID` — numeric personal-like target, входит в `TELEGRAM_ADMIN_CHAT_IDS` и отличается от report routes;
- legacy `SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID` пустой либо совпадает с unified alert;
- `SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY` задан и стабилен между deploy/rollback.

Raw chat ID и fingerprint key не пишутся в audit/status. Event хранит только HMAC route fingerprint, роль и provenance. Автофайл/caption помечены `AUTO Smartup`, ручной `/logistics` — `MANUAL /logistics`.

Логистика запускается отдельным due/recovery-контуром. Время по умолчанию равно `SMARTUP_AUTO_IMPORT_FINAL_TIME`; override — `SMARTUP_AUTO_IMPORT_LOGISTICS_DUE_TIME`. Failed send остаётся durable `failed`, повторяется с bounded exponential backoff и после cap блокирует status/readiness. Catch-up использует Smartup import metadata и наличие подходящих Orders на ожидаемую дату.

Legacy `sent` audit без route fingerprint по умолчанию считается доставленным и мигрируется в completed v2 event без повторной отправки. Для подтверждённого misroute допускается ровно одна export-date через `SMARTUP_AUTO_IMPORT_LOGISTICS_ROUTE_RECOVERY_EXPORT_DATE=YYYY-MM-DD`; после успешной отправки v2 idempotency блокирует дубль. Не оставлять recovery-date включённой после подтверждения доставки.

## Rollout и rollback

1. Backup PostgreSQL и фиксация текущих image digests/commit SHA.
2. Deploy кода с `SMARTUP_AUTO_IMPORT_SAGA_MODE=disabled` и Google reverse sync выключенным.
3. `alembic upgrade head`; ожидаемый head — `20260716_0019`.
4. Проверить `/ready`, worker heartbeat, отсутствие migration/queue errors.
5. Включить `enforced`, оставить `SMARTUP_AUTO_IMPORT_PROCESS_SKLADBOT_NOW=false`.
6. Canary: один тестовый/следующий реальный deal; доказать `Order → fulfillment → create event → WH-R` и отсутствие второго Smartup write.
7. Только после canary вернуть `TAKSKLAD_GOOGLE_TO_BACKEND_SYNC_ENABLED=true`; проверить batches ≤32, circuit closed и стабильные locks.

Stop conditions: migration mismatch, `payload_mismatch`, ambiguous Smartup/SkladBot, coverage invariant failure, повторный `out of shared memory`, рост duplicate WH-R или неизвестное физическое состояние. При stop: выключить reverse sync, вернуть saga mode `disabled`, не удалять durable записи и не повторять внешние POST вручную без read-back.

## Evidence

Статус-команда показывает fulfillment state counts и `fulfillment_manual_review`. В evidence разрешены counts, state, hash workflow key и redacted IDs. Credentials, auth headers, client payloads и production markers публиковать нельзя.
