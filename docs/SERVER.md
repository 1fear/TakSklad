# TakSklad: server context

Дата сверки: 2026-07-20.

Этот файл хранит только безопасную топологию и точки проверки. Он не содержит
паролей, токенов, private keys, cookies, chat IDs или содержимого env-файлов.

## Канонический production контур

| Поле | Значение | Класс истины |
|---|---|---|
| Public API | `https://api.taksklad.uz` | Docs + public live probe |
| Server app path | `/opt/stacks/taksklad/app` | Deploy runbook |
| Старый path | `/opt/taksklad/app` — не использовать для новых deploy | Deploy runbook |
| Compose file | `deploy/vds/docker-compose.yml` | Code truth |
| Deploy/rollback | `docs/deploy-rollback-runbook.md` | Docs truth |
| Access registry ID | `taksklad.production` | Access contract |

Runtime secret materialization и SSH-параметры намеренно не описаны здесь. Маршрут
к доступам: [ACCESS.md](ACCESS.md) и центральный
`/Users/anton/.codex/ACCESS_INDEX.md`.

## Компоненты

Production compose описывает:

- PostgreSQL и WAL/backup infrastructure;
- `backend-api`;
- `frontend`;
- `telegram-worker`;
- `skladbot-worker`;
- `smartup-auto-import-worker`;
- опциональные admin/principal services.

Наличие сервиса в compose не доказывает, что именно этот service set сейчас
запущен, healthy и использует ожидаемый image digest. Для этого нужен отдельный
read-only runtime inventory.

## Публичные read-only endpoints

| Endpoint | Назначение | Ожидаемый безопасный результат |
|---|---|---|
| `GET /health` | Процесс и runtime identity | `200`, `status=ok` |
| `GET /version` | Версия, commit/image/release identity | `200` |
| `GET /ready` | DB, migrations, workers и обязательные policies | `200` только при полной готовности; `503` при обязательном fail |

Read-only команды без auth:

```bash
curl -fsS https://api.taksklad.uz/health
curl -fsS https://api.taksklad.uz/version
curl -fsS https://api.taksklad.uz/ready
```

Не сохранять полный response в общие логи, если schema ответа изменилась и в нем
появились operational identifiers. Для автоматизации выбирать только status,
version и верхнеуровневые readiness states.

## Live snapshot

Read-only probe 2026-07-20:

- `/health`: `200`, `status=ok`, version `2.0.51`, commit prefix `87cdc9d5`;
- `/version`: `200`, та же runtime identity;
- `/ready`: `503`, `ready=false`, `workers=unhealthy`;
- database, migrations, daily report и desktop pairing на верхнем уровне были `ok`.

Root cause unhealthy worker, compose/service state, logs, restart count и resource
usage не проверялись. Статус подробнее: [CURRENT_STATUS.md](CURRENT_STATUS.md).

## Release и deploy boundary

Production activation допускается только по действующему runbook:

1. exact source SHA и verified `release.json`;
2. immutable `image@sha256` и attestations;
3. свежий backup и rollback record;
4. migration/preflight gates;
5. activation без source build на VDS;
6. `/health`, `/version`, `/ready`, acceptance и log scan.

SSH, deploy, restart, migration, restore, DB write, worker retry и cleanup являются
production mutations. Они требуют отдельного явного разрешения, backup/rollback и
stop condition. Обычный `git push` production не деплоит.
