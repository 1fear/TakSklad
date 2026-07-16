# Полный отказ TakSklad от Google Sheets

## Цель

Единственный operational source of truth — PostgreSQL. Desktop, web, Telegram,
Smartup и SkladBot работают только через backend API. Excel остаётся форматом
импорта и выгрузки, но не хранилищем.

После cutover в runtime не должно быть:

- чтения или записи Google Sheets;
- Google → PostgreSQL синхронизации;
- `google_sheets_export` в очереди новых событий;
- Google fallback на desktop;
- Google credentials, `gspread` и отдельного Google worker;
- Google-полей и управляющих действий в web-панели;
- зависимости readiness от Google.

## Инварианты склада

1. PostgreSQL сохраняет все заказы, позиции, сканы, возвраты и историю КИЗов.
2. `return`, `undo` и `reset` освобождают КИЗ; следующая отгрузка создаёт
   `re_outbound`.
3. Один активный КИЗ нельзя одновременно привязать к разным позициям.
4. Повторный импорт или повтор очереди не создаёт дубли.
5. Локальный scan backup и `pending_backend_events` остаются страховкой при
   временном отсутствии сети.
6. Старый desktop не может переключить работу обратно на Google.
7. Исторические Google-события не удаляются молча: они закрываются аудируемо.

## Release candidate — containment

- Запретить Google → PostgreSQL mutations.
- Перестать создавать новые `google_sheets_export`.
- Разделить SkladBot sync и Google sync.
- Исключить Google worker/mirror из readiness и operations.
- Сделать backend обязательным для desktop; при его недоступности работать с
  уже загруженным cache можно только без новых неаудируемых записей.
- Подготовить один DB-only desktop release и вручную подтвердить его установку
  на каждом рабочем компьютере до снятия operational pause.

Этот код готовится и проверяется изолированно. Публиковать его частями нельзя:
backend, web, desktop, migration и deploy contract должны относиться к одному
финальному SHA.

## Удаление runtime

- Удалить Google worker из compose и deploy workflow.
- Удалить Google credentials и flags из runtime contract.
- Удалить `gspread`/`oauth2client` из backend и desktop dependencies.
- Удалить Google API/UI contracts, filters, buttons и pending counters.
- Перевести reconciliation на PostgreSQL ↔ SkladBot/Smartup/import metadata.
- Перевести Excel import/export на backend endpoints.

## Preflight данных

Перед production cutover обязательны:

1. Проверенный PostgreSQL backup и PITR checkpoint.
2. Замороженный read-only export последнего состояния Google Sheets.
3. Cutover timestamp в UTC и Asia/Tashkent.
4. Агрегаты по заказам, позициям, сканам, возвратам и KIZ movements.
5. Отдельный список Google-only и DB-only расхождений.
6. Проверка локальных `pending_saves` и `pending_backend_events` на каждом
   рабочем компьютере.
7. Классификация всех `google_sheets_export` со статусами `pending`, `failed`
   или `processing`.
8. Завершение параллельных задач, повторная сверка с актуальным `origin/main` и
   отсутствие конфликтующих release/deploy изменений.

Если найдено легитимное состояние только в Google, cutover останавливается до
его аудируемого переноса в PostgreSQL.

## Cutover

1. Остановить новые импорты и сканирование на короткое согласованное окно.
2. Повторить preflight агрегатов и зафиксировать checkpoint.
3. Остановить и проверить остановку всех writers: `backend-api`, Telegram,
   SkladBot, Smartup и legacy `google-sheets-sync-worker`.
4. Сделать точный PostgreSQL backup уже после полного writer drain. Если backup
   не прошёл проверку, migration не запускать.
5. Применить migration: активные legacy Google events закрываются со статусом
   `cancelled`, marker в payload и отдельной записью audit.
6. Проверить `0` активных legacy Google events и развернуть DB-only backend и
   web из одного проверенного release.
7. Установить DB-only Windows-архив на каждом складе и подтвердить, что
   запускается именно новый `TakSklad.exe`.
8. Выполнить operator smoke.
9. Возобновить работу склада только после зелёной readiness и smoke.
10. Отозвать Google credentials только после rollback window и отдельного
   разрешения владельца.

## Operator smoke

Обязательная последовательность на тестовой партии:

1. Импорт Excel через backend/web.
2. Проверка одинакового заказа на двух рабочих местах.
3. Сканирование unit КИЗа и агрегатного короба.
4. Отмена последнего скана и повторный скан.
5. Завершение заказа.
6. Полный возврат заказа.
7. Сканирование возвращённого КИЗа в новый заказ.
8. Формирование дневного, KIZ и логистического XLSX-отчётов.
9. Проверка SkladBot/Telegram состояния без Google событий.

## Verifier

- Backend unit/integration tests и PostgreSQL migration tests.
- Desktop backend-only, offline queue и KIZ regression tests.
- Frontend typecheck, lint, unit tests и build.
- Compose/config/deploy contract tests.
- `git diff --check` и secret-marker scan.
- После deploy: `/ready`, worker heartbeats, queue summary и отсутствие новых
  `source=google_sheets`/`google_sheets_export` минимум одну рабочую смену.

## Stop conditions

Cutover запрещён, если выполняется хотя бы одно условие:

- есть Google-only заказ, скан, возврат или КИЗ;
- не разобраны локальные очереди или активные Google events;
- не проверен DB backup/restore;
- не проходит `return → new outbound`;
- desktop способен включить Google fallback;
- readiness остаётся красной;
- версии backend, web и desktop не соответствуют одному release SHA.
- хотя бы одна параллельная задача ещё меняет release/deploy/runtime surfaces.

## Rollback

- Откатить application release на предыдущий DB-compatible image.
- При повреждении данных использовать PostgreSQL PITR/backup.
- Старый двусторонний Google worker автоматически не включать: его повторный
  запуск может снова изменить KIZ movements.
- Если rollback требует Google runtime, остановить процесс и запросить отдельное
  решение владельца; Google не является штатным fallback после cutover.
