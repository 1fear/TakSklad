# Deploy And Rollback Runbook

Дата фиксации: 2026-06-30.

Документ описывает минимальную эксплуатационную процедуру TakSklad 2.0 на VDS. Секреты, пароли, токены и backup-файлы в Git не хранятся.

## 1. DNS И HTTPS

Целевой домен backend:

```text
api.taksklad.uz -> 159.195.138.95
```

На стороне DNS-регистратора нужна A-запись:

```text
type: A
name: api
value: 159.195.138.95
ttl: 300
```

Текущий статус на 2026-06-30:

- `api.taksklad.uz` и `taksklad.uz` резолвятся в `159.195.138.95`;
- production app path на сервере: `/opt/stacks/taksklad/app`;
- старый путь `/opt/taksklad/app` и старый IP `135.181.245.84` не использовать для новых deploy.

После обновления DNS на VDS в `deploy/vds/.env` должно быть:

```text
TAKSKLAD_BACKEND_HOST=api.taksklad.uz
```

Для переключения после готового DNS на VDS:

```bash
cd /opt/stacks/taksklad/app
./deploy/vds/switch_backend_host.sh api.taksklad.uz
```

Если нужно открыть Adminer через отдельный домен:

```bash
cd /opt/stacks/taksklad/app
./deploy/vds/switch_backend_host.sh api.taksklad.uz adminer.taksklad.uz
```

Проверка:

```bash
dig +short api.taksklad.uz
curl -fsS https://api.taksklad.uz/health
```

Fallback-проверка по текущему IP:

```text
https://api.159.195.138.95.sslip.io/health
```

## 2. Deploy Backend

Перед любым production deploy:

```bash
cd /Users/anton/Documents/work/TakSklad
git status --short
git diff --name-only
```

Do not run broad rsync from a dirty tree. Если worktree грязный, deploy должен быть selective deploy: отправлять только проверенные файлы из конкретного reviewed diff/commit. Нельзя копировать весь проект, `outputs/`, локальные runtime JSON, `.env`, credentials, backup-файлы и старые артефакты сборки.

На VDS перед заменой кода создать restore point:

```bash
cd /opt/stacks/taksklad/app
restore_id="pre-backend-only-hot-path-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "/opt/stacks/taksklad/restore_points/$restore_id"
cp -a backend deploy docs tools version.json "/opt/stacks/taksklad/restore_points/$restore_id/"
./deploy/vds/backup_postgres.sh
```

Локально:

```bash
rsync -az backend root@159.195.138.95:/opt/stacks/taksklad/app/
rsync -az --exclude '.env' deploy/vds/ root@159.195.138.95:/opt/stacks/taksklad/app/deploy/vds/
```

На VDS:

```bash
cd /opt/stacks/taksklad/app
./deploy/vds/backup_postgres.sh
docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml run --rm backend-api \
  alembic -c alembic.ini upgrade head
docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml up -d --build backend-api
curl -fsS https://api.taksklad.uz/health
curl -fsS https://api.taksklad.uz/ready
curl -fsS -H "Authorization: Bearer <service-token-from-secret-storage>" \
  https://api.taksklad.uz/api/v1/admin/operations
```

Если VDS database еще не stamped на baseline `20260616_0001`, сначала пройти `docs/database-migrations-runbook.md`. `deploy/vds/apply_schema.sh` не использовать для обычных production upgrades после baseline stamp; он остается только для legacy/bootstrap сценариев пустой БД.

Если DNS временно недоступен, fallback-проверка:

```bash
curl -fsS https://api.159.195.138.95.sslip.io/health
```

## 2.1 Controlled CI/CD

GitHub CI/CD настроен в безопасном режиме:

- `.github/workflows/ci.yml` запускается на `push main`, `pull_request main` и вручную;
- `.github/workflows/deploy-production.yml` запускается только вручную через `workflow_dispatch`;
- обычный `git push` не деплоит production;
- production deploy должен проходить через GitHub Environment `production`, где можно включить required reviewers.

CI проверяет:

```bash
PYTHONPATH=. python -m compileall -q backend/app backend/migrations tools tests
PYTHONPATH=. python -m unittest discover -s tests
PYTHONPATH=. python -m alembic -c backend/alembic.ini heads
TAKSKLAD_ENV_FILE=.env.example docker compose --env-file deploy/vds/.env.example -f deploy/vds/docker-compose.yml config --quiet
npm ci --prefix frontend
npm --prefix frontend run build
```

Для production deploy в GitHub Secrets нужны:

```text
VDS_HOST
VDS_USER
VDS_SSH_KEY
VDS_SSH_KNOWN_HOSTS
VDS_APP_DIR
WINDOWS_CODESIGN_PFX_BASE64
WINDOWS_CODESIGN_PFX_PASSWORD
```

`VDS_APP_DIR` можно не задавать, если production app лежит в стандартном пути `/opt/stacks/taksklad/app`. `VDS_SSH_KNOWN_HOSTS` должен содержать known_hosts строку сервера; не использовать `StrictHostKeyChecking=no`.

Windows signing secrets должны соответствовать заранее закреплённому SHA-256 публичного сертификата в `TRUSTED_WINDOWS_SIGNER_CERT_SHA256`. Сам PFX, пароль и private key не сохраняются в repository, release manifest, artifacts или логах.

Для внутреннего сертификата TakSklad публичные копии root CA и code-signing leaf хранятся в `supply-chain/taksklad-internal-windows-root-ca.pem` и `supply-chain/taksklad-internal-windows-codesign.pem`. Release runner сверяет leaf с PFX, временно добавляет root CA в пользовательский `Root`, а leaf — в `TrustedPublisher`, после сборки удаляя оба. Перед установкой подписанного desktop-релиза оператор должен импортировать root CA в `Trusted Root Certification Authorities`, а leaf — в `Trusted Publishers`; приватные ключи на рабочие станции не переносятся.

### Immutable release candidate

Версия собираемого приложения и уже опубликованный update channel разделены. Во время подготовки кандидата `APP_VERSION` может быть `2.0.26`, пока корневой `version.json` продолжает указывать на проверенный `2.0.25`. Это исключает ссылку клиентов на ещё не существующие подписанные файлы.

Безопасная последовательность:

1. получить один финальный candidate SHA и трижды пройти Phase 26 без production;
2. прогнать `CI / Release gate` на этом SHA во временной release-ветке;
3. применить защиту `main` и Environment `production` без bypass;
4. fast-forward отправить тот же SHA в `main` и дождаться exact-SHA CI;
5. создать новый тег `v<APP_VERSION>` и пустой draft release;
6. `Build Immutable Release` проверяет CI identity, подписывает Windows-файлы, один раз публикует digest-only образы, attestations и unified `release.json`;
7. после проверки всех attestations draft публикуется, а update channel обновляется реальными production-хешами отдельным контролируемым promotion-шагом;
8. production deploy принимает только GitHub/Sigstore manifest, создаёт свежий backup, выполняет count-only preflight, forward-only migration, digest activation и пятиминутный read-only SLO window.

Существующий тег или asset никогда не передвигается и не перезаписывается. `--clobber`, source build на VDS, schema downgrade, restore и автоматический data repair запрещены.

Если `/opt/stacks/taksklad/app` не является git checkout, `deploy/vds/deploy_from_git.sh` делает временный clone из `TAKSKLAD_DEPLOY_REMOTE_URL` и синхронизирует выбранный ref через `rsync --delete`, исключая `.env*`, `outputs`, `backups`, runtime logs, restore points, virtualenv, `node_modules`, `dist`, `__pycache__` и `*.pyc`.

Ручной запуск:

1. GitHub -> Actions -> `Deploy Production`.
2. `ref`: обычно `main`.
3. `services`: `all` или список compose-сервисов через пробел/запятую.
Production workflow запускается только вручную. Acceptance не имеет bypass-режима и всегда обязателен.

Разрешенные сервисы для rebuild/recreate:

```text
backend-api frontend telegram-worker google-sheets-sync-worker skladbot-worker smartup-auto-import-worker
```

Серверный скрипт `deploy/vds/deploy_from_git.sh` выполняет:

1. отказывается деплоить при tracked changes на VDS checkout;
2. создает restore point без `outputs`, `.env`, credentials и backup-файлов;
3. запускает `deploy/vds/backup_postgres.sh`;
4. checkout выбранного git ref или sync выбранного ref из временного clone, если app dir не git checkout;
5. build `backend-api`;
6. `alembic -c alembic.ini upgrade head`;
7. read-only сверяет единственный `alembic current` с единственным `alembic heads` до активации;
8. `docker compose up -d --build --wait --wait-timeout ...` для выбранных сервисов;
9. проверяет JSON-контракт `/health` с retry;
10. проверяет JSON-контракт `/ready`: database/migrations/head/mandatory policy обязаны быть готовы; optional Google degradation допускает `status=degraded` при `ready=true`;
11. обязательно запускает `deploy/vds/acceptance_status.sh --require-go`;
12. выполняет fresh log scan по rebuilt/recreated сервисам.

Любой missing/no-go acceptance, несовпадение migration head, нездоровая БД или обязательная очередь останавливают deploy. `/health` остаётся lightweight-проверкой процесса; `/ready` возвращает HTTP 503 при обязательном отказе.

## 3. Backup

Ручной backup:

```bash
cd /opt/stacks/taksklad/app
./deploy/vds/backup_postgres.sh
```

Проверка расписания:

```bash
systemctl list-timers 'taksklad-postgres-backup.timer'
systemctl status taksklad-postgres-backup.timer
```

## 4. Restore Drill

Restore-drill всегда выполняется в отдельную временную БД и не трогает production database.

```bash
cd /opt/stacks/taksklad/app
latest="$(ls -t /opt/taksklad/backups/postgres/taksklad-postgres-*.sql.gz | head -1)"
./deploy/vds/restore_drill.sh "$latest"
```

Успешный результат должен показать таблицы `orders`, `order_items`, `scan_codes`, `imports` и строку `restore_drill_ok`.

## 5. Production Restore

Production restore перезаписывает текущую БД. Запускать только при осознанном откате данных.

```bash
cd /opt/stacks/taksklad/app
CONFIRM_RESTORE=YES ./deploy/vds/restore_postgres.sh /opt/taksklad/backups/postgres/taksklad-postgres-YYYYmmddTHHMMSSZ.sql.gz
```

После restore:

```bash
curl -fsS https://api.taksklad.uz/health
```

## 6. Rollback Backend Code

Rollback к предыдущему Git-коммиту:

```bash
cd /opt/stacks/taksklad/app
git fetch --all
git checkout <previous-good-commit>
docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml up -d --build backend-api
```

Если код на VDS доставлялся через `rsync`, rollback делается повторным `rsync` из локального checkout предыдущего хорошего коммита.

Rollback после backend-only hot path не должен удалять pending events. До rollback сохранить:

```bash
curl -fsS https://api.taksklad.uz/ready > /tmp/taksklad-ready-before-rollback.json
curl -fsS -H "Authorization: Bearer <service-token-from-secret-storage>" \
  https://api.taksklad.uz/api/v1/admin/events > /tmp/taksklad-events-before-rollback.json
curl -fsS -H "Authorization: Bearer <service-token-from-secret-storage>" \
  https://api.taksklad.uz/api/v1/admin/operations > /tmp/taksklad-operations-before-rollback.json
```

После rollback повторить эти же три проверки. Допустимо уменьшение pending events только если есть audit/sync evidence. Недопустимо внезапное исчезновение `google_sheets_export`, `telegram_excel_import`, scan/order-complete events или open incidents без понятной причины.

### Rollback После Production Hardening 2.0.x

Перед откатом:

```bash
cd /opt/stacks/taksklad/app
./deploy/vds/backup_postgres.sh
docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml ps
curl -fsS https://api.taksklad.uz/health
```

Откат к предыдущему good commit:

```bash
cd /opt/stacks/taksklad/app
git fetch --all
git checkout <previous-good-commit>
docker compose --env-file deploy/vds/.env -f deploy/vds/docker-compose.yml up -d --build backend-api telegram-worker google-sheets-sync-worker skladbot-worker frontend
curl -fsS https://api.taksklad.uz/health
```

Если после релиза уже применялись Alembic migrations, downgrade БД нельзя делать автоматически вместе с кодом. Сначала выполнить backup, затем отдельно проверить конкретный migration downgrade plan. Если сомневаешься, откатывать только код, а БД оставлять на текущей схеме до ручного решения.

## 7. Release Safety

До ручной приёмки нельзя:

- менять `version.json`;
- отправлять desktop push-update;
- собирать и выкладывать Windows archive как обязательное обновление;
- удалять desktop fallback на Google/local режим.

Для включения backend в desktop используются feature flags, а не принудительный переход.

Release guard для backend-only:

- `TAKSKLAD_BACKEND_ONLY_REFRESH=1` включать сначала только на одном Windows workstation/test profile;
- `TAKSKLAD_BACKEND_EMERGENCY_GOOGLE_FALLBACK_ENABLED=0` должен быть default;
- `TELEGRAM_DESKTOP_POLLING_ENABLED=0` должен оставаться default, нормальный Telegram listener - backend worker;
- Windows startup diagnostics должны показать `telegram_desktop_polling=no`, `backend_only_refresh=yes`, `backend_emergency_google_fallback=no`;
- backend `/api/v1/admin/operations` должен показать `shadow_diagnostics` без hot-path blocker перед расширением rollout.

## 8. Acceptance Cleanup

Тестовые acceptance-данные удалять только по явному маркеру.

Dry-run:

```bash
cd /opt/stacks/taksklad/app
./deploy/vds/cleanup_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531"
```

Удаление:

```bash
cd /opt/stacks/taksklad/app
./deploy/vds/cleanup_acceptance_marker.sh "ACCEPTANCE TELEGRAM 20260531" --apply
```

Защита скрипта: marker должен содержать `ACCEPTANCE`, `WEB_UI_SMOKE` или `SMOKE_MVP`.
