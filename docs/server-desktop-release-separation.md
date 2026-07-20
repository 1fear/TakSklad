# Разделение серверных и Windows-релизов

Статус контракта: текущий desktop `2.0.53`, минимально поддерживаемый desktop `2.0.49`, API contract `1`.

## Цель

Backend, серверные workers и web-панель должны обновляться без новой установки
Windows-приложения. Публичный `version.json` и Windows-артефакты меняются только
при отдельном desktop/full release.

Server-only release обязан сохранить уже работающий складской процесс. Новая
серверная функциональность не является основанием менять desktop-контракт.

## Границы релизов

### Server-only

- источник: один exact commit SHA из `main` с зелёным Release gate;
- артефакты: immutable backend/frontend images и серверный manifest;
- допустимы: новые серверные обработчики, workers и additive API;
- запрещены: сборка Windows EXE/ZIP, изменение `src/taksklad/config.py`, promotion
  `version.json`, удаление или переименование API contract `1`;
- результат deploy идентифицируется server release ID, commit SHA и image digest,
  а не Windows-версией.

### Desktop/full

Нужен, если изменяется Windows-код, локальная конфигурация, обязательный API-вызов,
схема существующего ответа или минимальная поддерживаемая desktop-версия. Только
этот контур может создать новые Windows-артефакты и отдельно обновить
`version.json` после подписания и проверки.

## Frozen API contract 1

`tools/check_desktop_api_contract.py` статически проверяет реально используемые
desktop contract `1` (совместимые линии `2.0.49`-`2.0.53`) HTTP methods/routes,
Bearer authentication и минимальные scopes.
Удаление, переименование, смена метода, усиление auth без совместимого desktop
credential или потеря scope блокируют server-only release. Дополнительные
серверные routes и дополнительные поля в ответах разрешены.

Обязательные группы API:

- active orders и синхронизация источников;
- import/preview;
- KIZ availability, scan, undo и complete;
- returns list/lookup/write;
- day report;
- public `/health`.

Gate запускается без production credentials и данных:

```bash
PYTHONPATH=. python tools/check_desktop_api_contract.py
PYTHONPATH=. python -m unittest tests.test_desktop_api_contract_frozen
```

Изменять frozen-набор можно только в составе согласованного desktop/full release.
Новый server endpoint сам по себе не добавляется в frozen-набор.

## Миграции БД

Первый server-only release разделения не содержит новой Alembic migration.

Для последующих server-only релизов разрешена только поэтапная expand-only схема:

1. добавить nullable/default-compatible колонку или новую таблицу;
2. выпустить код, который понимает старую и новую форму, при необходимости dual-write;
3. выполнить проверяемый bounded backfill отдельно;
4. переключить чтение только после data preflight;
5. удаление старой формы — отдельный поздний contract release после подтверждения,
   что desktop contract `1` и rollback runtime её не используют.

Drop/rename, обязательная колонка без совместимого default, изменение смысла
существующего поля и автоматический Alembic downgrade запрещены.

## Stop conditions

До activation server-only release останавливается, если выполняется хотя бы одно:

- frozen contract gate не зелёный;
- source SHA, manifest, attestation или image digest не совпадают;
- Release gate не зелёный на exact SHA;
- migration head расходится либо release содержит несогласованную contract migration;
- backup/rollback record отсутствует или не проверен;
- pre-deploy `/health`, `/ready` или data-free auth canary не зелёные;
- изменены `src/taksklad/config.py`, Windows artifacts или `version.json`;
- нужны новые scopes/credential rotation, но они не provisioned и не доказаны;
- обнаружены новые client-facing Telegram/XLSX/UI изменения без отдельного approval.

После activation немедленный runtime rollback запускается при несовпадении identity,
неуспешных `/health` или `/ready`, отказе desktop auth canary, ошибке frozen API smoke,
аномальном fresh log scan или потере обязательного worker heartbeat.

## Rollback

Rollback использует только предыдущий проверенный server manifest и прежние image
digests. Windows-клиент и `version.json` при server-only rollback не меняются.

Порядок:

1. остановить дальнейшее расширение rollout;
2. сохранить read-only `/ready`, event/operations diagnostics и fresh logs;
3. активировать exact previous backend/frontend images без build/checkout;
4. не выполнять Alembic downgrade;
5. повторно проверить runtime identity, `/health`, `/ready`, desktop contract gate и
   data-free desktop auth canary;
6. если подтверждение не получено — статус `rollback_unverified`, дальнейшие writes
   и повторный deploy запрещены до ручного разбора.

Production DB restore не является обычным rollback и требует отдельного явного
разрешения, свежего backup и отдельного плана восстановления.
