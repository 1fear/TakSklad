# TakSklad: текущий статус

Дата сверки: 2026-07-20.

Статус: `PARTIALLY_CONFIRMED_LIVE_NOT_READY`.

## Короткий вывод

TakSklad является рабочей складской системой с Windows desktop, web, backend API,
PostgreSQL и серверными workers. Код и документация подтверждают DB-only архитектуру,
но текущий production backend нельзя считать полностью готовым: публичный `/ready`
вернул `503`, а верхнеуровневый readiness показал `workers=unhealthy`.

Причина нездорового состояния конкретного worker не диагностировалась в этой
документационной задаче. Production write, restart, deploy и исправление данных не
выполнялись.

## Назначение и рабочий контур

TakSklad закрывает следующие процессы:

- импорт и нормализацию Excel-заказов;
- хранение заказов, импортов, КИЗов, движений и аудита в PostgreSQL;
- сканирование, проверку дублей и сохранение КИЗов;
- локальную идемпотентную очередь desktop при временной недоступности backend;
- web-контроль, отчеты и экспорт;
- Telegram import/report flow;
- SkladBot заявки, возвраты и read-only отчеты в подтвержденных контурах;
- Smartup auto-import в пределах текущего backend-контракта;
- Windows update channel через GitHub Release.

Operational source of truth — PostgreSQL через backend API. Google Sheets не
является runtime-хранилищем или fallback. Excel остается форматом импорта и экспорта.

## Разделение источников истины

| Слой | Статус на 2026-07-20 | Evidence и граница |
|---|---|---|
| Docs truth | `CONFIRMED` | `README.md`, `docs/db-only-architecture.md` и runbook описывают DB-only контур и безопасный release/deploy. |
| Code truth | `CONFIRMED_LOCAL_ONLY` | Локальный `main` на `cef29859f5ec31288cf2a2e34005f3d7831dc000`; desktop `APP_VERSION=2.0.54`, backend `APP_VERSION=2.0.51`. В worktree есть чужие untracked artifacts; он не является готовым production source bundle. |
| Test truth | `NOT_RUN_THIS_REVIEW` | В рамках этой документационной задачи тесты приложения и CI не запускались. Старые test/CI records не доказывают текущий live. |
| Live truth | `PARTIAL_FAIL` | Read-only public probe: `/health` `200`, `/version` `200`, `/ready` `503`. Детали ниже. |
| Data truth | `NOT_CHECKED` | Production DB, очереди, заказы, КИЗы и отчеты не читались. |
| Operator truth | `NOT_CHECKED` | Windows workstation, Telegram, печать, scanner и физический складской сценарий не проверялись. |

## Текущая идентичность контуров

Эти идентификаторы нельзя смешивать:

- локальный checkout: `cef29859f5ec31288cf2a2e34005f3d7831dc000`;
- public desktop update manifest: версия `2.0.54`, source SHA
  `63f4506404408d44c02a8aa626f2fee2f26c526b`;
- live backend на read-only probe: версия `2.0.51`, commit prefix `87cdc9d5`.

Локальная ветка на момент сверки была `ahead 4, behind 1` относительно
`origin/main`. Это не ошибка само по себе, но запрещает считать текущий checkout
точной копией production или публичного desktop release без отдельной сверки.

## Live truth: безопасная публичная проверка

Дата проверки: 2026-07-20. Базовый URL: `https://api.taksklad.uz`.

| Endpoint | HTTP | Наблюдение |
|---|---:|---|
| `/health` | `200` | `status=ok`, версия `2.0.51`, commit prefix `87cdc9d5`. |
| `/version` | `200` | Та же версия и runtime identity. |
| `/ready` | `503` | `ready=false`, общий статус `unhealthy`; `database=ok`, `migrations=ok`, `daily_report=ok`, `desktop_pairing=ok`, `workers=unhealthy`. |

Readiness фиксирует наблюдаемую верхнеуровневую причину общего fail, но не root
cause конкретного worker. До диагностики и повторного `/ready=200` нельзя писать
`production ready`.

## Активное направление

1. Надежность Windows desktop, локальной очереди, backup и recovery.
2. PostgreSQL/backend как единственный operational source of truth.
3. Безопасные SkladBot, Smartup и Telegram integrations.
4. Отчеты, аудит, observability и понятная ручная приемка.
5. Web/admin контроль без ослабления auth и release gates.

## Ближайшие доказуемые действия

1. Read-only диагностировать, какой обязательный worker делает readiness unhealthy.
2. Сверить exact live backend SHA, public desktop release SHA и актуальный
   `origin/main`; не использовать dirty checkout как release source.
3. После исправления пройти focused tests, CI, deploy gates и повторный
   `/health` + `/version` + `/ready` smoke.
4. Отдельно выполнить operator acceptance на реальном Windows workstation,
   Telegram/печати/scanner flow без тестовых production-write действий вне
   согласованного scope.

## Читать дальше

1. [Server topology и live endpoints](SERVER.md).
2. [Доступы без секретов](ACCESS.md).
3. [DB-only архитектура](db-only-architecture.md).
4. [Deploy и rollback](deploy-rollback-runbook.md).
5. [Ручная приемка](manual-acceptance-runbook.md).
