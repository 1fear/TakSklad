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
| `skladbot_post_started` | POST мог дойти до SkladBot | Exact response-ID recovery или legacy marker reconciliation; без повторного POST |
| `skladbot_ambiguous` | Результат POST не доказан | Manual review, exact response ID или legacy marker; без повторного POST |
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

Новые create payload не содержат `TakSklad ref`/`TSF` или Smartup ID в `comment` и `fields.comment.value`: там остаются только существующий бизнес-текст, тип оплаты и ТП/контакты. Sanitizer удаляет из старых еще не отправленных payload только полные технические строки, распознаваемые canonical marker/Smartup regex; похожая подстрока внутри бизнес-текста сохраняется. Внутренний deterministic marker, idempotency key, payload hash, event/order links и audit остаются в TakSklad и не считаются remote evidence. Smartup ID берется из exact `Order.raw_payload.source_order_id` и связанных строк заказа; daily связывает его только по сохраненному exact SkladBot request ID/number и показывает в XLSX-колонке `Smartup ID`, но не добавляет в Telegram summary.

Timeout, network, 5xx, пустой/невалидный response ID и stale `post_state=started` считаются ambiguous. Если POST вернул request ID, recovery разрешён только через `GET /requests/show/{id}` с тем же ID и непустым WH-R. Без response ID новый markerless event переводится в `blocked/manual_review`: list/fuzzy lookup и повторный POST запрещены. Для legacy event, где сохранённый уже начатый request payload доказывает фактическую отправку marker, сохраняется exact-marker reconciliation; отсутствие marker в неполном list API не доказывает отсутствие заявки.

Event с `attempts > 1` и пустым/неизвестным `post_state` также считается ambiguous: разрешён только exact legacy-marker recovery, иначе manual review. Повторный POST разрешён лишь при явном `post_state=retry_scheduled`; его `available_at` обязан переживать lease finalize. Любой direct processor и worker recovery до Order load или remote call проверяет ownership tuple create-event (`event_type`, `aggregate_type`, `aggregate_id`, `payload.order_id`); чужой или отсутствующий event pointer не может связать WH-R и создаёт manual-review incident/audit.

Stock shortage признаётся только по детерминированному 4xx. Order/OrderItem/Google-строка не удаляются; ставится `blocked_stock`, incident и уведомление.

### SkladBot return-create

- Return payload содержит только business comment; обе comment-копии одинаковы, полная строка технического `TakSklad ref` удаляется.
- Перед любым Order/remote действием event обязан доказать ownership tuple `event_type + aggregate_type=order + aggregate_id=payload.order_id`.
- Первый queued claim сохраняет `post_state=started`, время, idempotency key и payload hash отдельным commit до единственного POST.
- Timeout/network/5xx или ответ без ID переводят event в blocked/manual review. List/fuzzy lookup и повторный POST запрещены.
- После response ID сохраняется `post_state=response_received` отдельным commit. Дальше разрешён только `show/{id}`; link требует совпадающий canonical ID и номер формата `WH-R-*` или `WR-*`.
- Exact-detail retry имеет future backoff 5 минут и общий лимит `SKLADBOT_API_MAX_RETRIES + 1` попыток (по умолчанию 3, максимум 10). После лимита — manual review без повторного POST.
- 429 считается доказанно не принятым POST: event остаётся pending с future backoff; следующий POST возможен только после `available_at`.
- При `TAKSKLAD_EVENT_LEASES_ENABLED=0` PostgreSQL claim-ит одну строку через `SELECT FOR UPDATE SKIP LOCKED`, меняет `processing/attempts` и commit-ит в той же transaction до внешнего вызова.
- SQLite и другие non-PostgreSQL dialects claim-ят одной conditional compare-and-set командой `UPDATE ... RETURNING id`, затем обязательно перечитывают ORM state; process-local mutex не является гарантией.
- Cached batch через commit не переносится ни в одном dialect.
- `already_linked` допустим только для полной canonical связи: положительный decimal ID и номер формата `WH-R-*` или `WR-*`. Partial/conflicting link требует manual review; ID-only восстанавливается только exact-detail при совпадении с durable response ID.
- Normal exact save и полный `already_linked` разрешают связанные `skladbot_return_create` incidents и пишут resolution audit.

## Google → backend circuit breaker

- Неизменившиеся KИЗ отбрасываются до advisory lock.
- Новые коды обрабатываются стабильными партиями максимум 32 с commit/audit checkpoint.
- `out of shared memory/max_locks_per_transaction` открывает circuit на минимум 900 секунд.
- Circuit хранится в audit log, переживает restart и после cooldown допускает только один half-open probe под advisory lease; закрывается только успешным sync.
- Outbound Google export продолжает работать; reverse sync возвращает `paused`.
- `/ready` показывает `google_backend_sync.circuit_open=true` как optional degraded.

## Telegram routing и доставка логистики

В production Smartup worker работает fail-closed:

- `client`, `logistics`, `admin` — три попарно различных typed route; client/logistics group-like, admin personal-like;
- client получает только Smartup export в `12:00`, `15:00`, `17:50` и daily в `22:00`;
- logistics получает только итоговый отчёт в `17:50`; все ошибки и служебные события получает только один admin route;
- legacy `SMARTUP_AUTO_IMPORT_ALERT_CHAT_ID` должен быть пустым; fallback и broadcast между ролями запрещены;
- `SMARTUP_AUTO_IMPORT_ROUTE_FINGERPRINT_KEY` задан и стабилен между deploy/rollback.

Raw production chat ID, token и fingerprint key не коммитятся и не пишутся в audit/status. Event хранит только HMAC route fingerprint, роль и internal provenance. Auto/manual provenance не меняет client-facing caption или filename.

Protected identity anchor хранится только во внешнем Production Environment secret `TELEGRAM_ROUTING_IDENTITY_ANCHOR_SHA256` и формируется вне deploy из канонического role/type/chat-ID mapping. Helper не создаёт anchor из candidate или persisted `.env`; missing, malformed или mismatch блокирует установку и restart. Сам anchor и raw chat IDs не выводятся в лог.

Логистика проверяется после Smartup slots тем же scheduler cycle и отправляется только при durable proof всех ожидаемых slots текущей даты: импорт terminal, все заказы существуют, SkladBot create queue terminal-success, client export terminal-success. Failed/partial/ambiguous dependency блокирует logistics. Build failure до начала Telegram delivery допускает bounded retry; после начала delivery любой неоднозначный результат становится `manual_recovery_required` без автоматического retry.

Любое изменение client-facing Telegram text/caption/filename/label, role/chat mapping, schedule, fallback или message kind требует exact before/after и явного согласования Антона. После согласования обязательны synthetic contract tests и no-send verifier.

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
