# Deploy And Rollback Runbook

Дата фиксации: 2026-05-30.

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

- `api.taksklad.uz` резолвится в `159.195.138.95`;
- SSH host: `root@159.195.138.95`;
- runtime app path: `/opt/stacks/taksklad/app`;
- старый адрес `135.181.245.84` не использовать для текущего production deploy.

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

Временный staging URL до переключения DNS:

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
